"""A black-box walk of the handoff acceptance criteria, one test per criterion.

Everything is asserted through the HTTP interface of contracts.md §4 against the
real PostgreSQL store, exactly as an evaluator would drive it:

======  ==========================================================================
AC 1    ``GET /products`` (five seeded products, stock, ``inStock``)
AC 2    reserve 2 of ``p-1001`` -> 48, replay -> 48 and ``replayed: true``
AC 3    two concurrent reservations over stock -> one 200, one 409, ledger >= 0
AC 4    release restores exactly once, a repeated release changes nothing
AC 5    release of an order that was never reserved -> 200, nothing changes
AC 6    reserve ``p-1005`` -> 409 and no ``stock_movements`` row
AC 7    release never exceeds the pre-reservation level
AC 8    ``python -m app.seed`` twice -> five products, documented levels
AC 9    ``app/domain`` and ``app/usecases`` import no framework
======  ==========================================================================

AC 8 and AC 9 are also covered in more depth by
``tests/integration/test_seed_idempotency.py``,
``tests/unit/test_architecture.py`` and the contract suite.
"""

from __future__ import annotations

import subprocess
import sys
from ast import Import, ImportFrom, parse, walk
from functools import partial
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import REPO_ROOT, database_url
from tests.support.db import movement_rows, stock_rows
from tests.support.requests import (
    RESERVE,
    fire_concurrently,
    release_body,
    release_headers,
    reservation_body,
    reserve_headers,
    stock_of,
)

DOCUMENTED_LEVELS = {"p-1001": 50, "p-1002": 25, "p-1003": 10, "p-1004": 100, "p-1005": 0}
FORBIDDEN_IMPORTS = {"fastapi", "sqlalchemy", "starlette", "alembic", "uvicorn", "pydantic"}


@pytest.fixture()
def http(server) -> httpx.Client:
    with server.client() as client:
        yield client


def reserve(client: httpx.Client, order_id: str, *lines: tuple[str, int]) -> httpx.Response:
    return client.post(RESERVE, json=reservation_body(order_id, *lines), headers=reserve_headers(order_id))


def release(client: httpx.Client, order_id: str, *lines: tuple[str, int]) -> httpx.Response:
    return client.post(
        "/inventory/releases", json=release_body(order_id, *lines), headers=release_headers(order_id)
    )


def test_acceptance_1_catalog(http: httpx.Client) -> None:
    body = http.get("/products").json()

    assert body["total"] == 5
    assert {item["productId"]: item["availableQuantity"] for item in body["items"]} == DOCUMENTED_LEVELS
    assert {item["productId"]: item["inStock"] for item in body["items"]} == {
        "p-1001": True,
        "p-1002": True,
        "p-1003": True,
        "p-1004": True,
        "p-1005": False,
    }


def test_acceptance_2_reserve_once_and_replay(http: httpx.Client) -> None:
    body = reservation_body("o-ac2", ("p-1001", 2))
    headers = reserve_headers("o-ac2")

    first = http.post(RESERVE, json=body, headers=headers)
    assert first.json() == {"orderId": "o-ac2", "reserved": True, "replayed": False}
    assert stock_of(http, "p-1001") == 48

    replay = http.post(RESERVE, json=body, headers=headers)
    assert replay.json() == {"orderId": "o-ac2", "reserved": True, "replayed": True}
    assert stock_of(http, "p-1001") == 48


