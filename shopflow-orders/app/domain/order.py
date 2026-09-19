"""The order aggregate and its status machine (HANDOFF #1, contracts.md §3/§9).

This module is pure Python. It owns

* the five statuses of an order and the four allowed transitions
  (``CREATED -> CONFIRMED``, ``CREATED -> FAILED``, ``CONFIRMED -> SHIPPED``,
  ``CONFIRMED -> CANCELLED``); every other transition raises
  :class:`~app.domain.errors.InvalidStatusTransition`;
* the ``inventory_release_state`` bookkeeping of an order (``NONE``, ``PENDING``,
  ``RELEASED``) - the durable intent behind an inventory release;
* the order line snapshot (``sku``, ``name``, ``unitPrice`` copied from the
  inventory product) and the money helpers shared by the view, the store and the
  event envelope;
* the request validation of ``POST /orders`` that does not depend on a web
  framework at all.

Nothing here imports FastAPI, SQLAlchemy or the broker client.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any

from app.domain.errors import InvalidStatusTransition, ValidationError

#: contracts.md §9: ``numeric(12,2)`` money is quantised with banker's rounding.
MONEY_QUANTUM = Decimal("0.01")

#: contracts.md §9 stores at most 200 characters of cancellation reason.
MAX_CANCELLATION_REASON_LENGTH = 200


class OrderStatus(StrEnum):
    """``orders.status``: the five statuses of contracts.md §9."""

    CREATED = "CREATED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"


CREATED = OrderStatus.CREATED
CONFIRMED = OrderStatus.CONFIRMED
FAILED = OrderStatus.FAILED
SHIPPED = OrderStatus.SHIPPED
CANCELLED = OrderStatus.CANCELLED


class InventoryReleaseState(StrEnum):
    """``orders.inventory_release_state``: the durable state of the stock restoration."""

    NONE = "NONE"
    PENDING = "PENDING"
    RELEASED = "RELEASED"


#: The complete transition table of the status machine (HANDOFF #1).
ALLOWED_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.CONFIRMED, OrderStatus.FAILED}),
    OrderStatus.CONFIRMED: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.FAILED: frozenset(),
    OrderStatus.SHIPPED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


def transition_allowed(current: OrderStatus, target: OrderStatus) -> bool:
    """Return ``True`` when the state machine allows ``current -> target``."""

    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def require_transition(current: OrderStatus, target: OrderStatus, *, order_id: str) -> None:
    """Raise :class:`InvalidStatusTransition` unless the transition is allowed."""

    if not transition_allowed(current, target):
        raise InvalidStatusTransition(order_id, str(current), str(target))


# --------------------------------------------------------------------------- #
# time and money helpers (contracts.md §1: ISO-8601 UTC with a ``Z`` suffix)
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""

    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime (a naive value means UTC)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Render a timestamp the way contracts.md §1 requires."""

    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def quantize_money(value: Decimal) -> Decimal:
    """Quantise a monetary amount to two decimal places (``numeric(12,2)``)."""

    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def money_as_number(value: Decimal) -> float:
    """Render a quantised amount as the JSON number the order view carries."""

    return float(quantize_money(value))


def as_money(value: Any) -> Decimal:
    """Interpret a JSON number/string as a quantised monetary amount."""

    return quantize_money(Decimal(str(value)))


# --------------------------------------------------------------------------- #
# request validation (POST /orders)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RequestedLine:
    """One requested ``{productId, quantity}`` line of ``POST /orders``."""

    product_id: str
    quantity: int


def validate_order_request(
    customer_id: Any,
    lines: Iterable[Any],
) -> tuple[RequestedLine, ...]:
    """Validate the ``POST /orders`` payload (``400 validation_error`` on any violation).

    The checks are repeated here - in addition to the request model of the API
    layer - so the rule is owned by the domain and can be exercised without HTTP.
    """

    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValidationError("customerId must be a non-empty string.", details={"field": "customerId"})

    requested: list[RequestedLine] = []
    if lines is None:
        raise ValidationError("lines must contain at least one order line.", details={"field": "lines"})
    for index, raw in enumerate(lines):
        if isinstance(raw, RequestedLine):
            product_id, quantity = raw.product_id, raw.quantity
        elif isinstance(raw, Mapping):
            product_id, quantity = raw.get("productId"), raw.get("quantity")
        else:
            raise ValidationError(
                f"lines[{index}] must be an object.",
                details={"field": "lines", "index": index},
            )
        if not isinstance(product_id, str) or not product_id.strip():
            raise ValidationError(
                f"lines[{index}].productId must be a non-empty string.",
                details={"field": "lines", "index": index},
            )
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValidationError(
                f"lines[{index}].quantity must be a positive integer.",
                details={"field": "lines", "index": index, "quantity": quantity},
            )
        requested.append(RequestedLine(product_id=product_id, quantity=quantity))

    if not requested:
        raise ValidationError("lines must contain at least one order line.", details={"field": "lines"})
    return tuple(requested)


# --------------------------------------------------------------------------- #
# the aggregate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OrderLine:
    """One stored order line: the snapshot copied from the inventory product."""

    product_id: str
    sku: str
    name: str
    quantity: int
    unit_price: Decimal

    @property
    def line_total(self) -> Decimal:
        """``unitPrice * quantity`` quantised to ``numeric(12,2)``."""

        return quantize_money(Decimal(self.unit_price) * Decimal(self.quantity))

    def to_view(self) -> dict[str, Any]:
        """The line shape of the order view (contracts.md §3)."""

        return {
            "productId": self.product_id,
            "sku": self.sku,
            "name": self.name,
            "quantity": self.quantity,
            "unitPrice": money_as_number(self.unit_price),
            "lineTotal": money_as_number(self.line_total),
        }

    def to_event_line(self) -> dict[str, Any]:
        """The line shape of the event envelope (contracts.md §8)."""

        return {
            "productId": self.product_id,
            "quantity": self.quantity,
            "unitPrice": money_as_number(self.unit_price),
        }

    def to_inventory_line(self) -> dict[str, Any]:
        """The line shape the inventory API consumes (contracts.md §4)."""

        return {"productId": self.product_id, "quantity": self.quantity}


