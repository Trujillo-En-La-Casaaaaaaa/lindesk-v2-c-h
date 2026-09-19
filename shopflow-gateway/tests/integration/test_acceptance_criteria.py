"""The nine acceptance criteria of the handoff, end to end over HTTP.

Every test drives the real gateway process over a real socket. The upstreams are
the contract-faithful services of ``tests/support/upstreams`` by default, or the
real ``shopflow-orders``/``shopflow-inventory`` when the suite runs in live mode
(``SHOPFLOW_GATEWAY_LIVE_UPSTREAMS=1`` with ``ORDERS_BASE_URL`` and
``INVENTORY_BASE_URL``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx

from app.config import Settings
from tests.support.assertions import (
    assert_error_envelope,
    assert_json_response,
    available_quantity,
    create_order,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_HEADERS = {"X-ShopFlow-Role": "admin"}


def test_ac1_catalog_is_proxied_with_a_correlation_id(client: httpx.Client, direct_inventory: httpx.Client) -> None:
    """AC 1: GET /api/products returns the inventory list plus a correlation id."""

    response = client.get("/api/products")

    assert response.status_code == 200
    assert response.json() == direct_inventory.get("/products").json()
    assert response.headers.get("X-Correlation-Id")


def test_ac2_order_creation_reaches_orders_and_returns_its_view(
    client: httpx.Client,
    direct_orders: httpx.Client,
    direct_inventory: httpx.Client,
    evidence: object,
) -> None:
    """AC 2: a valid POST /api/orders reaches shopflow-orders and returns its 201 view."""

    stock_before = available_quantity(direct_inventory, "p-1001")

    response = create_order(client, "ac2-customer", [{"productId": "p-1001", "quantity": 2}])

    order = assert_json_response(response, status=201)
    assert order == direct_orders.get(f"/orders/{order['orderId']}").json()
    assert order["status"] == "CONFIRMED"
    assert order["customerId"] == "ac2-customer"
    assert available_quantity(direct_inventory, "p-1001") == stock_before - 2
    assert evidence.gateway_calls("orders", "POST", "/orders") == 1  # type: ignore[attr-defined]
    evidence.assert_upstream_requests("orders", "POST", "/orders", 1)  # type: ignore[attr-defined]


def test_ac3_negative_quantity_is_rejected_without_an_upstream_call(
    client: httpx.Client,
    evidence: object,
    direct_orders: httpx.Client,
) -> None:
    """AC 3: a negative quantity is a 400 and no upstream call is made."""

    response = client.post(
        "/api/orders",
        json={"customerId": "ac3-customer", "lines": [{"productId": "p-1001", "quantity": -1}]},
    )

    assert_error_envelope(response, status=400, code="validation_error")
    evidence.assert_no_upstream_call(  # type: ignore[attr-defined]
        "orders",
        method="POST",
        path="/orders",
        state_check=lambda: _assert_no_orders(direct_orders, "ac3-customer"),
    )


def _assert_no_orders(direct_orders: httpx.Client, customer_id: str) -> None:
    listing = direct_orders.get("/orders", params={"customerId": customer_id})
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 0, "no order may be created for a rejected request"


def test_ac4_zero_stock_returns_insufficient_stock_with_the_correlation_id(
    client: httpx.Client,
    direct_inventory: httpx.Client,
) -> None:
    """AC 4: a zero-stock product is a 409 insufficient_stock with the correlation id."""

    correlation_id = "ac4-correlation"

    response = create_order(
        client,
        "ac4-customer",
        [{"productId": "p-1005", "quantity": 1}],
        headers={"X-Correlation-Id": correlation_id},
    )

    error = assert_error_envelope(response, status=409, code="insufficient_stock", correlation_id=correlation_id)
    assert error["correlationId"] == correlation_id
    assert available_quantity(direct_inventory, "p-1005") == 0


def test_ac5_cancel_reason_is_validated_then_applied(
    client: httpx.Client,
    evidence: object,
    direct_orders: httpx.Client,
) -> None:
    """AC 5: an empty reason is a 400 invalid_reason; a valid reason cancels the order."""

    order_id = create_order(client, "ac5-customer", [{"productId": "p-1002", "quantity": 1}]).json()["orderId"]
    evidence.gateway_log.clear()  # type: ignore[attr-defined]

    rejected = client.post(f"/api/orders/{order_id}/cancel", json={"reason": ""})

    assert_error_envelope(rejected, status=400, code="invalid_reason")
    evidence.assert_no_upstream_call(  # type: ignore[attr-defined]
        "orders",
        method="POST",
        path=f"/orders/{order_id}/cancel",
        state_check=lambda: _assert_order_status(direct_orders, order_id, "CONFIRMED"),
    )

    cancelled = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "customer changed their mind"})

    order = assert_json_response(cancelled, status=200)
    assert order["status"] == "CANCELLED"
    assert order["cancellationReason"] == "customer changed their mind"
    assert order["cancelledAt"]
    assert order == direct_orders.get(f"/orders/{order_id}").json()


def _assert_order_status(direct_orders: httpx.Client, order_id: str, expected: str) -> None:
    response = direct_orders.get(f"/orders/{order_id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected


def test_ac6_admin_guard_blocks_the_ship_route_without_touching_the_backend(
    client: httpx.Client,
    evidence: object,
    direct_orders: httpx.Client,
) -> None:
    """AC 6: without the admin header the ship route is 403 and calls no backend."""

    order_id = create_order(client, "ac6-customer", [{"productId": "p-1004", "quantity": 3}]).json()["orderId"]
    evidence.gateway_log.clear()  # type: ignore[attr-defined]

    forbidden = client.post(f"/api/orders/{order_id}/ship")

    assert_error_envelope(forbidden, status=403, code="forbidden")
    evidence.assert_no_upstream_call(  # type: ignore[attr-defined]
        "orders",
        method="POST",
        path=f"/orders/{order_id}/ship",
        state_check=lambda: _assert_order_status(direct_orders, order_id, "CONFIRMED"),
    )

    shipped = client.post(f"/api/orders/{order_id}/ship", headers=ADMIN_HEADERS)

    order = assert_json_response(shipped, status=200)
    assert order["status"] == "SHIPPED"
    assert order["shippedAt"]
    assert order == direct_orders.get(f"/orders/{order_id}").json()


def test_ac7_cancelling_a_shipped_order_passes_the_conflict_through(
    client: httpx.Client,
    direct_orders: httpx.Client,
) -> None:
    """AC 7: cancelling a SHIPPED order is a 409 order_shipped passed through unchanged."""

    order_id = create_order(client, "ac7-customer").json()["orderId"]
    assert client.post(f"/api/orders/{order_id}/ship", headers=ADMIN_HEADERS).status_code == 200

    correlation_id = "ac7-correlation"
    response = client.post(
        f"/api/orders/{order_id}/cancel",
        json={"reason": "too late"},
        headers={"X-Correlation-Id": correlation_id},
    )

    assert_error_envelope(response, status=409, code="order_shipped", correlation_id=correlation_id)
    _assert_order_status(direct_orders, order_id, "SHIPPED")


def test_ac8_ready_reflects_backend_availability(
    client: httpx.Client,
    gateway_factory: object,
    stopped_upstream_port: int,
) -> None:
    """AC 8: /ready is 200 when both services run and 503 when one is stopped."""

    healthy = assert_json_response(client.get("/ready"), status=200)
    assert healthy == {"status": "ready", "upstreams": {"orders": "ok", "inventory": "ok"}}

    stopped_gateway = gateway_factory(inventory_base_url=f"http://127.0.0.1:{stopped_upstream_port}")  # type: ignore[operator]
    with httpx.Client(base_url=stopped_gateway.base_url, timeout=15.0) as broken:
        degraded = broken.get("/ready")

    error = assert_error_envelope(degraded, status=503, code="upstream_unavailable")
    assert error["details"]["upstreams"]["inventory"] == "unavailable"


def test_ac9_gateway_has_no_store_dependency_and_no_durable_state(client: httpx.Client) -> None:
    """AC 9: no database client dependency and no durable state."""

    database_clients = ("psycopg", "sqlalchemy", "asyncpg", "redis", "pymongo", "mysql", "alembic")

    for name in ("DATABASE_URL", "BROKER_URL"):
        assert name not in os.environ, f"the gateway must not need {name}"

    for filename in ("requirements.txt", "requirements-dev.txt"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8").lower()
        assert [client_name for client_name in database_clients if client_name in text] == []

    for module in (REPO_ROOT / "app").rglob("*.py"):
        text = module.read_text(encoding="utf-8").lower()
        for client_name in database_clients:
            assert not re.search(rf"^\s*(import|from)\s+{re.escape(client_name)}\b", text, flags=re.MULTILINE), module

    keys = {key.upper() for key in Settings().model_dump()}
    assert not [key for key in keys if "DATABASE" in key or "BROKER" in key or "STORE" in key]

    # The running gateway serves traffic with no store configured at all.
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/products").status_code == 200
