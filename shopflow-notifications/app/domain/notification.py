"""Notification domain model of shopflow-notifications (contracts.md §8, §9).

This package is pure: nothing here imports FastAPI, SQLAlchemy, Alembic, psycopg2
or the broker client (acceptance criterion 8). Everything is a plain value object
or a pure function, so the notification rules can be exercised without a store, a
broker or an HTTP server.

Vocabulary:

* ``eventId`` / ``eventType`` / ``orderId`` / ... - the order-event envelope of
  contracts.md §8;
* ``confirmation`` / ``cancellation`` - the only two notification types this
  service owns (contracts.md §9);
* ``SENT`` / ``FAILED`` - the only two recorded statuses;
* ``order:{orderId}:{template}`` - the provider idempotency key of contracts.md §7.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------- #
# contracts.md §8 - the order-event envelope
# --------------------------------------------------------------------------- #
CONFIRMED_EVENT_TYPE = "order.confirmed"
CANCELLED_EVENT_TYPE = "order.cancelled"
SUPPORTED_EVENT_TYPES: tuple[str, ...] = (CONFIRMED_EVENT_TYPE, CANCELLED_EVENT_TYPE)
SUPPORTED_EVENT_VERSIONS: tuple[int, ...] = (1,)

# --------------------------------------------------------------------------- #
# contracts.md §7 - the two templates this service owns
# --------------------------------------------------------------------------- #
TEMPLATE_ORDER_CONFIRMATION = "order_confirmation"
TEMPLATE_ORDER_CANCELLATION = "order_cancellation"

#: contracts.md §9 - ``cancellation_reason varchar(200)`` bounds the reason.
MAX_REASON_LENGTH = 200

MONEY_QUANTUM = Decimal("0.01")


class DomainError(Exception):
    """A rule of the notification domain was violated."""

    #: contracts.md §12 error code.
    code = "internal_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class InvalidEventError(DomainError):
    """The event envelope (contracts.md §8) cannot be interpreted."""

    code = "validation_error"


class UnsupportedEventError(DomainError):
    """The event type or version is not one this service consumes.

    Permanent by nature: retrying will not make an unknown event type known, so
    the consumer dead-letters it instead of burning its retry budget.
    """

    code = "validation_error"


class InvalidRequestError(DomainError):
    """An HTTP request parameter is missing or malformed."""

    code = "validation_error"


# --------------------------------------------------------------------------- #
# time helpers - contracts.md §1: ISO-8601 UTC with a ``Z`` suffix
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""

    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime (naive input means UTC)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Render a timestamp the way contracts.md §1 requires."""

    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# coercion helpers
# --------------------------------------------------------------------------- #
def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise InvalidEventError(f"{field_name} is required", details={"field": field_name})
    candidate = str(value).strip()
    if not candidate:
        raise InvalidEventError(f"{field_name} must not be blank", details={"field": field_name})
    return candidate


