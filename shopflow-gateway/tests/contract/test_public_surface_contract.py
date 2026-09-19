"""The gateway exposes exactly the public API of contracts.md §2, black box."""

from __future__ import annotations

import httpx
import pytest

from tests.support.assertions import assert_error_envelope, assert_json_response, create_order


def _ship_ready_order(client: httpx.Client) -> str:
    created = create_order(client, customer_id="surface-customer")
    order_id = created.json()["orderId"]
    shipped = client.post(f"/api/orders/{order_id}/ship", headers={"X-ShopFlow-Role": "admin"})
    assert shipped.status_code == 200, shipped.text
    return order_id


def test_every_documented_route_answers_with_its_success_status(client: httpx.Client) -> None:
    products = assert_json_response(client.get("/api/products"), status=200)
    assert set(products) == {"items", "total"}

    product = assert_json_response(client.get("/api/products/p-1001"), status=200)
    assert product["productId"] == "p-1001"

    created = client.post(
        "/api/orders",
        json={"customerId": "surface-customer", "lines": [{"productId": "p-1002", "quantity": 1}]},
    )
    order = assert_json_response(created, status=201)
    order_id = order["orderId"]

    view = assert_json_response(client.get(f"/api/orders/{order_id}"), status=200)
    assert view["orderId"] == order_id

    listing = assert_json_response(client.get("/api/orders", params={"customerId": "surface-customer"}), status=200)
    assert listing["total"] == 1
    assert listing["items"][0]["orderId"] == order_id

    cancelled = assert_json_response(
        client.post(f"/api/orders/{order_id}/cancel", json={"reason": "changed my mind"}),
        status=200,
    )
    assert cancelled["status"] == "CANCELLED"

    shipped = _ship_ready_order(client)
    assert client.get(f"/api/orders/{shipped}").json()["status"] == "SHIPPED"

    health = assert_json_response(client.get("/health"), status=200)
    assert health == {"status": "ok"}

    ready = assert_json_response(client.get("/ready"), status=200)
    assert ready == {"status": "ready", "upstreams": {"orders": "ok", "inventory": "ok"}}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/api/admin", "/products", "/orders"])
def test_only_the_public_surface_is_exposed(client: httpx.Client, path: str) -> None:
    response = client.get(path)

    assert_error_envelope(response, status=404, code="not_found")


def test_unsupported_methods_are_normalized(client: httpx.Client) -> None:
    response = client.put("/api/products")

    assert_error_envelope(response, status=405, code="not_found")


def test_every_response_carries_a_correlation_id(client: httpx.Client) -> None:
    responses = [
        client.get("/health"),
        client.get("/ready"),
        client.get("/api/products"),
        client.get("/api/products/does-not-exist"),
        client.post("/api/orders", json={"customerId": "", "lines": []}),
        client.get("/api/orders"),
        client.get("/api/orders/missing"),
        client.post("/api/orders/missing/cancel", json={"reason": ""}),
        client.post("/api/orders/missing/ship"),
    ]

    for response in responses:
        assert response.headers.get("X-Correlation-Id"), response.request.url


def test_client_correlation_id_is_echoed_and_forwarded(client: httpx.Client, upstream_stack: object) -> None:
    correlation_id = "contract-correlation-42"

    response = client.get("/api/products", headers={"X-Correlation-Id": correlation_id})

    assert response.status_code == 200
    assert response.headers["X-Correlation-Id"] == correlation_id
    log = upstream_stack.log_for("inventory")  # type: ignore[attr-defined]
    assert log is not None
    forwarded = [record.correlation_id for record in log.matching("GET", "/products")]
    assert forwarded and all(value == correlation_id for value in forwarded)


def test_generated_correlation_id_is_forwarded(client: httpx.Client, upstream_stack: object) -> None:
    response = client.get("/api/products")

    correlation_id = response.headers["X-Correlation-Id"]
    log = upstream_stack.log_for("inventory")  # type: ignore[attr-defined]
    assert log is not None
    forwarded = [record.correlation_id for record in log.matching("GET", "/products")]
    assert forwarded and forwarded[-1] == correlation_id


def test_unknown_order_error_is_a_normalized_envelope(client: httpx.Client) -> None:
    response = client.get("/api/orders/00000000-0000-0000-0000-000000000000")

    assert_error_envelope(response, status=404, code="not_found")
