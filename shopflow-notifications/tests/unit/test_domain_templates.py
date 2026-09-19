"""Unit tests of the deterministic templates (handoff #3/#4)."""

from __future__ import annotations

import json

import pytest

from app.domain.notification import (
    TEMPLATE_ORDER_CANCELLATION,
    TEMPLATE_ORDER_CONFIRMATION,
    InvalidEventError,
    parse_order_event,
)
from app.domain.templates import (
    DEFAULT_RECIPIENT_DOMAIN,
    build_outbound_message,
    recipient_for,
    render_payload,
)
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id

ORDER = order_id("templates")
REASON = "customer changed their mind"


def confirmation():
    return parse_order_event(order_confirmed(order=ORDER, event=event_id("templates-confirmed")))


def cancellation(reason: str = REASON):
    return parse_order_event(order_cancelled(order=ORDER, event=event_id("templates-cancelled"), reason=reason))


def test_confirmation_payload_carries_order_lines_and_total() -> None:
    payload = render_payload(confirmation())

    assert payload["template"] == TEMPLATE_ORDER_CONFIRMATION
    assert payload["orderId"] == ORDER
    assert payload["total"] == 72.47
    assert payload["lines"] == [
        {"productId": "p-1001", "quantity": 3, "unitPrice": 19.99, "lineTotal": 59.97},
        {"productId": "p-1002", "quantity": 1, "unitPrice": 12.5, "lineTotal": 12.5},
    ]
    assert ORDER in payload["text"]
    assert "Total: 72.47" in payload["text"]


def test_cancellation_payload_carries_the_reason() -> None:
    payload = render_payload(cancellation())

    assert payload["template"] == TEMPLATE_ORDER_CANCELLATION
    assert payload["reason"] == REASON
    assert REASON in payload["text"]
    assert payload["orderId"] == ORDER


def test_rendering_is_deterministic_for_the_same_event() -> None:
    event = confirmation()

    assert render_payload(event) == render_payload(event)
    assert json.dumps(render_payload(event), sort_keys=True) == json.dumps(render_payload(event), sort_keys=True)


def test_the_body_depends_on_the_order_not_on_the_event_metadata() -> None:
    """Two events for the same order must render the same body (idempotent replay)."""

    first = parse_order_event(
        order_confirmed(order=ORDER, event=event_id("first"), occurred_at="2026-09-18T12:00:00Z")
    )
    second = parse_order_event(
        order_confirmed(order=ORDER, event=event_id("second"), occurred_at="2026-09-18T13:30:00Z")
    )

    assert render_payload(first) == render_payload(second)
    serialised = json.dumps(render_payload(first))
    for metadata in ("eventId", "occurredAt", "correlationId", "eventVersion"):
        assert metadata not in serialised


def test_a_different_reason_renders_a_different_body() -> None:
    assert render_payload(cancellation("out of stock")) != render_payload(cancellation(REASON))


def test_payload_is_json_serialisable() -> None:
    json.dumps(render_payload(confirmation()))
    json.dumps(render_payload(cancellation()))


def test_recipient_is_derived_deterministically_from_the_customer_id() -> None:
    assert recipient_for("cust-1") == f"cust-1@{DEFAULT_RECIPIENT_DOMAIN}"
    assert recipient_for("cust-1", domain="example.test") == "cust-1@example.test"
    assert recipient_for("customer@example.com") == "customer@example.com"
    with pytest.raises(InvalidEventError):
        recipient_for("   ")


def test_outbound_message_names_the_template_and_the_idempotency_key() -> None:
    message = build_outbound_message(confirmation())

    assert message.template == TEMPLATE_ORDER_CONFIRMATION
    assert message.idempotency_key == f"order:{ORDER}:order_confirmation"
    assert message.recipient == f"cust-smoke@{DEFAULT_RECIPIENT_DOMAIN}"
    assert message.correlation_id == "corr-smoke"
    assert message.notification_type.value == "confirmation"

    cancelled = build_outbound_message(cancellation())
    assert cancelled.template == TEMPLATE_ORDER_CANCELLATION
    assert cancelled.idempotency_key == f"order:{ORDER}:order_cancellation"