def _optional_text(value: Any, *, field_name: str, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if max_length is not None and len(candidate) > max_length:
        raise InvalidEventError(
            f"{field_name} must be at most {max_length} characters",
            details={"field": field_name, "length": len(candidate)},
        )
    return candidate


def as_uuid(value: Any, *, field_name: str) -> str:
    """Validate a UUID-format identifier (contracts.md §1) and normalise it."""

    candidate = _required_text(value, field_name=field_name)
    try:
        return str(uuid.UUID(candidate))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidEventError(
            f"{field_name} must be a UUID-format string",
            details={"field": field_name},
        ) from exc


def as_quantity(value: Any, *, field_name: str) -> int:
    """Validate a strictly positive integer quantity."""

    if isinstance(value, bool) or value is None:
        raise InvalidEventError(f"{field_name} must be a positive integer", details={"field": field_name})
    if isinstance(value, int):
        quantity = value
    else:
        try:
            quantity = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise InvalidEventError(
                f"{field_name} must be a positive integer",
                details={"field": field_name},
            ) from exc
    if quantity < 1:
        raise InvalidEventError(f"{field_name} must be a positive integer", details={"field": field_name})
    return quantity


def as_money(value: Any, *, field_name: str) -> Decimal:
    """Validate a monetary amount and quantise it to two decimal places."""

    if isinstance(value, bool) or value is None:
        raise InvalidEventError(f"{field_name} must be a number", details={"field": field_name})
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError, TypeError) as exc:
        raise InvalidEventError(f"{field_name} must be a number", details={"field": field_name}) from exc
    if not amount.is_finite():
        raise InvalidEventError(f"{field_name} must be a finite number", details={"field": field_name})
    return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def money_as_number(amount: Decimal) -> float:
    """Render a quantised amount as the JSON number the provider payload carries."""

    return float(amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def money_as_text(amount: Decimal) -> str:
    """Render a quantised amount for the deterministic human readable body."""

    return f"{amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN):.2f}"


# --------------------------------------------------------------------------- #
# the two notification types and the two recorded statuses (contracts.md §9)
# --------------------------------------------------------------------------- #
class NotificationType(StrEnum):
    """``notifications.notification_type``: the only two types in scope."""

    CONFIRMATION = "confirmation"
    CANCELLATION = "cancellation"

    @property
    def template(self) -> str:
        """The contracts.md §7 template delivered for this notification type."""

        return TEMPLATE_ORDER_CONFIRMATION if self is NotificationType.CONFIRMATION else TEMPLATE_ORDER_CANCELLATION

    @property
    def event_type(self) -> str:
        """The contracts.md §8 event type that produces this notification."""

        return CONFIRMED_EVENT_TYPE if self is NotificationType.CONFIRMATION else CANCELLED_EVENT_TYPE


class NotificationStatus(StrEnum):
    """``notifications.status``: the only two statuses in scope."""

    SENT = "SENT"
    FAILED = "FAILED"


def notification_type_for_event(event_type: str) -> NotificationType:
    """Map an event type (contracts.md §8) onto the notification type it produces."""

    if event_type == CONFIRMED_EVENT_TYPE:
        return NotificationType.CONFIRMATION
    if event_type == CANCELLED_EVENT_TYPE:
        return NotificationType.CANCELLATION
    raise UnsupportedEventError(
        f"eventType '{event_type}' is not consumed by this service",
        details={"eventType": event_type, "supported": list(SUPPORTED_EVENT_TYPES)},
    )


def idempotency_key(order_id: str, notification_type: NotificationType) -> str:
    """The contracts.md §7 ``Idempotency-Key`` of one logical notification."""

    return f"order:{order_id}:{notification_type.template}"


# --------------------------------------------------------------------------- #
# event envelope -> domain event
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrderEventLine:
    """One line of the event envelope (contracts.md §8)."""

    product_id: str
    quantity: int
    unit_price: Decimal

    @property
    def line_total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class OrderEvent:
    """A consumed ``order.confirmed`` / ``order.cancelled`` envelope (contracts.md §8)."""

    event_id: str
    event_type: str
    event_version: int
    order_id: str
    customer_id: str
    lines: tuple[OrderEventLine, ...] = ()
    reason: str | None = None
    occurred_at: str | None = None
    correlation_id: str | None = None

    @property
    def notification_type(self) -> NotificationType:
        return notification_type_for_event(self.event_type)

    @property
    def total(self) -> Decimal:
        """The order total implied by the event lines (the envelope carries no total)."""

        total = Decimal("0.00")
        for line in self.lines:
            total += line.line_total
        return total.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)

    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.order_id, self.notification_type)


