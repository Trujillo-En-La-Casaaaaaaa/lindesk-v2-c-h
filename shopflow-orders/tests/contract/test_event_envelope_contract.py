"""Contract tests of the event envelope written by the lifecycle (contracts.md §8)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.events.contracts import EVENT_ORDER_CANCELLED, EVENT_ORDER_CONFIRMED
from tests.support.fakes import InMemoryOrderRepository

ENVELOPE_KEYS = [
    "eventId",
    "eventType",
    "eventVersion",
    "occurredAt",
    "correlationId",
    "orderId",
    "customerId",
    "lines",
    "reason",
]


def confirm_order(client: TestClient, *, correlation_id: str = "corr-create") -> dict:
    response = client.post(
        "/orders",
        json={"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 2}]},
        headers={"X-Correlation-Id": correlation_id},
    )
    assert response.status_code == 201
    return response.json()


def test_a_confirmed_order_writes_exactly_one_order_confirmed_row(
    client: TestClient, repository: InMemoryOrderRepository
) -> None:
    order = confirm_order(client)

    assert repository.event_types == [EVENT_ORDER_CONFIRMED]
    message = repository.messages[0]
    payload = message.payload

    assert list(payload) == ENVELOPE_KEYS
    assert message.event_id == payload["eventId"]
    uuid.UUID(payload["eventId"])
    assert payload["eventType"] == "order.confirmed"
    assert payload["eventVersion"] == 1
    assert payload["orderId"] == order["orderId"]
    assert payload["customerId"] == "c-1"
    assert payload["correlationId"] == "corr-create"
    assert payload["reason"] is None
    assert payload["lines"] == [{"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}]
    assert payload["occurredAt"] == order["confirmedAt"]


def test_a_cancellation_writes_exactly_one_order_cancelled_row_with_the_reason(
    client: TestClient, repository: InMemoryOrderRepository
) -> None:
    order = confirm_order(client)
    cancelled = client.post(
        f"/orders/{order['orderId']}/cancel",
        json={"reason": "  customer changed their mind  "},
        headers={"X-Correlation-Id": "corr-cancel"},
    ).json()

    assert repository.event_types == [EVENT_ORDER_CONFIRMED, EVENT_ORDER_CANCELLED]
    payload = repository.messages[1].payload
    assert list(payload) == ENVELOPE_KEYS
    assert payload["eventType"] == "order.cancelled"
    assert payload["reason"] == "customer changed their mind"
    assert payload["correlationId"] == "corr-cancel"
    assert payload["occurredAt"] == cancelled["cancelledAt"]
    assert payload["lines"] == [{"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}]


def test_repeating_a_cancellation_writes_no_second_event(
    client: TestClient, repository: InMemoryOrderRepository
) -> None:
    order = confirm_order(client)
    client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})
    client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})

    assert repository.event_types == [EVENT_ORDER_CONFIRMED, EVENT_ORDER_CANCELLED]


def test_a_failed_order_writes_no_event(client: TestClient, repository: InMemoryOrderRepository, inventory) -> None:
    inventory.insufficient_at_reserve = True
    response = client.post("/orders", json={"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 1}]})

    assert response.status_code == 409
    assert repository.event_types == []


def test_shipping_writes_no_event(client: TestClient, repository: InMemoryOrderRepository) -> None:
    order = confirm_order(client)
    assert client.post(f"/orders/{order['orderId']}/ship").status_code == 200
    assert repository.event_types == [EVENT_ORDER_CONFIRMED]


def test_two_orders_publish_two_distinct_event_ids(client: TestClient, repository: InMemoryOrderRepository) -> None:
    confirm_order(client)
    confirm_order(client, correlation_id="corr-2")

    event_ids = [message.event_id for message in repository.messages]
    assert len(event_ids) == 2
    assert len(set(event_ids)) == 2
    assert {message.payload["correlationId"] for message in repository.messages} == {"corr-create", "corr-2"}


def test_the_envelope_of_a_multi_line_order_carries_every_line(
    client: TestClient, repository: InMemoryOrderRepository
) -> None:
    client.post(
        "/orders",
        json={
            "customerId": "c-9",
            "lines": [
                {"productId": "p-1004", "quantity": 3},
                {"productId": "p-1002", "quantity": 1},
            ],
        },
    )
    payload = repository.messages[0].payload
    assert payload["lines"] == [
        {"productId": "p-1004", "quantity": 3, "unitPrice": 4.25},
        {"productId": "p-1002", "quantity": 1, "unitPrice": 12.5},
    ]
    assert payload["customerId"] == "c-9"
