"""The read use cases: order detail and the order list (HANDOFF #5, contracts.md §5).

Both return the order view of contracts.md §3, which carries ``cancelledAt`` and
``cancellationReason`` so the customer status screen can show a cancellation.
"""

from __future__ import annotations

from app.domain.errors import NotFound, ValidationError
from app.domain.order import Order
from app.ports.order_repository import OrderPage, OrderRepository


def get_order(*, repository: OrderRepository, order_id: str) -> Order:
    """Return one order or raise ``404 not_found``."""

    order = repository.get(order_id)
    if order is None:
        raise NotFound(f"Order {order_id} was not found.", details={"orderId": order_id})
    return order


def list_orders(
    *,
    repository: OrderRepository,
    customer_id: str,
    limit: int | None = None,
    offset: int | None = None,
) -> OrderPage:
    """Return one page of a customer's orders plus the total number of orders.

    ``limit``/``offset`` are optional; a malformed value is ``400 validation_error``.
    """

    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValidationError("customerId must be a non-empty string.", details={"field": "customerId"})
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValidationError("limit must be a non-negative integer.", details={"field": "limit"})
    if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise ValidationError("offset must be a non-negative integer.", details={"field": "offset"})

    return repository.list_by_customer(customer_id, limit=limit, offset=offset or 0)


def orders_view(page: OrderPage) -> dict[str, object]:
    """The ``{items, total}`` list shape of contracts.md §1/§5."""

    return {"items": [order.to_view() for order in page.items], "total": page.total}


__all__ = ["get_order", "list_orders", "orders_view"]
