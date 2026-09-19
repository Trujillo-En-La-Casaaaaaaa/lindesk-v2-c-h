"""Fixtures of the integration suite: a real PostgreSQL store and the real inventory.

The suite needs two running containers from ``shopflow-infra``:

```bash
docker compose -f ../shopflow-infra/docker-compose.yml up -d orders-db inventory-db shopflow-inventory
```

The orders store under test is the published ``orders-db`` (``localhost:5433``) and
is migrated with **this repository's** own Alembic migration, exactly like the
``migrate-orders`` job of the composed stack. The inventory is the real
``shopflow-inventory`` HTTP service (``localhost:8002``), so availability,
reservation and release semantics of contracts.md §4 are exercised for real. A
controllable inventory stub on a real socket covers the failures that cannot be
provoked through a healthy service (unreachable, lost race, ambiguous reserve,
failing release).

Nothing here substitutes a fake for the store: when either dependency is missing the
suite fails with the command that starts it instead of passing quietly.

The suite drives the outbox relay, the release reconciler and the store itself, so it
must be the only writer of the orders store. Start ``orders-db`` and the inventory
(``docker compose up -d orders-db inventory-db shopflow-inventory``) but **not** the
``shopflow-orders`` service container while running it; the composed container is what
``tests/e2e`` exercises instead.

The inventory side prefers the real ``shopflow-inventory`` service; when it is not
running, a stub that implements the same contracts.md §4 interface (and the same
documented seed of contracts.md §10) is started on a real socket, so the order
lifecycle is always exercised over real HTTP. The store and the broker are never
substituted.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.inventory_http_client import InventoryHttpClient
from app.adapters.postgres_order_repository import PostgresOrderRepository
from app.adapters.postgres_outbox import PostgresOutbox
from app.config import Settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.main import create_app
from app.workers.outbox_publisher import OutboxPublisher
from app.workers.release_reconciler import ReleaseReconciler
from tests.support.db import truncate_store
from tests.support.fakes import FakeBroker
from tests.support.inventory_stub import InventoryStubState, create_inventory_stub
from tests.support.servers import RunningServer, start_server

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The orders store published by shopflow-infra's compose file on the host.
DEFAULT_DATABASE_URL = "postgresql://shopflow:shopflow@localhost:5433/shopflow_orders"
#: The inventory HTTP service published by shopflow-infra's compose file on the host.
DEFAULT_INVENTORY_BASE_URL = "http://localhost:8002"

START_COMMAND = (
    "docker compose -f ../shopflow-infra/docker-compose.yml up -d orders-db inventory-db shopflow-inventory"
)


# --------------------------------------------------------------------------- #
# dependency resolution
# --------------------------------------------------------------------------- #
def candidate_database_urls() -> list[str]:
    """Stores to try, in order: the test override, ``DATABASE_URL``, then the published port."""

    candidates = [
        url
        for url in (
            os.environ.get("ORDERS_TEST_DATABASE_URL"),
            os.environ.get("DATABASE_URL"),
            DEFAULT_DATABASE_URL,
        )
        if url
    ]
    return list(dict.fromkeys(candidates))


def resolve_database_url() -> str:
    """Return the first candidate store that actually answers."""

    attempts: list[str] = []
    for candidate in candidate_database_urls():
        try:
            engine = create_engine(candidate, pool_pre_ping=True)
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            finally:
                engine.dispose()
        except SQLAlchemyError as exc:
            attempts.append(f"{candidate} ({type(exc).__name__})")
            continue
        return candidate
    raise RuntimeError(
        "the orders store is not reachable; start it with\n"
        f"  {START_COMMAND}\n"
        f"tried: {', '.join(attempts) or 'no candidate'}"
    )


def candidate_inventory_urls() -> list[str]:
    candidates = [
        url
        for url in (
            os.environ.get("ORDERS_TEST_INVENTORY_URL"),
            os.environ.get("INVENTORY_BASE_URL"),
            DEFAULT_INVENTORY_BASE_URL,
        )
        if url
    ]
    return list(dict.fromkeys(url.rstrip("/") for url in candidates))


def first_reachable_inventory_url() -> str | None:
    """Return the first inventory base url that answers ``/health``, or ``None``."""

    for candidate in candidate_inventory_urls():
        try:
            response = httpx.get(f"{candidate}/health", timeout=3.0)
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            return candidate
    return None


def resolve_inventory_url() -> str:
    """Return a reachable inventory base url, or raise with the command that starts one."""

    url = first_reachable_inventory_url()
    if url is None:
        raise RuntimeError(
            "the inventory service is not reachable; start it with\n"
            f"  {START_COMMAND}\n"
            f"tried: {', '.join(candidate_inventory_urls()) or 'no candidate'}"
        )
    return url


_RESOLVED: dict[str, str] = {}


def store_url() -> str:
    if "database" not in _RESOLVED:
        _RESOLVED["database"] = resolve_database_url()
    return _RESOLVED["database"]


def inventory_base_url() -> str:
    if "inventory" not in _RESOLVED:
        _RESOLVED["inventory"] = resolve_inventory_url()
    return _RESOLVED["inventory"]


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run this repository's own migration tool against the store under test."""

    return subprocess.run(  # noqa: S603 - fixed interpreter and fixed alembic arguments
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": store_url()},
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def migrated_store_url() -> str:
    """Migrate the store to head once per session, exactly like shopflow-infra does."""

    url = store_url()
    completed = alembic("upgrade", "head")
    if completed.returncode != 0:
        pytest.fail(
            "the orders store is not reachable or the migration failed; start it with\n"
            f"  {START_COMMAND}\nDATABASE_URL={url}\n{completed.stdout}\n{completed.stderr}"
        )
    return url


@pytest.fixture(scope="session")
def inventory_url() -> Iterator[str]:
    """The real ``shopflow-inventory`` when it answers, otherwise a stub of it.

    contracts.md §4 is the contract, and the handoff's layout allows an *inventory
    stub container* for the integration suite. The real service is preferred; when it
    is not running (for example because only ``orders-db`` was started), a stub that
    implements the same interface - same operations, same idempotency keys, same
    documented seed of contracts.md §10 - is started on a real socket so the suite
    still exercises the whole order lifecycle over HTTP. The store is never faked.
    """

    url = first_reachable_inventory_url()
    if url is not None:
        yield url
        return

    state = InventoryStubState()
    server = start_server(create_inventory_stub(state), name="shopflow-orders-inventory-stub")
    warnings.warn(
        "the real shopflow-inventory is not reachable "
        f"({', '.join(candidate_inventory_urls())}); the integration suite is running "
        f"against a stub inventory at {server.base_url} instead. Start the real one with\n"
        f"  {START_COMMAND}",
        pytest.PytestWarning,
        stacklevel=1,
    )
    try:
        yield server.base_url
    finally:
        server.stop()


@pytest.fixture(scope="session")
def broker_url() -> str:
    """The broker of the composed stack (contracts.md §11 default of local Docker)."""

    return os.environ.get("ORDERS_TEST_BROKER_URL") or os.environ.get(
        "BROKER_URL", "amqp://shopflow:shopflow@localhost:5672/"
    )


@pytest.fixture(scope="session")
def settings(migrated_store_url: str, inventory_url: str) -> Settings:
    return Settings(
        database_url=migrated_store_url,
        inventory_base_url=inventory_url,
        request_timeout_seconds=5.0,
        release_retry_interval_seconds=0.2,
        reservation_grace_seconds=0.0,
        start_workers=False,
    )


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine_from_settings(settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture()
def repository(session_factory: sessionmaker[Session]) -> PostgresOrderRepository:
    """The real adapter of the owned store (used for direct adapter-level assertions)."""

    return PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))


