"""Deterministic message bodies (component: template-renderer, handoff #3/#4).

The provider contract (contracts.md §7) carries the message as a JSON ``payload``
object; this service owns exactly two templates, ``order_confirmation`` and
``order_cancellation``.

The renderer is a pure function of the order *content*:

* rendering the same event twice yields an identical body - no clock, no
  randomness, no dependence on dictionary iteration order (the human readable
  ``text`` is built from the event lines in envelope order);
* the body deliberately excludes the event metadata (``eventId``, ``occurredAt``,
  ``correlationId``). The provider is idempotent per ``order:{orderId}:{template}``,
  so two events describing the same order must render the same body: including a
  fresh ``eventId`` would make the emulator answer ``409 idempotency_conflict``
  instead of replaying the original ``messageId``. This is what makes the
  provider-side half of exactly-once delivery work (handoff, failure semantics);
* the cancellation body carries the event ``reason`` both in its own field and
  inside the readable ``text`` (handoff #4).

The recipient address is derived deterministically from the ``customerId`` of the
event envelope, because contracts.md §8 carries no address and this service owns
no customer-preferences store (scope lock). ``customerId`` values that already
look like an address are used verbatim.
"""

from __future__ import annotations

from typing import Any

from app.domain.notification import (
    InvalidEventError,
    NotificationType,
    OrderEvent,
    OrderEventLine,
    OutboundMessage,
    money_as_number,
    money_as_text,
)

#: Recipient domain of the derivation below. Overridable via configuration.
DEFAULT_RECIPIENT_DOMAIN = "notifications.shopflow.local"


def recipient_for(customer_id: str, *, domain: str = DEFAULT_RECIPIENT_DOMAIN) -> str:
    """Return the deterministic address of a customer (no preferences store)."""

    address = (customer_id or "").strip()
    if not address:
        raise InvalidEventError("customerId is required to address a notification", details={"field": "customerId"})
    if "@" in address:
        return address
    return f"{address}@{domain}"


def _render_line(line: OrderEventLine) -> dict[str, Any]:
    return {
        "productId": line.product_id,
        "quantity": line.quantity,
        "unitPrice": money_as_number(line.unit_price),
        "lineTotal": money_as_number(line.line_total),
    }


def _render_text(event: OrderEvent) -> str:
    if event.notification_type is NotificationType.CANCELLATION:
        return f"Order {event.order_id} was cancelled. Reason: {event.reason}. Total: {money_as_text(event.total)}."
    lines = "; ".join(
        f"{line.product_id} x{line.quantity} at {money_as_text(line.unit_price)} = {money_as_text(line.line_total)}"
        for line in event.lines
    )
    summary = f"Lines: {lines}. " if lines else ""
    return f"Order {event.order_id} is confirmed. {summary}Total: {money_as_text(event.total)}."


def render_payload(event: OrderEvent) -> dict[str, Any]:
    """Render the provider ``payload`` of the notification this event produces."""

    notification_type = event.notification_type
    payload: dict[str, Any] = {
        "template": notification_type.template,
        "orderId": event.order_id,
        "customerId": event.customer_id,
        "total": money_as_number(event.total),
        "lines": [_render_line(line) for line in event.lines],
    }
    if notification_type is NotificationType.CANCELLATION:
        payload["reason"] = event.reason
    payload["text"] = _render_text(event)
    return payload


def build_outbound_message(
    event: OrderEvent,
    *,
    recipient_domain: str = DEFAULT_RECIPIENT_DOMAIN,
) -> OutboundMessage:
    """Build the notification to deliver for one event."""

    return OutboundMessage(
        order_id=event.order_id,
        notification_type=event.notification_type,
        recipient=recipient_for(event.customer_id, domain=recipient_domain),
        payload=render_payload(event),
        correlation_id=event.correlation_id,
    )


__all__ = ["DEFAULT_RECIPIENT_DOMAIN", "build_outbound_message", "recipient_for", "render_payload"]
