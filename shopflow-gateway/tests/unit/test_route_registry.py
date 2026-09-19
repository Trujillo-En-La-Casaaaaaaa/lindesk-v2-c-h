"""The route registry is the single source of truth for the public surface."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import create_app
from app.routing.route_registry import (
    PUBLIC_ROUTES,
    SERVICE_HEALTH_PATHS,
    RouteRegistry,
    Service,
    base_url_for,
)

#: contracts.md §2, method by public path.
EXPECTED_SURFACE: dict[tuple[str, str], tuple[Service, str, bool, bool]] = {
    ("GET", "/api/products"): (Service.INVENTORY, "/products", True, False),
    ("GET", "/api/products/{productId}"): (Service.INVENTORY, "/products/{productId}", True, False),
    ("POST", "/api/orders"): (Service.ORDERS, "/orders", False, False),
    ("GET", "/api/orders"): (Service.ORDERS, "/orders", True, False),
    ("GET", "/api/orders/{orderId}"): (Service.ORDERS, "/orders/{orderId}", True, False),
    ("POST", "/api/orders/{orderId}/cancel"): (Service.ORDERS, "/orders/{orderId}/cancel", False, False),
    ("POST", "/api/orders/{orderId}/ship"): (Service.ORDERS, "/orders/{orderId}/ship", False, True),
}


def _collect_routes(routes: object) -> list[tuple[str, set[str]]]:
    collected: list[tuple[str, set[str]]] = []
    for route in routes:  # type: ignore[union-attr]
        inner = getattr(route, "original_router", None)
        inner_routes = getattr(inner, "routes", None)
        if inner_routes:
            collected.extend(_collect_routes(inner_routes))
            continue
        path = getattr(route, "path", None)
        if path is not None:
            collected.append((path, set(getattr(route, "methods", None) or ())))
    return collected


def test_registry_contains_exactly_the_public_routes() -> None:
    registry = RouteRegistry()

    assert {route.key for route in registry.routes} == set(EXPECTED_SURFACE)
    for route in registry.routes:
        service, upstream_path, idempotent, administrative = EXPECTED_SURFACE[route.key]
        assert route.service is service, route
        assert route.upstream_path == upstream_path, route
        assert route.idempotent is idempotent, route
        assert route.administrative is administrative, route


def test_only_the_ship_route_is_administrative() -> None:
    administrative = {route.key for route in PUBLIC_ROUTES if route.administrative}

    assert administrative == {("POST", "/api/orders/{orderId}/ship")}


def test_state_changing_posts_are_not_idempotent() -> None:
    assert [route.key for route in PUBLIC_ROUTES if route.method == "POST" and route.idempotent] == []


def test_both_services_expose_a_health_path() -> None:
    assert SERVICE_HEALTH_PATHS == {Service.ORDERS: "/health", Service.INVENTORY: "/health"}


def test_build_upstream_path_resolves_and_encodes_parameters() -> None:
    registry = RouteRegistry()
    route = registry.require_route("GET", "/api/orders/{orderId}")

    assert registry.build_upstream_path(route, {"orderId": "0f9c1f8c-1111-2222-3333-444455556666"}) == (
        "/orders/0f9c1f8c-1111-2222-3333-444455556666"
    )

    product_route = registry.require_route("GET", "/api/products/{productId}")
    assert registry.build_upstream_path(product_route, {"productId": "p-1001"}) == "/products/p-1001"
    assert registry.build_upstream_path(product_route, {"productId": "a/b c"}) == "/products/a%2Fb%20c"


def test_build_upstream_path_rejects_missing_and_unknown_parameters() -> None:
    registry = RouteRegistry()
    route = registry.require_route("GET", "/api/orders/{orderId}")

    with pytest.raises(ValueError):
        registry.build_upstream_path(route, {})
    with pytest.raises(KeyError):
        registry.build_upstream_path(route, {"unknown": "x"})


def test_unknown_route_is_not_registered() -> None:
    registry = RouteRegistry()

    assert registry.get_route("GET", "/api/admin") is None
    assert registry.get_route("DELETE", "/api/orders/1") is None
    with pytest.raises(KeyError):
        registry.require_route("POST", "/api/products")


def test_base_url_lookup_uses_the_settings() -> None:
    settings = Settings(orders_base_url="http://orders:9000/", inventory_base_url="http://inventory:9001")

    assert base_url_for(Service.ORDERS, settings) == "http://orders:9000"
    assert base_url_for(Service.INVENTORY, settings) == "http://inventory:9001"


def test_fastapi_app_exposes_exactly_the_public_surface() -> None:
    app = create_app(settings=Settings())
    surface: set[tuple[str, str]] = set()
    for path, methods in _collect_routes(app.routes):
        for method in methods:
            surface.add((method, path))

    # contracts.md §2 business routes plus the two operational endpoints.
    expected = set(EXPECTED_SURFACE) | {("GET", "/health"), ("GET", "/ready")}
    assert surface == expected
    assert {path for path, _methods in _collect_routes(app.routes)} == (
        {"/health", "/ready"} | {path for _method, path in EXPECTED_SURFACE}
    )
