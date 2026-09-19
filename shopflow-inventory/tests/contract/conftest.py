"""Fixtures of the contract suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.support.fakes import (
    FakeCatalogRepository,
    FakeStockRepository,
    HealthyEngine,
    UnreachableEngine,
)


@pytest.fixture()
def ready_client(catalog: FakeCatalogRepository, stock: FakeStockRepository) -> Iterator[TestClient]:
    """A client whose store answers the readiness probe."""

    app = create_app(engine=HealthyEngine(), catalog_repository=catalog, stock_repository=stock)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def unreachable_store_client(catalog: FakeCatalogRepository, stock: FakeStockRepository) -> Iterator[TestClient]:
    """A client whose store refuses to answer the readiness probe."""

    app = create_app(engine=UnreachableEngine(), catalog_repository=catalog, stock_repository=stock)
    with TestClient(app) as test_client:
        yield test_client
