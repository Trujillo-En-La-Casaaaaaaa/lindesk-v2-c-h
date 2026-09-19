"""The create-order use case (HANDOFF #2, contracts.md §5).

Order of operations, exactly as the handoff fixes it:

1. validate the payload (``400 validation_error``);
2. ask the inventory for availability (``POST /inventory/availability``): an
   insufficient answer creates **no** order, an unreachable inventory returns
   ``503 inventory_unavailable`` and creates no order either;
3. persist the order with its lines as ``CREATED``, snapshotting ``sku``, ``name``
   and ``unitPrice`` from the inventory product and computing ``total``;
4. reserve the stock (``POST /inventory/reservations`` with
   ``Idempotency-Key: order:{orderId}:reserve``):
   * confirmed -> ``CONFIRMED`` plus the ``order.confirmed`` outbox row, in one
     transaction with the status change;
   * ``409 insufficient_stock`` (a race lost after validation) -> ``FAILED``; the
     reservation is all-or-nothing, so no stock remains reserved;
   * timeout/``503`` -> the order stays ``CREATED`` and the caller gets
     ``503 inventory_unavailable``. A ``CREATED`` order is never reported as
     confirmed; the reconciler retries the reservation with the same
     idempotency key until it is known to have succeeded.

The status change and its outbox row are always one transaction; no cross-service
distributed transaction is attempted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from app.domain.errors import (
    InsufficientStock,
    InternalError,
    UnknownProduct,
    insufficient_stock_details_for_items,
)
from app.domain.order import (
    InventoryReleaseState,
    Order,
    OrderLine,
    OrderStatus,
    RequestedLine,
    new_order_id,
    order_total,
    utc_now,
    validate_order_request,
)
from app.events.contracts import order_confirmed_event
from app.ports.inventory import InventoryPort
from app.ports.order_repository import OrderRepository
from app.usecases.deadline import DEFAULT_REQUEST_TIMEOUT_SECONDS, RequestBudget

LOGGER = logging.getLogger("shopflow.orders.create_order")


def create_order(
    *,
    repository: OrderRepository,
    inventory: InventoryPort,
    customer_id: str,
    lines: Sequence[RequestedLine],
    correlation_id: str = "",
    request_timeout_seconds: float | None = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    order_id: str | None = None,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> Order:
    """Create an order, reserve its stock and return the stored order view."""

    requested = validate_order_request(customer_id, lines)
    budget = RequestBudget(request_timeout_seconds, monotonic=monotonic)

    # -- 2. availability (read-only, before anything is written) ------------
    availability = inventory.check_availability(requested, timeout_seconds=budget.remaining())
    if not availability.available:
        failing = [item.to_payload() for item in availability.insufficient_items]
        LOGGER.info(
            "availability rejected",
            extra={"customerId": customer_id, "items": failing, "correlationId": correlation_id},
        )
        raise InsufficientStock(
            "Requested quantity exceeds available stock.",
            details=insufficient_stock_details_for_items(failing),
        )

    # -- 3. the order line snapshot + one transaction for order and lines ---
    snapshot: list[OrderLine] = []
    for line in requested:
        product = inventory.get_product(line.product_id, timeout_seconds=budget.remaining())
        snapshot.append(
            OrderLine(
                product_id=product.product_id,
                sku=product.sku,
                name=product.name,
                quantity=line.quantity,
                unit_price=product.price,
            )
        )

    created_at = now()
    order = Order(
        order_id=order_id or new_order_id(),
        customer_id=customer_id,
        status=OrderStatus.CREATED,
        lines=tuple(snapshot),
        total=order_total(snapshot),
        created_at=created_at,
        updated_at=created_at,
        inventory_release_state=InventoryReleaseState.NONE,
    )
    repository.create(order)

    # -- 4. reserve the stock ----------------------------------------------
    try:
        inventory.reserve(order.order_id, requested, timeout_seconds=budget.remaining())
    except (InsufficientStock, UnknownProduct):
        # The race was lost (or the product vanished): the reservation changed
        # nothing, so the order is terminal and no release is needed.
        repository.mark_failed(order.order_id, failed_at=now())
        raise
    except Exception as exc:  # noqa: BLE001 - InventoryUnavailable and friends
        # Ambiguous: the order stays CREATED. It is never reported as confirmed
        # here, and the reservation is retried idempotently (the key is derived
        # from the order id) by the reconciler.
        LOGGER.warning(
            "reservation not confirmed; order stays CREATED",
            extra={"orderId": order.order_id, "error": str(exc), "correlationId": correlation_id},
        )
        raise

    # -- confirmed: status change and outbox row in one transaction ---------
    confirmed_at = now()
    confirmed = order.confirmed(at=confirmed_at)
    event = order_confirmed_event(confirmed, correlation_id=correlation_id, occurred_at=confirmed_at)
    if not repository.mark_confirmed(order.order_id, confirmed_at=confirmed_at, event=event):
        # The reconciler confirmed this order in the same window: it won the
        # guarded update (and wrote the single outbox row), so report its state.
        stored = repository.get(order.order_id)
        if stored is None:  # pragma: no cover - the row was just written
            raise InternalError(
                f"Order {order.order_id} disappeared while being confirmed.",
                details={"orderId": order.order_id},
            )
        return stored

    stored = repository.get(order.order_id)
    return stored if stored is not None else confirmed


__all__ = ["create_order"]