def order_total(lines: Sequence[OrderLine]) -> Decimal:
    """Sum the line totals, quantised to ``numeric(12,2)``."""

    total = Decimal("0.00")
    for line in lines:
        total += line.line_total
    return quantize_money(total)


@dataclass(frozen=True, slots=True)
class Order:
    """The order aggregate of contracts.md §3/§9."""

    order_id: str
    customer_id: str
    status: OrderStatus
    lines: tuple[OrderLine, ...] = ()
    total: Decimal = Decimal("0.00")
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    confirmed_at: datetime | None = None
    shipped_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    inventory_release_state: InventoryReleaseState = InventoryReleaseState.NONE

    # -- invariant helpers -------------------------------------------------
    def require_transition_to(self, target: OrderStatus) -> None:
        """Raise :class:`InvalidStatusTransition` unless ``self.status -> target`` is allowed."""

        require_transition(self.status, target, order_id=self.order_id)

    @property
    def is_cancelled(self) -> bool:
        return self.status is OrderStatus.CANCELLED

    # -- transitions -------------------------------------------------------
    def confirmed(self, *, at: datetime) -> Order:
        """Return this order moved to ``CONFIRMED`` (the reservation succeeded)."""

        self.require_transition_to(OrderStatus.CONFIRMED)
        moment = as_utc(at)
        return replace(self, status=OrderStatus.CONFIRMED, confirmed_at=moment, updated_at=moment)

    def failed(self, *, at: datetime) -> Order:
        """Return this order moved to ``FAILED`` (the reservation was rejected)."""

        self.require_transition_to(OrderStatus.FAILED)
        return replace(self, status=OrderStatus.FAILED, updated_at=as_utc(at))

    def shipped(self, *, at: datetime) -> Order:
        """Return this order moved to ``SHIPPED``."""

        self.require_transition_to(OrderStatus.SHIPPED)
        moment = as_utc(at)
        return replace(self, status=OrderStatus.SHIPPED, shipped_at=moment, updated_at=moment)

    def cancelled(self, *, at: datetime, reason: str) -> Order:
        """Return this order moved to ``CANCELLED`` with a ``PENDING`` release.

        The status change and the pending release state are one write: the release
        that follows the commit is retried by the reconciler until it is
        ``RELEASED`` (contracts.md §4, HANDOFF #7).
        """

        self.require_transition_to(OrderStatus.CANCELLED)
        moment = as_utc(at)
        return replace(
            self,
            status=OrderStatus.CANCELLED,
            cancelled_at=moment,
            cancellation_reason=reason,
            inventory_release_state=InventoryReleaseState.PENDING,
            updated_at=moment,
        )

    def with_release_state(self, state: InventoryReleaseState) -> Order:
        """Return this order with the given ``inventory_release_state``."""

        return replace(self, inventory_release_state=state)

    # -- projections -------------------------------------------------------
    def to_view(self) -> dict[str, Any]:
        """The order view of contracts.md §3, field for field."""

        return {
            "orderId": self.order_id,
            "status": str(self.status),
            "customerId": self.customer_id,
            "createdAt": format_timestamp(self.created_at),
            "updatedAt": format_timestamp(self.updated_at),
            "confirmedAt": format_timestamp(self.confirmed_at) if self.confirmed_at else None,
            "shippedAt": format_timestamp(self.shipped_at) if self.shipped_at else None,
            "cancelledAt": format_timestamp(self.cancelled_at) if self.cancelled_at else None,
            "cancellationReason": self.cancellation_reason,
            "total": money_as_number(self.total),
            "lines": [line.to_view() for line in self.lines],
        }

    def inventory_lines(self) -> list[dict[str, Any]]:
        """The ``{productId, quantity}`` lines the inventory API consumes."""

        return [line.to_inventory_line() for line in self.lines]


def new_order_id() -> str:
    """Generate an order id (contracts.md §1: UUID-format strings)."""

    return str(uuid.uuid4())


def line_ids(order_id: str, count: int) -> tuple[str, ...]:
    """Deterministic, order-preserving surrogate keys for the lines of an order.

    contracts.md §9 declares ``order_lines(id uuid primary key)`` but no position
    column, while a client may send several lines and expects them back in the
    order it sent them. The line key therefore keeps the requested position in its
    low 16 bits: reading the lines ordered by ``id`` returns them in request order
    without adding a column to the documented schema.
    """

    base = uuid.UUID(str(order_id)).int >> 16
    return tuple(str(uuid.UUID(int=(base << 16) | position)) for position in range(count))


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CANCELLED",
    "CONFIRMED",
    "CREATED",
    "FAILED",
    "MAX_CANCELLATION_REASON_LENGTH",
    "MONEY_QUANTUM",
    "SHIPPED",
    "InventoryReleaseState",
    "Order",
    "OrderLine",
    "OrderStatus",
    "RequestedLine",
    "as_money",
    "as_utc",
    "format_timestamp",
    "line_ids",
    "money_as_number",
    "new_order_id",
    "order_total",
    "quantize_money",
    "require_transition",
    "transition_allowed",
    "utc_now",
    "validate_order_request",
]
