"""Shared fixtures of the unit and contract suites.

Both suites exercise the real HTTP application (real routers, real error
envelopes, real use cases) against in-process doubles of the two ports, so they
need no database and stay fast. The integration suite wires the same application
to the real PostgreSQL adapters instead.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.support.fakes import FakeCatalogRepository, FakeInventoryStore, FakeStockRepository


@pytest.fixture()
def store() -> FakeInventoryStore:
    """A store holding exactly the deterministic seed of contracts.md §10."""

    return FakeInventoryStore.seeded()


@pytest.fixture()
def catalog(store: FakeInventoryStore) -> FakeCatalogRepository:
    return FakeCatalogRepository(list(store.products.values()))


@pytest.fixture()
def stock(store: FakeInventoryStore) -> FakeStockRepository:
    return FakeStockRepository(store)


@pytest.fixture()
def app(store: FakeInventoryStore, catalog: FakeCatalogRepository, stock: FakeStockRepository):
    """The HTTP application with both ports backed by the doubles."""

    return create_app(catalog_repository=catalog, stock_repository=stock)


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
