"""End-to-end order flows through the gateway, including concurrency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.support.assertions import (
    assert_error_envelope,
    assert_json_response,
    available_quantity,
    create_order,
)

ADMIN_HEADERS = {"X-ShopFlow-Role": "admin"}


def test_full_order_lifecycle(client: httpx.Client, direct_inventory: httpx.Client) -> None:
    """Create, read, list, cancel and ship a real order through the gateway."""

    stock_before = available_quantity(direct_inventory, "p-1001")

    created = create_order(
        client,
        "lifecycle-customer",
        [{"productId": "p-1001", "quantity": 2}, {"productId": "p-1002", "quantity": 1}],
    )
    order = assert_json_response(created, status=201)
    order_id = order["orderId"]
    assert order["total"] == round(2 * 19.99 + 12.50, 2)
    assert {line["productId"] for line in order["lines"]} == {"p-1001", "p-1002"}
    assert available_quantity(direct_inventory, "p-1001") == stock_before - 2

    read = assert_json_response(client.get(f"/api/orders/{order_id}"), status=200)
    assert read == order

    listing = assert_json_response(client.get("/api/orders", params={"customerId": "lifecycle-customer"}), status=200)
    assert listing["total"] == 1
    assert listing["items"][0]["orderId"] == order_id

    cancelled = assert_json_response(
        client.post(f"/api/orders/{order_id}/cancel", json={"reason": "found cheaper"}),
        status=200,
    )
    assert cancelled["status"] == "CANCELLED"
    assert available_quantity(direct_inventory, "p-1001") == stock_before

    second = create_order(client, "lifecycle-customer", [{"productId": "p-1003", "quantity": 1}])
    second_order = assert_json_response(second, status=201)

    shipped = assert_json_response(
        client.post(f"/api/orders/{second_order['orderId']}/ship", headers=ADMIN_HEADERS),
        status=200,
    )
    assert shipped["status"] == "SHIPPED"

    rejected = client.post(f"/api/orders/{second_order['orderId']}/cancel", json={"reason": "changed my mind"})
    assert_error_envelope(rejected, status=409, code="order_shipped")


def test_cancellation_is_idempotent_and_releases_stock_once(
    client: httpx.Client,
    direct_inventory: httpx.Client,
) -> None:
    stock_before = available_quantity(direct_inventory, "p-1003")
    order_id = create_order(client, "idempotent-cancel", [{"productId": "p-1003", "quantity": 2}]).json()["orderId"]

    first = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "first"})
    second = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "second"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "CANCELLED"
    assert available_quantity(direct_inventory, "p-1003") == stock_before


def test_failed_requests_do_not_change_stock(client: httpx.Client, direct_inventory: httpx.Client) -> None:
    stock_before = available_quantity(direct_inventory, "p-1001")

    rejected_quantity = client.post(
        "/api/orders",
        json={"customerId": "c", "lines": [{"productId": "p-1001", "quantity": -5}]},
    )
    rejected_stock = client.post(
        "/api/orders",
        json={"customerId": "c", "lines": [{"productId": "p-1005", "quantity": 1}]},
    )

    assert rejected_quantity.status_code == 400
    assert rejected_stock.status_code == 409
    assert client.post("/api/orders/missing/ship").status_code == 403

    assert available_quantity(direct_inventory, "p-1001") == stock_before


def test_concurrent_order_creation_never_oversells(client: httpx.Client, direct_inventory: httpx.Client) -> None:
    """Concurrent creates through the gateway must not exceed available stock."""

    product_id = "p-1003"
    stock_before = available_quantity(direct_inventory, product_id)
    attempts = 12

    def place(index: int) -> httpx.Response:
        return client.post(
            "/api/orders",
            json={"customerId": f"race-customer-{index}", "lines": [{"productId": product_id, "quantity": 1}]},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(place, range(attempts)))

    created = [response for response in responses if response.status_code == 201]
    rejected = [response for response in responses if response.status_code == 409]

    assert len(created) + len(rejected) == attempts, [response.status_code for response in responses]
    assert all(response.json()["error"]["code"] == "insufficient_stock" for response in rejected)
    assert len(created) <= stock_before, "the gateway amplified concurrent orders into an oversell"
    assert len(created) == min(attempts, stock_before)
    assert available_quantity(direct_inventory, product_id) == stock_before - len(created)


def test_gateway_reports_readiness_after_traffic(client: httpx.Client) -> None:
    create_order(client, "readiness-customer")

    ready = assert_json_response(client.get("/ready"), status=200)

    assert ready["status"] == "ready"
    assert ready["upstreams"] == {"orders": "ok", "inventory": "ok"}
