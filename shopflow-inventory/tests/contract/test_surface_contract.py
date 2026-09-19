"""Contract tests: the error envelope and the exact public surface.

contracts.md §1 fixes the envelope shape and §4 fixes the routes; nothing outside
those may be exposed by this service.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app

EXPECTED_ENDPOINTS = {
    ("GET", "/products"),
    ("GET", "/products/{productId}"),
    ("POST", "/inventory/availability"),
    ("POST", "/inventory/reservations"),
    ("POST", "/inventory/releases"),
    ("GET", "/health"),
    ("GET", "/ready"),
}


def endpoints(app) -> set[tuple[str, str]]:
    """Collect ``(method, path)`` of every route, including lazily included routers."""

    found: set[tuple[str, str]] = set()

    def walk(routes: Iterable[Any]) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if isinstance(path, str) and methods:
                found.update((method, path) for method in methods if method not in {"HEAD", "OPTIONS"})
            nested = getattr(route, "original_router", None) or getattr(route, "router", None)
            inner = getattr(nested, "routes", None) if nested is not None else getattr(route, "routes", None)
            if isinstance(inner, (list, tuple)):
                walk(inner)

    walk(app.routes)
    return found


def test_the_public_surface_is_exactly_the_contract(app) -> None:
    assert endpoints(app) == EXPECTED_ENDPOINTS


def test_the_envelope_has_the_documented_shape(client: TestClient) -> None:
    body = client.get("/products/p-9999").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "correlationId", "details"}
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)


def test_the_correlation_id_is_echoed_unchanged(client: TestClient) -> None:
    body = client.get("/products/p-9999", headers={"X-Correlation-Id": "cid-123"}).json()

    assert body["error"]["correlationId"] == "cid-123"


def test_the_correlation_id_is_empty_when_absent(client: TestClient) -> None:
    body = client.get("/products/p-9999").json()

    assert body["error"]["correlationId"] == ""


def test_an_unknown_route_returns_the_envelope(client: TestClient) -> None:
    response = client.get("/inventory/unknown")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_wrong_method_returns_the_envelope(client: TestClient) -> None:
    response = client.post("/products", json={})

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "validation_error"


def test_no_documentation_routes_are_exposed(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_the_default_app_is_the_production_composition_root() -> None:
    """``create_app`` without injected ports wires the PostgreSQL adapters."""

    from app.adapters.postgres_catalog import PostgresCatalogRepository
    from app.adapters.postgres_stock import PostgresStockRepository

    default_app = create_app()

    assert isinstance(default_app.state.catalog_repository, PostgresCatalogRepository)
    assert isinstance(default_app.state.stock_repository, PostgresStockRepository)
    assert default_app.state.engine is not None
    default_app.state.engine.dispose()