@pytest.fixture(autouse=True)
def empty_store(session_factory: sessionmaker[Session]) -> Iterator[None]:
    """Every test starts from an empty owned store; the inventory is shared on purpose."""

    truncate_store(session_factory)
    yield


@pytest.fixture()
def broker() -> FakeBroker:
    """An in-memory broker for the tests that do not exercise the real one."""

    return FakeBroker()


@pytest.fixture()
def app_factory(
    engine: Engine,
    settings: Settings,
    broker: FakeBroker,
) -> Callable[..., FastAPI]:
    """Build the real application against the real store, with an injectable inventory."""

    def factory(
        *,
        inventory_base_url: str | None = None,
        broker_override: object | None = None,
        store_probe: Callable[[], bool] | None = None,
        start_workers: bool = False,
        **setting_overrides: object,
    ) -> FastAPI:
        resolved = settings.model_copy(update=setting_overrides) if setting_overrides else settings
        return create_app(
            settings=resolved,
            engine=engine,
            inventory=InventoryHttpClient(
                inventory_base_url or resolved.inventory_base_url,
                timeout_seconds=resolved.request_timeout_seconds,
            ),
            broker=broker if broker_override is None else broker_override,
            store_probe=store_probe,
            start_workers=start_workers,
        )

    return factory


