"""The ship-order use case (HANDOFF #4, contracts.md §5).

An administrative action: only a ``CONFIRMED`` order may ship (``404`` when the
order is unknown, ``409 invalid_status_transition`` otherwise). The write is
guarded with ``WHERE status='CONFIRMED'`` so two concurrent ship calls cannot both
succeed, and the order is re-read afterwards so the response reports the stored
facts.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app.domain.errors import InvalidStatusTransition, NotFound
from app.domain.order import Order, OrderStatus, utc_now
from app.ports.order_repository import OrderRepository


def ship_order(
    *,
    repository: OrderRepository,
    order_id: str,
    now: Callable[[], datetime] = utc_now,
) -> Order:
    """Mark a confirmed order as shipped and return the stored order view."""

    order = repository.get(order_id)
    if order is None:
        raise NotFound(f"Order {order_id} was not found.", details={"orderId": order_id})

    if order.status is not OrderStatus.CONFIRMED:
        raise InvalidStatusTransition(order_id, str(order.status), str(OrderStatus.SHIPPED))

    shipped_at = now()
    if not repository.mark_shipped(order_id, shipped_at=shipped_at):
        fresh = repository.get(order_id)
        current = str(fresh.status) if fresh is not None else str(order.status)
        raise InvalidStatusTransition(order_id, current, str(OrderStatus.SHIPPED))

    stored = repository.get(order_id)
    return stored if stored is not None else order.shipped(at=shipped_at)


__all__ = ["ship_order"]
