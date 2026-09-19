"""Integration tests of cancellation (HANDOFF #3, acceptance criteria 3-7)."""

from __future__ import annotations

import threading

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.domain.cancellation import MAX_REASON_LENGTH
from tests.integration.conftest import start_orders_server
from tests.support.db import count_orders, outbox_rows
from tests.support.db import order_row as read_order_row
from tests.support.inventory_stub import MODE_RESERVE_DOWN, MODE_RESERVE_RACE_LOST

CUSTOMER = "c-integration-cancel"
TEN_CHAR_REASON = "not wanted"


def place_order(client: TestClient, quantity: int = 2, product_id: str = "p-1001") -> dict:
    response = client.post(
        "/orders",
        json={"customerId": CUSTOMER, "lines": [{"productId": product_id, "quantity": quantity}]},
        headers={"X-Correlation-Id": "corr-create"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def cancel(client: TestClient, order_id: str, reason: str = TEN_CHAR_REASON, *, correlation: str = "corr-cancel"):
    return client.post(f"/orders/{order_id}/cancel", json={"reason": reason}, headers={"X-Correlation-Id": correlation})


def test_acceptance_3_cancelling_restores_the_stock_exactly_once(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    from tests.integration.conftest import inventory_stock

    before = inventory_stock(inventory_url, "p-1001")
    order = place_order(client, 2)
    assert inventory_stock(inventory_url, "p-1001") == before - 2

    response = cancel(client, order["orderId"])

    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancelledAt"] is not None
    assert cancelled["cancellationReason"] == TEN_CHAR_REASON
    assert inventory_stock(inventory_url, "p-1001") == before  # restored to the pre-order value

    row = read_order_row(session_factory, order["orderId"])
    assert row is not None
    assert row["status"] == "CANCELLED"
    assert row["cancelled_at"] is not None
    assert row["cancellation_reason"] == TEN_CHAR_REASON
    assert row["inventory_release_state"] == "RELEASED"

    events = outbox_rows(session_factory, order_id=order["orderId"])
    assert [event["event_type"] for event in events] == ["order.confirmed", "order.cancelled"]
    assert events[1]["payload"]["reason"] == TEN_CHAR_REASON
    assert events[1]["payload"]["lines"] == [{"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}]
    assert events[1]["payload"]["occurredAt"] == cancelled["cancelledAt"]


def test_acceptance_4_a_repeated_cancellation_changes_nothing(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    from tests.integration.conftest import inventory_stock

    before = inventory_stock(inventory_url, "p-1001")
    order = place_order(client, 1)
    first = cancel(client, order["orderId"]).json()
    stock_after_first = inventory_stock(inventory_url, "p-1001")
    assert stock_after_first == before

    second = cancel(client, order["orderId"], reason="a completely different reason")

    assert second.status_code == 200
    repeated = second.json()
    assert repeated["cancelledAt"] == first["cancelledAt"]
    assert repeated["cancellationReason"] == TEN_CHAR_REASON
    assert inventory_stock(inventory_url, "p-1001") == stock_after_first  # no second restoration

    events = outbox_rows(session_factory, order_id=order["orderId"])
    assert len([event for event in events if event["event_type"] == "order.cancelled"]) == 1
    row = read_order_row(session_factory, order["orderId"])
    assert row is not None and row["cancelled_at"] is not None


def test_acceptance_5_an_empty_reason_changes_nothing(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    from tests.integration.conftest import inventory_stock

    before = inventory_stock(inventory_url, "p-1001")
    order = place_order(client, 1)

    response = cancel(client, order["orderId"], reason="   ")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reason"
    row = read_order_row(session_factory, order["orderId"])
    assert row is not None
    assert row["status"] == "CONFIRMED"
    assert row["cancelled_at"] is None
    assert row["cancellation_reason"] is None
    assert row["inventory_release_state"] == "NONE"
    assert inventory_stock(inventory_url, "p-1001") == before - 1
    assert len(outbox_rows(session_factory, order_id=order["orderId"])) == 1  # only the confirmation


def test_acceptance_6_a_201_character_reason_changes_nothing(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    from tests.integration.conftest import inventory_stock

    order = place_order(client, 1)
    stock_before_cancel = inventory_stock(inventory_url, "p-1001")

    response = cancel(client, order["orderId"], reason="a" * (MAX_REASON_LENGTH + 1))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reason"
    row = read_order_row(session_factory, order["orderId"])
    assert row is not None and row["status"] == "CONFIRMED"
    assert inventory_stock(inventory_url, "p-1001") == stock_before_cancel


def test_acceptance_7_a_200_character_reason_succeeds(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    order = place_order(client, 1)
    reason = "a" * MAX_REASON_LENGTH

    response = cancel(client, order["orderId"], reason=reason)

    assert response.status_code == 200
    assert response.json()["cancellationReason"] == reason
    row = read_order_row(session_factory, order["orderId"])
    assert row is not None
    assert row["cancellation_reason"] == reason
    assert len(str(row["cancellation_reason"])) == MAX_REASON_LENGTH


def test_a_reason_is_trimmed_before_it_is_stored(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    order = place_order(client, 1)
    response = cancel(client, order["orderId"], reason="   customer changed their mind   ")

    assert response.status_code == 200
    assert response.json()["cancellationReason"] == "customer changed their mind"
    row = read_order_row(session_factory, order["orderId"])
    assert row is not None and row["cancellation_reason"] == "customer changed their mind"


def test_cancelling_an_unknown_order_is_not_found(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    response = cancel(client, "11111111-2222-3333-4444-555555555555")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert count_orders(session_factory) == 0


def test_concurrent_duplicate_cancellations_release_the_stock_once(
    app, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    """Two simultaneous cancels: exactly one release, one event, one stored reason."""

    from tests.integration.conftest import inventory_stock

    server = start_orders_server(app)
    before = inventory_stock(inventory_url, "p-1001")
    responses: list[tuple[int, dict]] = []
    lock = threading.Lock()

    try:
        with httpx.Client(base_url=server.base_url, timeout=30.0) as http:
            created = http.post(
                "/orders",
                json={"customerId": "c-concurrent-cancel", "lines": [{"productId": "p-1001", "quantity": 2}]},
            )
            assert created.status_code == 201
            order_id = created.json()["orderId"]

            def do_cancel() -> None:
                response = http.post(f"/orders/{order_id}/cancel", json={"reason": TEN_CHAR_REASON})
                with lock:
                    responses.append((response.status_code, response.json()))

            threads = [threading.Thread(target=do_cancel) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
    finally:
        server.stop()

    assert [status for status, _body in responses] == [200, 200]
    bodies = [body for _status, body in responses]
    assert {body["cancelledAt"] for body in bodies} == {bodies[0]["cancelledAt"]}  # one winner
    assert {body["cancellationReason"] for body in bodies} == {TEN_CHAR_REASON}
    assert inventory_stock(inventory_url, "p-1001") == before  # restored exactly once

    row = read_order_row(session_factory, order_id)
    assert row is not None and row["status"] == "CANCELLED"
    cancelled_events = [
        event for event in outbox_rows(session_factory, order_id=order_id) if event["event_type"] == "order.cancelled"
    ]
    assert len(cancelled_events) == 1


def test_a_created_order_is_not_cancellable(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    stub = stub_inventory()
    stub.state.set_mode(MODE_RESERVE_DOWN)
    client = stub.client()

    created = client.post(
        "/orders",
        json={"customerId": "c-created", "lines": [{"productId": "p-1001", "quantity": 1}]},
    )
    assert created.status_code == 503

    rows = [row for row in _rows(session_factory) if row["status"] == "CREATED"]
    assert len(rows) == 1

    response = client.post(f"/orders/{rows[0]['id']}/cancel", json={"reason": TEN_CHAR_REASON})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "order_not_cancellable"
    assert body["error"]["details"]["status"] == "CREATED"
    assert list(stub.state.calls_of("release")) == []


def test_a_failed_order_is_not_cancellable(app_factory, stub_inventory, session_factory: sessionmaker[Session]) -> None:
    stub = stub_inventory()
    stub.state.set_mode(MODE_RESERVE_RACE_LOST)
    client = stub.client()

    created = client.post(
        "/orders",
        json={"customerId": "c-failed", "lines": [{"productId": "p-1001", "quantity": 1}]},
    )
    assert created.status_code == 409

    failed = [row for row in _rows(session_factory) if row["status"] == "FAILED"]
    assert len(failed) == 1

    response = client.post(f"/orders/{failed[0]['id']}/cancel", json={"reason": TEN_CHAR_REASON})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_not_cancellable"
    assert len(outbox_rows(session_factory)) == 0


def _rows(session_factory: sessionmaker[Session]) -> list[dict]:
    from tests.support.db import order_rows

    return order_rows(session_factory)
