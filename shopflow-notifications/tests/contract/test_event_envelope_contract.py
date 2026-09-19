"""Contract tests of the consumed event envelope (contracts.md §8).

The envelope below is a golden sample built from the field list of contracts.md §8.
Renaming or dropping a field, changing the version handling or accepting an unknown
event type would show up here.
"""

from __future__ import annotations

import pytest

from app.domain.notification import (
    CANCELLED_EVENT_TYPE,
    CONFIRMED_EVENT_TYPE,
    MAX_REASON_LENGTH,
    InvalidEventError,
    NotificationType,
    UnsupportedEventError,
    parse_order_event,
)
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id

ORDER = order_id("envelope")


def test_the_confirmed_envelope_has_exactly_the_contracted_fields() -> None:
    payload = order_confirmed(order=ORDER, event=event_id("envelope-c"))

    assert set(payload) == {
        "eventId",
        "eventType",
        "eventVersion",
        "occurredAt",
        "correlationId",
        "orderId",
        "customerId",
        "lines",
        "reason",
    }
    assert set(payload["lines"][0]) == {"productId", "quantity", "unitPrice"}

    event = parse_order_event(payload)
    assert event.event_type == CONFIRMED_EVENT_TYPE
    assert event.event_version == 1
    assert event.notification_type is NotificationType.CONFIRMATION


def test_the_cancelled_envelope_carries_the_reason() -> None:
    payload = order_cancelled(order=ORDER, event=event_id("envelope-x"), reason="out of stock")

    event = parse_order_event(payload)

    assert event.event_type == CANCELLED_EVENT_TYPE
    assert event.notification_type is NotificationType.CANCELLATION
    assert event.reason == "out of stock"
    assert len(event.reason) <= MAX_REASON_LENGTH


def test_an_envelope_without_the_correlation_id_is_still_accepted() -> None:
    payload = order_confirmed(order=ORDER, event=event_id("envelope-no-corr"))
    payload.pop("correlationId")

    assert parse_order_event(payload).correlation_id is None


def test_only_the_two_contracted_event_types_are_consumed() -> None:
    for event_type in ("order.created", "order.shipped", "order.failed", "Order.Confirmed"):
        payload = order_confirmed(order=ORDER, event=event_id(f"envelope-{event_type}"))
        payload["eventType"] = event_type
        with pytest.raises(UnsupportedEventError):
            parse_order_event(payload)


def test_only_version_one_of_the_envelope_is_consumed() -> None:
    payload = order_confirmed(order=ORDER, event=event_id("envelope-v2"), event_version=2)

    with pytest.raises(UnsupportedEventError):
        parse_order_event(payload)


def test_a_cancellation_must_carry_a_usable_reason() -> None:
    for reason in (None, "", "   ", "x" * (MAX_REASON_LENGTH + 1)):
        payload = order_cancelled(order=ORDER, event=event_id("envelope-bad-reason"))
        payload["reason"] = reason
        with pytest.raises(InvalidEventError):
            parse_order_event(payload)
