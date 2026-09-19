"""The cancel-order use case (HANDOFF #3, contracts.md §5).

Rules, in the order the handoff fixes them:

1. the trimmed reason must be non-empty and at most 200 characters
   (``400 invalid_reason``); the trimmed value is what gets stored;
2. an unknown order is ``404 not_found``;
3. a ``SHIPPED`` order is ``409 order_shipped``;
4. an already ``CANCELLED`` order returns ``200`` with the **stored**
   ``cancelledAt``/``cancellationReason`` and performs no status change, no stock
   release and no notification (repeated cancellation safety);
5. any other non-cancellable status (``CREATED``, ``FAILED``) is
   ``409 order_not_cancellable``;
6. otherwise one transaction performs the guarded update
   ``... WHERE id = :id AND status = 'CONFIRMED'`` and inserts the
   ``order.cancelled`` outbox row with the order lines and the reason. When the
   guarded update matches nothing, the order is re-read and rules 3-5 are applied
   to the fresh status - that is what makes concurrent duplicate cancels safe:
   exactly one wins, the loser reports the stored cancellation;
7. after the commit the inventory release is attempted
   (``Idempotency-Key: order:{orderId}:release``). On success
   ``inventory_release_state`` becomes ``RELEASED``; on failure or timeout it
   stays ``PENDING`` and the reconciler retries until it is ``RELEASED``;
8. the response is ``200`` with the stored order view.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from app.domain.cancellation import decide_cancellation, normalize_reason, raise_for_decision
from app.domain.errors import DomainError, InternalError, NotFound
from app.domain.order import (
    InventoryReleaseState,
    Order,
    RequestedLine,
    utc_now,
)
from app.events.contracts import order_cancelled_event
from app.ports.inventory import InventoryPort
from app.ports.order_repository import OrderRepository
from app.usecases.deadline import DEFAULT_REQUEST_TIMEOUT_SECONDS

LOGGER = logging.getLogger("shopflow.orders.cancel_order")

#: How many times a lost guarded ``UPDATE`` may be retried against a fresh status.
MAX_CANCEL_ATTEMPTS = 3


def cancel_order(
    *,
    repository: OrderRepository,
    inventory: InventoryPort,
    order_id: str,
    reason: str,
    correlation_id: str = "",
    request_timeout_seconds: float | None = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    now: Callable[[], datetime] = utc_now,
    max_attempts: int = MAX_CANCEL_ATTEMPTS,
) -> Order:
    """Cancel a confirmed order and return the stored order view."""

    normalized_reason = normalize_reason(reason)

    order = repository.get(order_id)
    if order is None:
        raise NotFound(f"Order {order_id} was not found.", details={"orderId": order_id})

    decision = decide_cancellation(order, reason=normalized_reason)
    if decision.is_repeat:
        # Rule 4: a repeated cancellation is a safe read of the stored facts.
        LOGGER.info("repeated cancellation", extra={"orderId": order_id, "correlationId": correlation_id})
        return decision.order
    raise_for_decision(decision)  # rules 3 and 5

    cancelled: Order | None = None
    for _attempt in range(max(1, max_attempts)):
        cancelled_at = now()
        candidate = decision.order.cancelled(at=cancelled_at, reason=normalized_reason)
        event = order_cancelled_event(candidate, correlation_id=correlation_id, occurred_at=cancelled_at)
        if repository.mark_cancelled(
            order_id,
            cancelled_at=cancelled_at,
            reason=normalized_reason,
            event=event,
        ):
            cancelled = candidate
            break

        # Rule 6: the row is no longer in the status the decision was made on.
        # Re-read and re-apply rules 3-5 with the fresh status.
        fresh = repository.get(order_id)
        if fresh is None:
            raise NotFound(f"Order {order_id} was not found.", details={"orderId": order_id})
        decision = decide_cancellation(fresh, reason=normalized_reason)
        if decision.is_repeat:
            return decision.order
        raise_for_decision(decision)

    if cancelled is None:  # pragma: no cover - only under pathological contention
        raise InternalError(
            f"Order {order_id} could not be cancelled after {max_attempts} attempts.",
            details={"orderId": order_id},
        )

    # Rule 7: the release is a post-commit side effect. The PENDING release state
    # written with the cancellation is its durable intent.
    _release_reserved_stock(
        repository=repository,
        inventory=inventory,
        order=cancelled,
        timeout_seconds=request_timeout_seconds,
        correlation_id=correlation_id,
    )

    stored = repository.get(order_id)
    return stored if stored is not None else cancelled


def _release_reserved_stock(
    *,
    repository: OrderRepository,
    inventory: InventoryPort,
    order: Order,
    timeout_seconds: float | None,
    correlation_id: str,
) -> None:
    """Restore the stock of a cancelled order, exactly once.

    A failure is not fatal for the request: the order is already ``CANCELLED``
    with ``inventory_release_state='PENDING'`` and the reconciler retries the same
    idempotency key until the inventory reports the release, so the customer is
    never blocked by a transient inventory outage.
    """

    lines = [RequestedLine(product_id=line.product_id, quantity=line.quantity) for line in order.lines]
    try:
        inventory.release(order.order_id, lines, timeout_seconds=timeout_seconds)
    except DomainError as exc:
        LOGGER.warning(
            "release pending; the reconciler will retry",
            extra={"orderId": order.order_id, "error": str(exc), "correlationId": correlation_id},
        )
        return
    repository.set_release_state(order.order_id, state=InventoryReleaseState.RELEASED)


__all__ = ["MAX_CANCEL_ATTEMPTS", "cancel_order"]
