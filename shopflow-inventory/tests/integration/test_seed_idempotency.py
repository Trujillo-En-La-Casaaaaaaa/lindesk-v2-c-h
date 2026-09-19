"""Integration tests: the deterministic seed command (handoff acceptance 8).

The seed is exercised through its documented entry point - ``python -m app.seed``,
the exact command ``shopflow-infra``'s ``seed-inventory`` job runs - not through a
library call, so the tests prove what infrastructure will actually execute.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import REPO_ROOT, database_url
from tests.support.db import movement_rows, product_rows, stock_rows
from tests.support.requests import reservation_body, reserve_headers

DOCUMENTED_LEVELS = {"p-1001": 50, "p-1002": 25, "p-1003": 10, "p-1004": 100, "p-1005": 0}
DOCUMENTED_PRODUCTS = {
    "p-1001": ("SF-TEE-001", "ShopFlow Classic Tee", "19.99"),
    "p-1002": ("SF-MUG-002", "ShopFlow Ceramic Mug", "12.50"),
    "p-1003": ("SF-HOOD-003", "ShopFlow Hoodie", "49.00"),
    "p-1004": ("SF-STICK-004", "ShopFlow Sticker Pack", "4.25"),
    "p-1005": ("SF-CAP-005", "ShopFlow Cap", "22.00"),
}


def run_seed(database: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the documented seed entry point against the store under test."""

    return subprocess.run(  # noqa: S603 - fixed interpreter and fixed module
        [sys.executable, "-m", "app.seed"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database or database_url()},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_seed_command_exits_zero_and_reports_the_documented_products() -> None:
    completed = run_seed()

    assert completed.returncode == 0, completed.stderr
    for product_id in DOCUMENTED_LEVELS:
        assert product_id in completed.stdout


def test_running_the_seed_twice_leaves_exactly_five_products(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert run_seed().returncode == 0
    assert run_seed().returncode == 0

    products = product_rows(session_factory)
    assert len(products) == 5
    assert len({row["sku"] for row in products}) == 5
    assert stock_rows(session_factory) == DOCUMENTED_LEVELS

    body = client.get("/products").json()
    assert body["total"] == 5
    assert [item["productId"] for item in body["items"]] == list(DOCUMENTED_LEVELS)
    assert {item["productId"]: item["availableQuantity"] for item in body["items"]} == DOCUMENTED_LEVELS


def test_the_seed_never_creates_a_duplicate_or_exceeds_the_levels(session_factory: sessionmaker[Session]) -> None:
    assert run_seed().returncode == 0
    assert run_seed().returncode == 0

    levels = stock_rows(session_factory)
    assert levels == DOCUMENTED_LEVELS
    for product_id, seeded in DOCUMENTED_LEVELS.items():
        assert levels[product_id] <= seeded, product_id
    assert levels["p-1005"] == 0


def test_the_seed_restores_a_drained_catalogue_without_exceeding_it(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A drained store converges back to §10, and never above it."""

    reserved = client.post(
        "/inventory/reservations",
        json=reservation_body("o-drain", ("p-1001", 12), ("p-1003", 4)),
        headers=reserve_headers("o-drain"),
    )
    assert reserved.status_code == 200
    assert stock_rows(session_factory)["p-1001"] == 38

    assert run_seed().returncode == 0

    assert stock_rows(session_factory) == DOCUMENTED_LEVELS
    assert len(product_rows(session_factory)) == 5


def test_the_seed_keeps_the_movement_log_intact(session_factory: sessionmaker[Session]) -> None:
    """The seed owns the catalogue; it must not rewrite the reservation history."""

    with session_factory() as session, session.begin():
        session.execute(
            text(
                "INSERT INTO stock_movements (id, order_id, operation, payload, created_at)"
                " VALUES (gen_random_uuid(), 'o-history', 'reserve', '{\"lines\": []}'::jsonb, now())"
            )
        )

    assert run_seed().returncode == 0

    assert len(movement_rows(session_factory, order_id="o-history")) == 1


def test_the_seed_fails_loudly_when_the_store_is_unreachable() -> None:
    completed = run_seed(database="postgresql://shopflow:shopflow@127.0.0.1:1/shopflow_inventory")

    assert completed.returncode == 1
    assert "FAILED" in completed.stderr


@pytest.mark.parametrize("product_id", sorted(DOCUMENTED_PRODUCTS))
def test_every_documented_product_is_served_with_its_documented_identity(
    client: TestClient, product_id: str
) -> None:
    sku, name, price = DOCUMENTED_PRODUCTS[product_id]

    body = client.get(f"/products/{product_id}").json()

    assert (body["sku"], body["name"]) == (sku, name)
    assert Decimal(str(body["price"])) == Decimal(price)
    assert body["availableQuantity"] == DOCUMENTED_LEVELS[product_id]
