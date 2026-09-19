"""Release reconciler (component: ``release-reconciler``, HANDOFF #7).

Two durable progresses are repaired here, both from the owned store, so a restart
at any moment continues where the process left off:

1. **pending inventory releases** - every order with
   ``status='CANCELLED' AND inventory_release_state='PENDING'`` is retried with the
   same ``Idempotency-Key: order:{orderId}:release`` until the inventory reports the
   release, and only then is the state moved to ``RELEASED``. The inventory's
   release command is idempotent and never fails for an order it never reserved
   (contracts.md §4), so an ambiguous failure can be retried indefinitely. Stock is
   restored exactly once even though the call may be attempted many times.
2. **ambiguous reservations** - an order left in ``CREATED`` by a crash or a
   timeout between the order commit and the reservation is retried with the same
   ``Idempotency-Key: order:{orderId}:reserve``. A replayed reservation returns
   ``reserved: true`` without touching stock a second time; a lost race moves the
   order to ``FAILED``; an unreachable inventory leaves it ``CREATED``. The order
   becomes ``CONFIRMED`` only after a reservation is *known* to have succeeded, and
   the ``order.confirmed`` row is written by the same guarded update, so exactly one
   event is ever recorded.

The reconciler only looks at orders older than ``RESERVATION_GRACE_SECONDS``, so it
never races the request that is currently creating one.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.domain.errors import DomainError, InsufficientStock, UnknownProduct
from app.domain.order import (
    InventoryReleaseState,
    Order,
    RequestedLine,
    utc_now,
)
from app.events.contracts import order_confirmed_event
from app.ports.inventory import InventoryPort
from app.ports.order_repository import OrderRepository

LOGGER = logging.getLogger("shopflow.orders.release_reconciler")


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one reconciliation pass did."""

    released: int = 0
    pending_releases: int = 0
    confirmed: int = 0
    failed: int = 0
    pending_reservations: int = 0

    def __add__(self, other: ReconcileReport) -> ReconcileReport:
        return ReconcileReport(
            released=self.released + other.released,
            pending_releases=self.pending_releases + other.pending_releases,
            confirmed=self.confirmed + other.confirmed,
            failed=self.failed + other.failed,
            pending_reservations=self.pending_reservations + other.pending_reservations,
        )


class ReleaseReconciler:
    """Repairs the pending side effects of the order lifecycle."""

    def __init__(
        self,
        *,
        repository: OrderRepository,
        inventory: InventoryPort,
        interval_seconds: float = 5.0,
        batch_size: int = 50,
        reservation_grace_seconds: float = 30.0,
        timeout_seconds: float | None = None,
        now: Any = utc_now,
    ) -> None:
        self._repository = repository
        self._inventory = inventory
        self._interval = max(0.01, float(interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._grace = max(0.0, float(reservation_grace_seconds))
        self._timeout = timeout_seconds
        self._now = now

    def run_once(self) -> ReconcileReport:
        """One pass over the pending releases and the ambiguous reservations."""

        report = self._reconcile_releases()
        return report + self._reconcile_reservations()

    def run_forever(self, stop_event: threading.Event) -> ReconcileReport:
        """Reconcile until ``stop_event`` is set; returns the accumulated report."""

        total = ReconcileReport()
        while not stop_event.is_set():
            report = self.run_once()
            total = total + report
            stop_event.wait(self._interval)
        return total

    # -- releases -----------------------------------------------------------
    def _reconcile_releases(self) -> ReconcileReport:
        released = 0
        pending = 0
        orders = self._repository.find_pending_releases(limit=self._batch_size)
        for order in orders:
            if self._release(order):
                released += 1
            else:
                pending += 1
        if orders:
            LOGGER.info(
                "pending releases reconciled",
                extra={"released": released, "pending": pending},
            )
        return ReconcileReport(released=released, pending_releases=pending)

    def _release(self, order: Order) -> bool:
        lines = _requested_lines(order)
        try:
            self._inventory.release(order.order_id, lines, timeout_seconds=self._timeout)
        except DomainError as exc:
            LOGGER.warning(
                "release still pending",
                extra={"orderId": order.order_id, "error": str(exc)},
            )
            return False
        self._repository.set_release_state(order.order_id, state=InventoryReleaseState.RELEASED)
        return True

    # -- reservations -------------------------------------------------------
    def _reconcile_reservations(self) -> ReconcileReport:
        confirmed = 0
        failed = 0
        pending = 0
        cutoff = self._now() - timedelta(seconds=self._grace)
        orders = self._repository.find_pending_reservations(limit=self._batch_size, older_than=cutoff)
        for order in orders:
            outcome = self._retry_reservation(order)
            if outcome == "confirmed":
                confirmed += 1
            elif outcome == "failed":
                failed += 1
            else:
                pending += 1
        if orders:
            LOGGER.info(
                "ambiguous reservations reconciled",
                extra={"confirmed": confirmed, "failed": failed, "pending": pending},
            )
        return ReconcileReport(confirmed=confirmed, failed=failed, pending_reservations=pending)

    def _retry_reservation(self, order: Order) -> str:
        lines = _requested_lines(order)
        try:
            self._inventory.reserve(order.order_id, lines, timeout_seconds=self._timeout)
        except (InsufficientStock, UnknownProduct) as exc:
            # The reservation can never succeed for this order: make it terminal.
            self._repository.mark_failed(order.order_id, failed_at=self._now())
            LOGGER.info(
                "reservation retry rejected; order failed",
                extra={"orderId": order.order_id, "error": str(exc)},
            )
            return "failed"
        except DomainError as exc:
            LOGGER.warning(
                "reservation retry still pending",
                extra={"orderId": order.order_id, "error": str(exc)},
            )
            return "pending"

        confirmed_at = self._now()
        confirmed = order.confirmed(at=confirmed_at)
        event = order_confirmed_event(confirmed, occurred_at=confirmed_at)
        if self._repository.mark_confirmed(order.order_id, confirmed_at=confirmed_at, event=event):
            return "confirmed"
        # Another writer confirmed it first; its guarded update owns the event row.
        return "pending"


def _requested_lines(order: Order) -> list[RequestedLine]:
    return [RequestedLine(product_id=line.product_id, quantity=line.quantity) for line in order.lines]


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone reconciler: ``python -m app.workers.release_reconciler``."""

    del argv
    from app.adapters.inventory_http_client import InventoryHttpClient
    from app.adapters.postgres_order_repository import PostgresOrderRepository
    from app.adapters.postgres_outbox import PostgresOutbox
    from app.config import load_settings
    from app.db.session import create_engine_from_settings, create_session_factory

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    inventory = InventoryHttpClient(
        settings.inventory_base_url,
        timeout_seconds=settings.request_timeout_seconds,
    )
    reconciler = ReleaseReconciler(
        repository=repository,
        inventory=inventory,
        interval_seconds=settings.release_retry_interval_seconds,
        batch_size=settings.reconciler_batch_size,
        reservation_grace_seconds=settings.reservation_grace_seconds,
        timeout_seconds=settings.request_timeout_seconds,
    )
    stop_event = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, name, None)
        if candidate is None:
            continue
        try:
            signal.signal(candidate, lambda *_args: stop_event.set())
        except ValueError:  # pragma: no cover - only outside the main thread
            continue
    try:
        reconciler.run_forever(stop_event)
    finally:
        inventory.close()
        engine.dispose()
    return 0


__all__ = ["ReconcileReport", "ReleaseReconciler", "main"]


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
