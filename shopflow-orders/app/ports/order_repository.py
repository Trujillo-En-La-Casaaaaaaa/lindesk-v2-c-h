"""The order repository port: the writable owner of the order store (contracts.md §9).

Every method that changes a status also records the matching outbox row **inside
the same transaction** ("the status change and its outbox row are always written
in one transaction", HANDOFF *Consistency and failure semantics*). The port
expresses that as a parameter - the use case hands the durable intent to the
repository - so an adapter cannot accidentally commit half of it.

The guarded ``UPDATE ... WHERE id = :id AND status = :expected`` pattern is part
of the port contract: every status method returns ``True`` only when this call
performed the transition. A concurrent duplicate therefore loses the race and
re-reads the row instead of writing a second event.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.order import InventoryReleaseState, Order
from app.ports.outbox import OutboxMessage


@dataclass(frozen=True, slots=True)
class OrderPage:
    """One page of an order list plus the total number of matching orders."""

    items: tuple[Order, ...]
    total: int


@runtime_checkable
class OrderRepository(Protocol):
    """The order store of ``shopflow-orders``."""

    def create(self, order: Order, *, event: OutboxMessage | None = None) -> Order:
        """Persist a new order with its lines (and optionally an event) in one transaction."""

    def get(self, order_id: str) -> Order | None:
        """Return the order with its lines, or ``None`` when it does not exist."""

    def list_by_customer(
        self,
        customer_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> OrderPage:
        """Return one page of a customer's orders plus the total count."""

    def mark_confirmed(self, order_id: str, *, confirmed_at: datetime, event: OutboxMessage) -> bool:
        """``CREATED -> CONFIRMED`` guarded by the current status, with the ``order.confirmed`` row."""

    def mark_failed(self, order_id: str, *, failed_at: datetime) -> bool:
        """``CREATED -> FAILED`` guarded by the current status (no stock remains reserved)."""

    def mark_cancelled(
        self,
        order_id: str,
        *,
        cancelled_at: datetime,
        reason: str,
        event: OutboxMessage,
    ) -> bool:
        """``CONFIRMED -> CANCELLED`` guarded by the current status.

        Stores ``cancelled_at``, the trimmed ``reason`` and
        ``inventory_release_state = 'PENDING'`` together with the ``order.cancelled``
        outbox row, in one transaction.
        """

    def mark_shipped(self, order_id: str, *, shipped_at: datetime) -> bool:
        """``CONFIRMED -> SHIPPED`` guarded by the current status."""

    def set_release_state(self, order_id: str, *, state: InventoryReleaseState) -> bool:
        """Move ``inventory_release_state`` of an order (``PENDING -> RELEASED``)."""

    def find_pending_releases(self, *, limit: int) -> Sequence[Order]:
        """Orders with ``status='CANCELLED' AND inventory_release_state='PENDING'``."""

    def find_pending_reservations(self, *, limit: int, older_than: datetime) -> Sequence[Order]:
        """``CREATED`` orders older than ``older_than``: an ambiguous reservation to retry."""


__all__ = ["OrderPage", "OrderRepository"]
