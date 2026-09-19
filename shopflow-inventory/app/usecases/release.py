"""Release use case: ``release_stock`` (contracts.md §4).

A release is the only thing that restores stock. It is

* **idempotent and durable**: the ``release`` movement row is inserted first,
  guarded by ``unique(order_id, operation)``. A repeated release finds that row
  and returns ``restored: false, replayed: true`` without touching stock, even
  after a restart or an ambiguous failure;
* **all-or-nothing**: the insert guard, the quantity restore and the movement
  record are one transaction;
* **a safe no-op for an order that was never reserved** (``200 restored:false``):
  the guard row is discarded again, so a retry can still restore the stock if the
  order is reserved later. Retries after an ambiguous failure therefore never
  fail and never double-restore;
* **bounded**: the restoration uses the quantities recorded by the reservation,
  so a release can never raise a product above its pre-reservation level, not
  even when the caller retries with a different body.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.domain.errors import IdempotencyConflict
from app.domain.stock import (
    RELEASE,
    RESERVE,
    StockLine,
    StockMovement,
    canonical_lines,
    movement_payload,
    plan_restoration,
    require_idempotency_key,
)
from app.ports.repositories import StockRepository


def release_stock(
    *,
    stock: StockRepository,
    order_id: str,
    lines: Sequence[StockLine],
    idempotency_key: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restore what ``order_id`` reserved, once and only once."""

    require_idempotency_key(order_id=order_id, operation=RELEASE, provided=idempotency_key)
    canonical = canonical_lines(lines)
    payload = movement_payload(canonical)

    movement = StockMovement(
        order_id=order_id,
        operation=RELEASE,
        lines=canonical,
        created_at=now or datetime.now(UTC),
    )

    with stock.transaction() as transaction:
        if not transaction.record_movement(movement):
            existing = transaction.find_movement(order_id, RELEASE)
            transaction.rollback()
            if existing is None or existing.payload() != payload:
                raise IdempotencyConflict(order_id, RELEASE)
            return {"orderId": order_id, "restored": False, "replayed": True}

        reservation = transaction.find_movement(order_id, RESERVE)
        if reservation is None:
            # Never reserved: nothing to restore. Discard the guard row so the
            # order's later reservation can still be released exactly once.
            transaction.rollback()
            return {"orderId": order_id, "restored": False, "replayed": False}

        increments = plan_restoration(reservation.lines, canonical)
        transaction.increment(increments)

    return {"orderId": order_id, "restored": True, "replayed": False}
