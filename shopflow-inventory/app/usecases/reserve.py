"""Reservation use case: ``reserve_stock`` (contracts.md §4).

A reservation is the only thing that decrements stock. It is

* **all-or-nothing**: every line is verified under row locks inside one
  transaction before a single unit is removed, so a partial reservation can
  never be left behind;
* **idempotent and durable**: the ``reserve`` movement row is inserted first,
  guarded by ``unique(order_id, operation)``. When that row already exists the
  command is a replay and no stock changes - the guarantee lives in the store,
  not in process memory, so it survives restarts and concurrent retries;
* **safe under concurrency**: the stock rows are locked with
  ``SELECT ... FOR UPDATE`` in a deterministic order, so two orders racing for
  the last unit are serialized and the loser gets ``409 insufficient_stock``
  instead of a negative quantity.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.domain.errors import IdempotencyConflict, UnknownProduct
from app.domain.stock import (
    RESERVE,
    StockLine,
    StockMovement,
    canonical_lines,
    movement_payload,
    plan_decrements,
    require_idempotency_key,
)
from app.ports.repositories import CatalogRepository, StockRepository


def reserve_stock(
    *,
    catalog: CatalogRepository,
    stock: StockRepository,
    order_id: str,
    lines: Sequence[StockLine],
    idempotency_key: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reserve every line for ``order_id``, or change nothing at all."""

    require_idempotency_key(order_id=order_id, operation=RESERVE, provided=idempotency_key)
    canonical = canonical_lines(lines)
    payload = movement_payload(canonical)

    for line in canonical:
        if catalog.get_product(line.product_id) is None:
            raise UnknownProduct(line.product_id)

    movement = StockMovement(
        order_id=order_id,
        operation=RESERVE,
        lines=canonical,
        created_at=now or datetime.now(UTC),
    )

    with stock.transaction() as transaction:
        if not transaction.record_movement(movement):
            # The (order, reserve) record already exists: same body -> replay,
            # different body -> conflict. Either way nothing is written.
            existing = transaction.find_movement(order_id, RESERVE)
            transaction.rollback()
            if existing is None or existing.payload() != payload:
                raise IdempotencyConflict(order_id, RESERVE)
            return {"orderId": order_id, "reserved": True, "replayed": True}

        locked = transaction.lock_levels([line.product_id for line in canonical])
        decrements = plan_decrements(canonical, locked)  # raises InsufficientStock before any write
        transaction.decrement(decrements)

    return {"orderId": order_id, "reserved": True, "replayed": False}
