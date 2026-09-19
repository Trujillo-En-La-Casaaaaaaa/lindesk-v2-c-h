"""Unit tests of the event contracts (contracts.md §8, HANDOFF #6)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.errors import InternalError
from app.domain.order import OrderStatus
from app.events.contracts import (
    EVENT_ORDER_CANCELLED,
    EVENT_ORDER_CONFIRMED,
    EVENT_VERSION,
    envelope,
    order_cancelled_event,
    order_confirmed_event,
)
from tests.support.fakes import new_order

MOMENT = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
ORDER_ID = "0f14de0e-6f2e-4b7a-9f5b-1f3f1d9c1111"

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


def confirmed_order():
    return new_order(order_id=ORDER_ID, status=OrderStatus.CONFIRMED)


def cancelled_order():
    return confirmed_order().cancelled(at=MOMENT + timedelta(minutes=1), reason="customer changed their mind")


def test_confirmed_envelope_has_the_documented_shape() -> None:
    message = order_confirmed_event(confirmed_order(), correlation_id="corr-1", occurred_at=MOMENT)

    assert message.event_type == EVENT_ORDER_CONFIRMED
    assert message.routing_key == "order.confirmed"
    assert message.occurred_at == MOMENT
    assert list(message.payload) == ENVELOPE_KEYS
    assert message.payload == {
        "eventId": message.event_id,
        "eventType": "order.confirmed",
        "eventVersion": EVENT_VERSION,
        "occurredAt": "2026-09-18T12:00:00Z",
        "correlationId": "corr-1",
        "orderId": ORDER_ID,
        "customerId": "c-1",
        "lines": [{"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}],
        "reason": None,
    }
    uuid.UUID(message.event_id)  # UUID-format, contracts.md §1


def test_cancelled_envelope_carries_the_stored_reason() -> None:
    order = cancelled_order()
    message = order_cancelled_event(order, correlation_id="corr-2", occurred_at=order.cancelled_at)

    assert message.event_type == EVENT_ORDER_CANCELLED
    assert message.routing_key == "order.cancelled"
    assert message.payload["reason"] == "customer changed their mind"
    assert message.payload["occurredAt"] == "2026-09-18T12:01:00Z"
    assert message.payload["correlationId"] == "corr-2"
    assert list(message.payload) == ENVELOPE_KEYS


def test_a_cancelled_event_without_a_reason_is_refused() -> None:
    order = new_order(order_id=ORDER_ID, status=OrderStatus.CANCELLED)
    assert order.cancellation_reason is None
    with pytest.raises(InternalError):
        order_cancelled_event(order, occurred_at=MOMENT)


def test_the_event_id_is_stable_when_it_is_supplied() -> None:
    event_id = "9c8b7a65-4321-4321-9876-0fedcba98765"
    first = order_confirmed_event(confirmed_order(), occurred_at=MOMENT, event_id=event_id)
    second = order_confirmed_event(confirmed_order(), occurred_at=MOMENT, event_id=event_id)

    assert first.event_id == second.event_id == event_id
    assert first.payload["eventId"] == event_id
    assert order_confirmed_event(confirmed_order(), occurred_at=MOMENT).event_id != event_id


def test_envelope_rejects_an_unknown_event_type() -> None:
    with pytest.raises(InternalError):
        envelope(
            order=confirmed_order(),
            event_id=str(uuid.uuid4()),
            event_type="order.exploded",
            occurred_at=MOMENT,
            correlation_id="",
            reason=None,
        )


def test_lines_are_rendered_field_for_field() -> None:
    message = order_confirmed_event(confirmed_order(), occurred_at=MOMENT)
    assert message.payload["lines"][0] == {"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}
    assert set(message.payload["lines"][0]) == {"productId", "quantity", "unitPrice"}


def test_the_correlation_id_is_forwarded_unchanged_even_when_absent() -> None:
    message = order_confirmed_event(confirmed_order(), occurred_at=MOMENT)
    assert message.payload["correlationId"] == ""
    forwarded = order_confirmed_event(confirmed_order(), correlation_id="abc-123", occurred_at=MOMENT)
    assert forwarded.payload["correlationId"] == "abc-123"