def parse_order_event(body: Mapping[str, Any]) -> OrderEvent:
    """Interpret an event envelope (contracts.md §8); raise :class:`DomainError` when it is invalid."""

    if not isinstance(body, Mapping):
        raise InvalidEventError("the event body must be a JSON object")

    event_type = _required_text(body.get("eventType"), field_name="eventType")
    notification_type = notification_type_for_event(event_type)

    raw_version = body.get("eventVersion")
    try:
        event_version = int(str(raw_version).strip())
    except (TypeError, ValueError) as exc:
        raise InvalidEventError(
            "eventVersion must be an integer",
            details={"field": "eventVersion"},
        ) from exc
    if event_version not in SUPPORTED_EVENT_VERSIONS:
        raise UnsupportedEventError(
            f"eventVersion {event_version} is not supported",
            details={"eventVersion": event_version, "supported": list(SUPPORTED_EVENT_VERSIONS)},
        )

    raw_lines = body.get("lines") or []
    if not isinstance(raw_lines, (list, tuple)):
        raise InvalidEventError("lines must be a list", details={"field": "lines"})
    lines: list[OrderEventLine] = []
    for index, raw_line in enumerate(raw_lines):
        if not isinstance(raw_line, Mapping):
            raise InvalidEventError(f"lines[{index}] must be an object", details={"field": "lines", "index": index})
        lines.append(
            OrderEventLine(
                product_id=_required_text(raw_line.get("productId"), field_name=f"lines[{index}].productId"),
                quantity=as_quantity(raw_line.get("quantity"), field_name=f"lines[{index}].quantity"),
                unit_price=as_money(raw_line.get("unitPrice"), field_name=f"lines[{index}].unitPrice"),
            )
        )

    reason = _optional_text(body.get("reason"), field_name="reason", max_length=MAX_REASON_LENGTH)
    if notification_type is NotificationType.CANCELLATION and reason is None:
        # contracts.md §8: ``reason`` is present only for ``order.cancelled``, and the
        # cancellation notification must carry it (handoff #4), so its absence is fatal.
        raise InvalidEventError(
            "reason is required for a cancellation",
            details={"field": "reason", "eventType": event_type},
        )
    if notification_type is NotificationType.CONFIRMATION:
        reason = None

    return OrderEvent(
        event_id=as_uuid(body.get("eventId"), field_name="eventId"),
        event_type=event_type,
        event_version=event_version,
        order_id=_required_text(body.get("orderId"), field_name="orderId"),
        customer_id=_required_text(body.get("customerId"), field_name="customerId"),
        lines=tuple(lines),
        reason=reason,
        occurred_at=_optional_text(body.get("occurredAt"), field_name="occurredAt"),
        correlation_id=_optional_text(body.get("correlationId"), field_name="correlationId"),
    )


# --------------------------------------------------------------------------- #
# outbound notification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutboundMessage:
    """One notification to deliver through the ``provider-port``."""

    order_id: str
    notification_type: NotificationType
    recipient: str
    payload: dict[str, Any]
    correlation_id: str | None = None

    @property
    def template(self) -> str:
        return self.notification_type.template

    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.order_id, self.notification_type)


@dataclass(frozen=True)
class ProviderReceipt:
    """What the provider answered for one accepted message (contracts.md §7)."""

    message_id: str
    status: str = "accepted"
    accepted_at: str | None = None


# --------------------------------------------------------------------------- #
# the notification record (contracts.md §9)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NotificationRecord:
    """One row of ``notifications`` (contracts.md §9)."""

    notification_id: str
    order_id: str
    notification_type: NotificationType
    event_id: str
    status: NotificationStatus
    provider_message_id: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime

    @property
    def template(self) -> str:
        return self.notification_type.template

    @property
    def delivered(self) -> bool:
        return self.status is NotificationStatus.SENT


def new_notification_id() -> str:
    """Generate the surrogate key of a notification record."""

    return str(uuid.uuid4())


__all__ = [
    "CANCELLED_EVENT_TYPE",
    "CONFIRMED_EVENT_TYPE",
    "DomainError",
    "InvalidEventError",
    "InvalidRequestError",
    "NotificationRecord",
    "NotificationStatus",
    "NotificationType",
    "OrderEvent",
    "OrderEventLine",
    "OutboundMessage",
    "ProviderReceipt",
    "SUPPORTED_EVENT_TYPES",
    "SUPPORTED_EVENT_VERSIONS",
    "TEMPLATE_ORDER_CANCELLATION",
    "TEMPLATE_ORDER_CONFIRMATION",
    "UnsupportedEventError",
    "as_money",
    "as_quantity",
    "as_utc",
    "as_uuid",
    "format_timestamp",
    "idempotency_key",
    "money_as_number",
    "money_as_text",
    "new_notification_id",
    "notification_type_for_event",
    "parse_order_event",
    "utc_now",
]
