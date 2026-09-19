"""Route registry: public gateway route -> owning service and upstream path.

The registry is the single source of truth for what the gateway exposes
(contracts.md §2), which service owns each route, and whether a route is a
state-changing call that must never be retried (contracts.md §4/§5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from app.config import Settings


class Service(StrEnum):
    """Upstream services consumed by the gateway."""

    ORDERS = "orders"
    INVENTORY = "inventory"


SERVICE_HEALTH_PATHS: Mapping[Service, str] = {
    Service.ORDERS: "/health",
    Service.INVENTORY: "/health",
}


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    """A single public route and the upstream call it maps to."""

    method: str
    public_path: str
    upstream_path: str
    service: Service
    idempotent: bool
    administrative: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.public_path)


#: Every business route of contracts.md §2, mapped to its owning service.
PUBLIC_ROUTES: tuple[RouteDefinition, ...] = (
    RouteDefinition("GET", "/api/products", "/products", Service.INVENTORY, idempotent=True),
    RouteDefinition("GET", "/api/products/{productId}", "/products/{productId}", Service.INVENTORY, idempotent=True),
    RouteDefinition("POST", "/api/orders", "/orders", Service.ORDERS, idempotent=False),
    RouteDefinition("GET", "/api/orders", "/orders", Service.ORDERS, idempotent=True),
    RouteDefinition("GET", "/api/orders/{orderId}", "/orders/{orderId}", Service.ORDERS, idempotent=True),
    RouteDefinition(
        "POST",
        "/api/orders/{orderId}/cancel",
        "/orders/{orderId}/cancel",
        Service.ORDERS,
        idempotent=False,
    ),
    RouteDefinition(
        "POST",
        "/api/orders/{orderId}/ship",
        "/orders/{orderId}/ship",
        Service.ORDERS,
        idempotent=False,
        administrative=True,
    ),
)

PUBLIC_ROUTE_INDEX: Mapping[tuple[str, str], RouteDefinition] = {route.key: route for route in PUBLIC_ROUTES}


class RouteRegistry:
    """Read-only lookup of the public routes this gateway exposes."""

    def __init__(self, routes: tuple[RouteDefinition, ...] = PUBLIC_ROUTES) -> None:
        self._routes = tuple(routes)
        self._index: dict[tuple[str, str], RouteDefinition] = {route.key: route for route in self._routes}

    @property
    def routes(self) -> tuple[RouteDefinition, ...]:
        return self._routes

    def get_route(self, method: str, public_path: str) -> RouteDefinition | None:
        return self._index.get((method.upper(), public_path))

    def require_route(self, method: str, public_path: str) -> RouteDefinition:
        route = self.get_route(method, public_path)
        if route is None:
            raise KeyError(f"no gateway route registered for {method.upper()} {public_path}")
        return route

    def build_upstream_path(self, route: RouteDefinition, path_params: Mapping[str, Any] | None = None) -> str:
        """Resolve ``{placeholders}`` of the upstream path with URL-encoded values."""

        path = route.upstream_path
        for name, value in (path_params or {}).items():
            placeholder = "{" + name + "}"
            if placeholder not in path:
                raise KeyError(f"{placeholder} is not a path parameter of {route.upstream_path}")
            path = path.replace(placeholder, quote(str(value), safe=""))
        if "{" in path or "}" in path:
            raise ValueError(f"unresolved path parameter in {path!r} for route {route.public_path}")
        return path


def base_url_for(service: Service, settings: Settings) -> str:
    """Return the configured base url of ``service`` without a trailing slash."""

    if service is Service.ORDERS:
        return settings.orders_base_url.rstrip("/")
    if service is Service.INVENTORY:
        return settings.inventory_base_url.rstrip("/")
    raise ValueError(f"unknown upstream service: {service!r}")
