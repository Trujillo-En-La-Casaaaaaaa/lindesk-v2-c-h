"""Contract tests of the exposed surface (contracts.md §6).

contracts.md §6 defines exactly three routes for this service: ``GET /health``,
``GET /ready`` and ``GET /notifications?orderId=``. Anything else this service
exposed would be an unintended public interface, and the API documentation routes
are disabled for the same reason.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

DOCUMENTED_ROUTES = {("GET", "/health"), ("GET", "/ready"), ("GET", "/notifications")}

#: Nested router containers of the framework (an included router keeps the routes
#: of the included :class:`APIRouter`).
NESTED_ATTRIBUTES = ("routes", "original_router", "router", "app")


def route_paths(routes: Any, seen: set[int] | None = None) -> set[tuple[str, str]]:
    """Collect (method, path) pairs, descending into included routers."""

    visited = seen if seen is not None else set()
    found: set[tuple[str, str]] = set()
    for route in routes:
        if id(route) in visited:
            continue
        visited.add(id(route))
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            found.update((method, path) for method in methods)
        for attribute in NESTED_ATTRIBUTES:
            nested = getattr(route, attribute, None)
            nested_routes = getattr(nested, "routes", None)
            if nested_routes:
                found |= route_paths(nested_routes, visited)
    return found


def test_the_service_exposes_exactly_the_documented_routes(ready_client: TestClient) -> None:
    application = ready_client.app

    assert route_paths(application.routes) == DOCUMENTED_ROUTES


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_api_documentation_is_not_exposed(ready_client: TestClient, path: str) -> None:
    response = ready_client.get(path)

    assert response.status_code == 404


def test_the_notifications_route_requires_the_order_id_parameter(ready_client: TestClient) -> None:
    assert ready_client.get("/notifications").status_code == 400
    assert ready_client.get("/notifications", params={"orderId": "order-1"}).status_code == 200
