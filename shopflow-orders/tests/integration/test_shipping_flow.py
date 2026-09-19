"""Integration tests of the administrative ship action (HANDOFF #4, criteria 8 and 9)."""

from __future__ import annotations

import threading

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import inventory_stock, start_orders_server
from tests.support.db import order_row as read_order_row
from tests.support.db import outbox_rows
from tests.support.inventory_stub import MODE_RESERVE_DOWN

CUSTOMER = "c-integration-ship"


def place_order(client: TestClient, quantity: int = 2) -> dict:
    response = client.post(
        "/orders",
        json={"customerId": CUSTOMER, "lines": [{"productId": "p-1004", "quantity": quantity}]},
        headers={"X-Correlation-Id": "corr-create"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_acceptance_8_shipping_sets_the_state_and_blocks_cancellation(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    stock_after_order = None
    order = place_order(client, 2)
    stock_after_order = inventory_stock(inventory_url, "p-1004")

    response = client.post(f"/orders/{order['orderId']}/ship", headers={"X-Correlation-Id": "corr-ship"})

    assert response.status_code == 200, response.text
    shipped = response.json()
    assert shipped["status"] == "SHIPPED"
    assert shipped["shippedAt"] is not None
    assert shipped["shippedAt"].endswith("Z")
    assert shipped["confirmedAt"] is not None
    assert shipped["cancelledAt"] is None

    row = read_order_row(session_factory, order["orderId"])
    assert row is not None
    assert row["status"] == "SHIPPED"
    assert row["shipped_at"] is not None

    # the stock stays reserved: shipping does not restore anything
    assert inventory_stock(inventory_url, "p-1004") == stock_after_order

    cancel = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "too late, already shipped"})
    assert cancel.status_code == 409
    body = cancel.json()
    assert body["error"]["code"] == "order_shipped"
    assert body["error"]["details"]["status"] == "SHIPPED"

    after = client.get(f"/orders/{order['orderId']}").json()
    assert after["status"] == "SHIPPED"
    assert after["cancelledAt"] is None
    assert after["cancellationReason"] is None
    assert inventory_stock(inventory_url, "p-1004") == stock_after_order
    assert [event["event_type"] for event in outbox_rows(session_factory, order_id=order["orderId"])] == [
        "order.confirmed"
    ]


def test_acceptance_9_shipping_a_cancelled_order_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    order = place_order(client, 1)
    cancelled = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})
    assert cancelled.status_code == 200

    response = client.post(f"/orders/{order['orderId']}/ship")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "invalid_status_transition"
    assert body["error"]["details"]["status"] == "CANCELLED"
    assert body["error"]["details"]["targetStatus"] == "SHIPPED"

    row = read_order_row(session_factory, order["orderId"])
    assert row is not None
    assert row["status"] == "CANCELLED"
    assert row["shipped_at"] is None
    assert [event["event_type"] for event in outbox_rows(session_factory, order_id=order["orderId"])] == [
        "order.confirmed",
        "order.cancelled",
    ]


def test_shipping_an_unknown_order_is_not_found(client: TestClient) -> None:
    response = client.post("/orders/11111111-2222-3333-4444-555555555555/ship")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_created_order_cannot_ship(app_factory, stub_inventory, session_factory: sessionmaker[Session]) -> None:
    stub = stub_inventory()
    stub.state.set_mode(MODE_RESERVE_DOWN)
    client = stub.client()

    created = client.post(
        "/orders",
        json={"customerId": "c-created-ship", "lines": [{"productId": "p-1001", "quantity": 1}]},
    )
    assert created.status_code == 503

    from tests.support.db import order_rows

    order_id = next(row["id"] for row in order_rows(session_factory) if row["status"] == "CREATED")
    response = client.post(f"/orders/{order_id}/ship")

    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == "CREATED"


def test_concurrent_ships_let_exactly_one_succeed(app, session_factory: sessionmaker[Session]) -> None:
    server = start_orders_server(app)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    try:
        with httpx.Client(base_url=server.base_url, timeout=30.0) as http:
            created = http.post(
                "/orders",
                json={"customerId": "c-concurrent-ship", "lines": [{"productId": "p-1004", "quantity": 1}]},
            )
            assert created.status_code == 201
            order_id = created.json()["orderId"]

            def do_ship() -> None:
                response = http.post(f"/orders/{order_id}/ship")
                with lock:
                    results.append((response.status_code, response.json()))

            threads = [threading.Thread(target=do_ship) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
    finally:
        server.stop()

    assert sorted(status for status, _body in results) == [200, 409]
    bodies = [body for _status, body in results]
    winner = next(body for body in bodies if body["status"] == "SHIPPED")
    loser = next(body for body in bodies if "error" in body)
    assert loser["error"]["code"] == "invalid_status_transition"
    assert winner["shippedAt"] is not None

    row = read_order_row(session_factory, order_id)
    assert row is not None and row["status"] == "SHIPPED"