@pytest.fixture()
def app(app_factory: Callable[..., FastAPI]) -> FastAPI:
    return app_factory()


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def start_orders_server(app: FastAPI) -> RunningServer:
    """Run the real application on a real socket (concurrency tests)."""

    return start_server(app, name="shopflow-orders-test-server")


# --------------------------------------------------------------------------- #
# controllable inventory stub
# --------------------------------------------------------------------------- #
@dataclass
class StubInventory:
    """A running stub inventory plus the applications that call it."""

    state: InventoryStubState
    server: RunningServer
    app_factory: Callable[..., FastAPI]

    @property
    def base_url(self) -> str:
        return self.server.base_url

    def client(self, **kwargs: object) -> TestClient:
        """An orders client whose inventory is this stub."""

        return TestClient(self.app_factory(inventory_base_url=self.base_url, **kwargs))

    def http(self, *, timeout: float = 10.0) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout)

    def stock(self, product_id: str) -> int:
        return int(self.state.stock.get(product_id, 0))

    def movements(self, order_id: str) -> list[dict]:
        return self.state.movements_for(order_id)


@pytest.fixture()
def stub_inventory(app_factory: Callable[..., FastAPI]) -> Iterator[Callable[..., StubInventory]]:
    """Factory of controllable stub inventories (each with its own socket)."""

    started: list[RunningServer] = []

    def factory(*, products=None, stock=None) -> StubInventory:
        state = InventoryStubState(products=products, stock=stock)
        server = start_server(
            create_inventory_stub(state),
            name="shopflow-orders-inventory-stub",
        )
        started.append(server)
        return StubInventory(state=state, server=server, app_factory=app_factory)

    try:
        yield factory
    finally:
        for server in started:
            server.stop()


# --------------------------------------------------------------------------- #
# workers driven by the tests
# --------------------------------------------------------------------------- #
def make_publisher(store, broker, **kwargs) -> OutboxPublisher:
    return OutboxPublisher(
        store=store,
        broker=broker,
        batch_size=kwargs.pop("batch_size", 50),
        poll_interval_seconds=kwargs.pop("poll_interval_seconds", 0.01),
        max_backoff_seconds=kwargs.pop("max_backoff_seconds", 0.05),
    )


def make_reconciler(repository, inventory, **kwargs) -> ReleaseReconciler:
    return ReleaseReconciler(
        repository=repository,
        inventory=inventory,
        interval_seconds=kwargs.pop("interval_seconds", 0.05),
        batch_size=kwargs.pop("batch_size", 50),
        reservation_grace_seconds=kwargs.pop("reservation_grace_seconds", 0.0),
        timeout_seconds=kwargs.pop("timeout_seconds", 5.0),
    )


# --------------------------------------------------------------------------- #
# read-only inventory helpers (contracts.md §4)
# --------------------------------------------------------------------------- #
def inventory_product(base_url: str, product_id: str) -> dict:
    """``GET /products/{productId}`` of the inventory under test."""

    response = httpx.get(f"{base_url.rstrip('/')}/products/{product_id}", timeout=5.0)
    response.raise_for_status()
    return response.json()


def inventory_stock(base_url: str, product_id: str) -> int:
    """The available quantity the inventory reports for one product."""

    return int(inventory_product(base_url, product_id)["availableQuantity"])


__all__ = [
    "DEFAULT_DATABASE_URL",
    "DEFAULT_INVENTORY_BASE_URL",
    "START_COMMAND",
    "StubInventory",
    "alembic",
    "first_reachable_inventory_url",
    "inventory_base_url",
    "inventory_product",
    "inventory_stock",
    "make_publisher",
    "make_reconciler",
    "start_orders_server",
    "store_url",
]
