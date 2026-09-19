"""The cancellation policy (HANDOFF #3, contracts.md §5).

The policy answers two questions, and it answers them in the order the handoff
fixes:

1. is the supplied ``reason`` acceptable? It is trimmed first, then it must be
   non-empty and at most 200 characters - the value that is stored is the trimmed
   one (contracts.md §9 stores ``varchar(200)``);
2. given a *known* order, what may happen to it? A ``SHIPPED`` order is rejected
   with ``409 order_shipped``, an already ``CANCELLED`` order is a safe repeat
   (``200`` with the stored ``cancelledAt``/``cancellationReason`` and no second
   side effect), and any other non-cancellable status (``CREATED``, ``FAILED``)
   is rejected with ``409 order_not_cancellable``. Only ``CONFIRMED`` may be
   cancelled.

Keeping the decision a pure function of the order means the use case can re-apply
exactly the same rules to a re-read row after a lost guarded ``UPDATE`` (the
concurrent duplicate cancel of HANDOFF #3 rule 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.errors import InvalidReason, OrderNotCancellable, OrderShipped
from app.domain.order import MAX_CANCELLATION_REASON_LENGTH, Order, OrderStatus

MAX_REASON_LENGTH = MAX_CANCELLATION_REASON_LENGTH


class CancellationAction(StrEnum):
    """What the policy decided for one known order."""

    #: The order is ``CONFIRMED``: perform the guarded cancellation.
    APPLY = "APPLY"
    #: The order is already ``CANCELLED``: return the stored cancellation, do nothing.
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    #: The order is ``SHIPPED``: ``409 order_shipped``.
    REJECT_SHIPPED = "REJECT_SHIPPED"
    #: The order is ``CREATED`` or ``FAILED``: ``409 order_not_cancellable``.
    REJECT_NOT_CANCELLABLE = "REJECT_NOT_CANCELLABLE"


@dataclass(frozen=True, slots=True)
class CancellationDecision:
    """The policy outcome plus the order it applies to."""

    action: CancellationAction
    order: Order
    reason: str

    @property
    def applies(self) -> bool:
        return self.action is CancellationAction.APPLY

    @property
    def is_repeat(self) -> bool:
        return self.action is CancellationAction.ALREADY_CANCELLED


def normalize_reason(reason: Any) -> str:
    """Trim the reason, then require non-empty and at most 200 characters.

    Raises :class:`~app.domain.errors.InvalidReason` (``400 invalid_reason``) for
    every violation. The trimmed value is what the caller stores and publishes.
    """

    if reason is None or not isinstance(reason, str):
        raise InvalidReason(
            "Cancellation reason is required.",
            details={"field": "reason", "maxLength": MAX_REASON_LENGTH},
        )
    trimmed = reason.strip()
    if not trimmed:
        raise InvalidReason(
            "Cancellation reason must not be empty.",
            details={"field": "reason", "reason": reason, "maxLength": MAX_REASON_LENGTH},
        )
    if len(trimmed) > MAX_REASON_LENGTH:
        raise InvalidReason(
            f"Cancellation reason must be at most {MAX_REASON_LENGTH} characters.",
            details={"field": "reason", "length": len(trimmed), "maxLength": MAX_REASON_LENGTH},
        )
    return trimmed


def decide_cancellation(order: Order, *, reason: str) -> CancellationDecision:
    """Apply the status rules to a known order (HANDOFF #3 rules 3-6)."""

    if order.status is OrderStatus.SHIPPED:
        return CancellationDecision(CancellationAction.REJECT_SHIPPED, order, reason)
    if order.status is OrderStatus.CANCELLED:
        return CancellationDecision(CancellationAction.ALREADY_CANCELLED, order, reason)
    if order.status is not OrderStatus.CONFIRMED:
        return CancellationDecision(CancellationAction.REJECT_NOT_CANCELLABLE, order, reason)
    return CancellationDecision(CancellationAction.APPLY, order, reason)


def raise_for_decision(decision: CancellationDecision) -> None:
    """Raise the domain error the decision implies (``APPLY``/repeat return silently)."""

    if decision.action is CancellationAction.REJECT_SHIPPED:
        raise OrderShipped(decision.order.order_id)
    if decision.action is CancellationAction.REJECT_NOT_CANCELLABLE:
        raise OrderNotCancellable(decision.order.order_id, str(decision.order.status))


def stored_cancellation(order: Order) -> dict[str, Any]:
    """The stored cancellation facts of an already cancelled order (repeated cancel)."""

    return {
        "orderId": order.order_id,
        "status": str(order.status),
        "cancelledAt": order.cancelled_at,
        "cancellationReason": order.cancellation_reason,
    }


__all__ = [
    "MAX_REASON_LENGTH",
    "CancellationAction",
    "CancellationDecision",
    "decide_cancellation",
    "normalize_reason",
    "raise_for_decision",
    "stored_cancellation",
]
