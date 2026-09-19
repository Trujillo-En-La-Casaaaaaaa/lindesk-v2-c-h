"""Unit tests of the notification domain model (contracts.md §8, §9, §1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.notification import (
    CANCELLED_EVENT_TYPE,
    CONFIRMED_EVENT_TYPE,
    MAX_REASON_LENGTH,
    TEMPLATE_ORDER_CANCELLATION,
    TEMPLATE_ORDER_CONFIRMATION,
    InvalidEventError,
    NotificationStatus,
    NotificationType,
    UnsupportedEventError,
    as_money,
    as_uuid,
    format_timestamp,
    idempotency_key,
    notification_type_for_event,
    parse_order_event,
)
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id

ORDER = order_id("domain")
EVENT = event_id("domain")


def envelope(**overrides: object) -> dict[str, object]:
    payload = order_confirmed(order=ORDER, event=EVENT)
    payload.update(overrides)
    return payload


def test_confirmed_envelope_is_interpreted_field_by_field() -> None:
    event = parse_order_event(envelope())

    assert event.event_id == EVENT
    assert event.event_type == CONFIRMED_EVENT_TYPE
    assert event.event_version == 1
    assert event.order_id == ORDER
    assert event.customer_id == "cust-smoke"
    assert event.occurred_at == "2026-09-18T12:00:00Z"
    assert event.correlation_id == "corr-smoke"
    assert event.reason is None
    assert [line.product_id for line in event.lines] == ["p-1001", "p-1002"]
    assert event.lines[0].quantity == 3
    assert event.lines[0].unit_price == Decimal("19.99")
    assert event.lines[0].line_total == Decimal("59.97")


def test_total_is_computed_from_the_lines() -> None:
    event = parse_order_event(envelope())

    assert event.total == Decimal("72.47")


def test_lines_are_optional_and_total_is_zero_without_them() -> None:
    event = parse_order_event(envelope(lines=[]))

    assert event.lines == ()
    assert event.total == Decimal("0.00")


def test_notification_type_and_idempotency_key() -> None:
    event = parse_order_event(envelope())

    assert event.notification_type is NotificationType.CONFIRMATION
    assert event.notification_type.template == TEMPLATE_ORDER_CONFIRMATION
    assert event.idempotency_key == f"order:{ORDER}:order_confirmation"
    assert idempotency_key(ORDER, NotificationType.CANCELLATION) == f"order:{ORDER}:order_cancellation"


def test_cancelled_envelope_keeps_the_reason() -> None:
    event = parse_order_event(order_cancelled(order=ORDER, event=EVENT, reason="out of stock"))

    assert event.event_type == CANCELLED_EVENT_TYPE
    assert event.reason == "out of stock"
    assert event.notification_type is NotificationType.CANCELLATION
    assert event.notification_type.template == TEMPLATE_ORDER_CANCELLATION


def test_confirmation_never_carries_a_reason() -> None:
    payload = envelope()
    payload["reason"] = "left over from the envelope"

    assert parse_order_event(payload).reason is None


def test_cancellation_without_a_reason_is_rejected() -> None:
    payload = order_cancelled(order=ORDER, event=EVENT)
    payload["reason"] = None

    with pytest.raises(InvalidEventError):
        parse_order_event(payload)


def test_cancellation_reason_is_bounded_like_the_orders_schema() -> None:
    payload = order_cancelled(order=ORDER, event=EVENT, reason="x" * (MAX_REASON_LENGTH + 1))

    with pytest.raises(InvalidEventError):
        parse_order_event(payload)


def test_unknown_event_type_is_unsupported() -> None:
    with pytest.raises(UnsupportedEventError):
        parse_order_event(envelope(eventType="order.shipped"))


def test_unknown_event_version_is_unsupported() -> None:
    with pytest.raises(UnsupportedEventError):
        parse_order_event(envelope(eventVersion=2))


@pytest.mark.parametrize("bad", [None, "", "not-a-uuid", 42])
def test_event_id_must_be_a_uuid(bad: object) -> None:
    with pytest.raises(InvalidEventError):
        parse_order_event(envelope(eventId=bad))


@pytest.mark.parametrize("field_name", ["orderId", "customerId"])
@pytest.mark.parametrize("bad", [None, "", "   "])
def test_required_identifiers_must_not_be_blank(field_name: str, bad: object) -> None:
    with pytest.raises(InvalidEventError):
        parse_order_event(envelope(**{field_name: bad}))


@pytest.mark.parametrize(
    "bad_line",
    [
        {"productId": "p-1001", "quantity": 0, "unitPrice": 1.0},
        {"productId": "p-1001", "quantity": -3, "unitPrice": 1.0},
        {"productId": "p-1001", "quantity": "many", "unitPrice": 1.0},
        {"productId": "", "quantity": 1, "unitPrice": 1.0},
        {"quantity": 1, "unitPrice": 1.0},
        {"productId": "p-1001", "quantity": 1, "unitPrice": "free"},
    ],
)
def test_malformed_lines_are_rejected(bad_line: dict[str, object]) -> None:
    with pytest.raises(InvalidEventError):
        parse_order_event(envelope(lines=[bad_line]))


def test_money_is_quantised_to_two_decimals() -> None:
    assert as_money("19.999", field_name="unitPrice") == Decimal("20.00")
    assert as_money(12.5, field_name="unitPrice") == Decimal("12.50")
    with pytest.raises(InvalidEventError):
        as_money("not money", field_name="unitPrice")


def test_uuid_normalisation_accepts_braces_and_uppercase() -> None:
    expected = "3f2b1a4c-1111-4222-8333-444455556666"

    assert as_uuid(expected.upper(), field_name="eventId") == expected
    assert as_uuid("{" + expected + "}", field_name="eventId") == expected


def test_notification_type_for_event_maps_the_two_consumed_types() -> None:
    assert notification_type_for_event(CONFIRMED_EVENT_TYPE) is NotificationType.CONFIRMATION
    assert notification_type_for_event(CANCELLED_EVENT_TYPE) is NotificationType.CANCELLATION
    with pytest.raises(UnsupportedEventError):
        notification_type_for_event("order.shipped")


def test_statuses_and_types_use_the_contracted_values() -> None:
    assert [status.value for status in NotificationStatus] == ["SENT", "FAILED"]
    assert [kind.value for kind in NotificationType] == ["confirmation", "cancellation"]


def test_timestamps_are_iso_8601_utc_with_a_z_suffix() -> None:
    moment = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)

    assert format_timestamp(moment) == "2026-09-18T12:00:00Z"
    assert format_timestamp(moment.astimezone(timezone(timedelta(hours=2)))) == "2026-09-18T12:00:00Z"
    assert format_timestamp(datetime(2026, 9, 18, 12, 0, 0)) == "2026-09-18T12:00:00Z"
