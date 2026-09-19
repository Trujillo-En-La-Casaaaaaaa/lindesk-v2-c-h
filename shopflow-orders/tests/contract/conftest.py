"""Fixtures of the contract suite (HTTP surface, no store, no broker needed).

The contract suite drives the real ASGI application, but with in-memory ports: it
asserts the HTTP contract of contracts.md §5, the error envelope of §1 and the event
envelope of §8. Persistence, transactionality and durability are proven by the
integration suite against the real store, the real inventory and the real broker.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import (
    DEFAULT_BROKER_URL,
    DEFAULT_DATABASE_URL,
    DEFAULT_RELEASE_RETRY_INTERVAL_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    Settings,
)
from app.main import create_app
from tests.support.fakes import FakeBroker, FakeInventory, InMemoryOrderRepository


def contract_settings(**overrides: Any) -> Settings:
    """Settings that never depend on the ambient environment of the test runner."""

    values: dict[str, Any] = {
        "database_url": DEFAULT_DATABASE_URL,
        "broker_url": DEFAULT_BROKER_URL,
        "inventory_base_url": "http://inventory.invalid:8000",
        "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "release_retry_interval_seconds": DEFAULT_RELEASE_RETRY_INTERVAL_SECONDS,
        "start_workers": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def repository() -> InMemoryOrderRepository:
    return InMemoryOrderRepository()


@pytest.fixture()
def inventory() -> FakeInventory:
    return FakeInventory()


@pytest.fixture()
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture()
def store_probe() -> dict[str, bool]:
    return {"reachable": True}


@pytest.fixture()
def app(repository: InMemoryOrderRepository, inventory: FakeInventory, broker: FakeBroker, store_probe):
    return create_app(
        settings=contract_settings(),
        order_repository=repository,
        inventory=inventory,
        outbox_store=repository.outbox,
        broker=broker,
        store_probe=lambda: store_probe["reachable"],
        start_workers=False,
    )


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def route_surface(app) -> list[tuple[str, str]]:
    """Every ``(method, path)`` the application exposes (including included routers)."""

    found: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                found.update(
                    (method, str(path)) for method in methods if method not in {"HEAD", "OPTIONS"}
                )
            inner = getattr(route, "routes", None)
            if inner is None:
                original = getattr(route, "original_router", None)
                inner = getattr(original, "routes", None) if original is not None else None
            if inner:
                walk(inner)

    walk(app.routes)
    return sorted(found)


__all__ = ["contract_settings", "route_surface"]
