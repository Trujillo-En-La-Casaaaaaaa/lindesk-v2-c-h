"""Order event contracts: the ``order.confirmed`` / ``order.cancelled`` envelopes.

The payload built here is the complete envelope of contracts.md §8 - the same
object that is stored in the ``outbox.payload`` column and published to the
``shopflow.orders`` topic exchange with routing key ``order.confirmed`` or
``order.cancelled``. Nothing in this module talks to a broker: it only builds the
durable intent.

Envelope (contracts.md §8)::

    {
      "eventId": "uuid",                # stable across every retry of the row
      "eventType": "order.confirmed | order.cancelled",
      "eventVersion": 1,
      "occurredAt": "ISO-8601",
      "correlationId": "string",
      "orderId": "uuid",
      "customerId": "string",
      "lines": [{ "productId": "string", "quantity": 1, "unitPrice": 0.0 }],
      "reason": "string | null"         # only the cancellation carries a reason
    }
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.errors import InternalError
from app.domain.order import Order, format_timestamp, utc_now
from app.ports.outbox import OutboxMessage

#: contracts.md §8: the two event types this service publishes.
EVENT_ORDER_CONFIRMED = "order.confirmed"
EVENT_ORDER_CANCELLED = "order.cancelled"
EVENT_VERSION = 1
SUPPORTED_EVENT_TYPES: tuple[str, ...] = (EVENT_ORDER_CONFIRMED, EVENT_ORDER_CANCELLED)


def envelope(
    *,
    order: Order,
    event_id: str,
    event_type: str,
    occurred_at: datetime,
    correlation_id: str,
    reason: str | None,
) -> dict[str, Any]:
    """Build the full event envelope of contracts.md §8 for one order."""

    if event_type not in SUPPORTED_EVENT_TYPES:
        raise InternalError(f"Unsupported order event type {event_type!r}.")
    return {
        "eventId": event_id,
        "eventType": event_type,
        "eventVersion": EVENT_VERSION,
        "occurredAt": format_timestamp(occurred_at),
        "correlationId": correlation_id,
        "orderId": order.order_id,
        "customerId": order.customer_id,
        "lines": [line.to_event_line() for line in order.lines],
        "reason": reason,
    }


def order_confirmed_event(
    order: Order,
    *,
    correlation_id: str = "",
    occurred_at: datetime | None = None,
    event_id: str | None = None,
) -> OutboxMessage:
    """Build the ``order.confirmed`` durable intent of ``order``."""

    moment = occurred_at or utc_now()
    message_id = event_id or _new_event_id()
    payload = envelope(
        order=order,
        event_id=message_id,
        event_type=EVENT_ORDER_CONFIRMED,
        occurred_at=moment,
        correlation_id=correlation_id,
        reason=None,
    )
    return OutboxMessage(
        event_id=message_id,
        event_type=EVENT_ORDER_CONFIRMED,
        payload=payload,
        occurred_at=moment,
    )


def order_cancelled_event(
    order: Order,
    *,
    correlation_id: str = "",
    occurred_at: datetime | None = None,
    event_id: str | None = None,
) -> OutboxMessage:
    """Build the ``order.cancelled`` durable intent of ``order``.

    contracts.md §8: ``reason`` is present for a cancellation and the consumer
    requires it, so an order without a stored reason cannot be published.
    """

    reason = order.cancellation_reason
    if not reason:
        raise InternalError(
            f"Order {order.order_id} cannot publish order.cancelled without a cancellation reason.",
            details={"orderId": order.order_id},
        )
    moment = occurred_at or utc_now()
    message_id = event_id or _new_event_id()
    payload = envelope(
        order=order,
        event_id=message_id,
        event_type=EVENT_ORDER_CANCELLED,
        occurred_at=moment,
        correlation_id=correlation_id,
        reason=reason,
    )
    return OutboxMessage(
        event_id=message_id,
        event_type=EVENT_ORDER_CANCELLED,
        payload=payload,
        occurred_at=moment,
    )


def _new_event_id() -> str:
    import uuid

    return str(uuid.uuid4())


__all__ = [
    "EVENT_ORDER_CANCELLED",
    "EVENT_ORDER_CONFIRMED",
    "EVENT_VERSION",
    "SUPPORTED_EVENT_TYPES",
    "envelope",
    "order_cancelled_event",
    "order_confirmed_event",
]