def test_acceptance_3_concurrent_reservations_never_oversell(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    assert reserve(http, "o-ac3-fill", ("p-1003", 9)).status_code == 200

    results = fire_concurrently(
        [partial(reserve, http, "o-ac3-a", ("p-1003", 1)), partial(reserve, http, "o-ac3-b", ("p-1003", 1))]
    )

    assert sorted(response.status_code for response in results) == [200, 409]
    assert next(response for response in results if response.status_code == 409).json()["error"][
        "code"
    ] == "insufficient_stock"
    assert stock_rows(session_factory)["p-1003"] == 0
    assert stock_of(http, "p-1003") == 0


def test_acceptance_4_release_once_and_replay(http: httpx.Client, session_factory: sessionmaker[Session]) -> None:
    assert reserve(http, "o-ac4", ("p-1001", 2)).status_code == 200

    first = release(http, "o-ac4", ("p-1001", 2))
    assert first.json() == {"orderId": "o-ac4", "restored": True, "replayed": False}
    assert stock_of(http, "p-1001") == 50

    replay = release(http, "o-ac4", ("p-1001", 2))
    assert replay.json() == {"orderId": "o-ac4", "restored": False, "replayed": True}
    assert stock_rows(session_factory)["p-1001"] == 50


def test_acceptance_5_release_of_a_never_reserved_order(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    before = stock_rows(session_factory)

    response = release(http, "o-ac5", ("p-1001", 2))

    assert response.status_code == 200
    assert response.json() == {"orderId": "o-ac5", "restored": False, "replayed": False}
    assert stock_rows(session_factory) == before
    assert movement_rows(session_factory) == []


def test_acceptance_6_zero_stock_reservation(http: httpx.Client, session_factory: sessionmaker[Session]) -> None:
    response = reserve(http, "o-ac6", ("p-1005", 1))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    assert movement_rows(session_factory) == []
    assert stock_rows(session_factory)["p-1005"] == 0


def test_acceptance_7_release_never_exceeds_the_pre_reservation_value(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    before = stock_rows(session_factory)["p-1002"]
    assert reserve(http, "o-ac7", ("p-1002", 20)).status_code == 200
    assert stock_rows(session_factory)["p-1002"] == before - 20

    assert release(http, "o-ac7", ("p-1002", 20)).json()["restored"] is True

    assert stock_rows(session_factory)["p-1002"] == before


def test_acceptance_8_seed_twice_leaves_the_documented_catalogue(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    for _ in range(2):
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed module
            [sys.executable, "-m", "app.seed"],
            cwd=REPO_ROOT,
            env={**_environment(), "DATABASE_URL": database_url()},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    products = http.get("/products").json()
    assert products["total"] == 5
    assert {item["productId"]: item["availableQuantity"] for item in products["items"]} == DOCUMENTED_LEVELS
    assert stock_rows(session_factory) == DOCUMENTED_LEVELS


def test_acceptance_9_the_core_imports_no_framework() -> None:
    offenders: dict[str, set[str]] = {}
    for package in ("app/domain", "app/usecases"):
        for source in sorted((REPO_ROOT / package).glob("*.py")):
            tree = parse(source.read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in walk(tree):
                if isinstance(node, Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            found = imported & FORBIDDEN_IMPORTS
            if found:
                offenders[source.name] = found

    assert not offenders, offenders


def _environment() -> dict[str, str]:
    import os

    return dict(os.environ)


def test_the_repository_ships_the_required_files() -> None:
    """The deliverables checklist of the handoff, checked against the tree."""

    required = [
        "README.md",
        "Dockerfile",
        ".env.example",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "alembic.ini",
        "app/main.py",
        "app/seed.py",
        "app/api/products.py",
        "app/api/inventory.py",
        "app/domain/product.py",
        "app/domain/stock.py",
        "app/domain/errors.py",
        "app/usecases/catalog.py",
        "app/usecases/availability.py",
        "app/usecases/reserve.py",
        "app/usecases/release.py",
        "app/ports/repositories.py",
        "app/adapters/postgres_catalog.py",
        "app/adapters/postgres_stock.py",
        "app/db/models.py",
        "app/db/session.py",
        "app/config.py",
        "tests/conftest.py",
    ]
    missing = [path for path in required if not (Path(REPO_ROOT) / path).is_file()]

    assert not missing, missing
    assert list((Path(REPO_ROOT) / "migrations" / "versions").glob("*.py"))
    for package in ("unit", "integration", "contract"):
        assert list((Path(REPO_ROOT) / "tests" / package).glob("test_*.py")), package
