"""Outbox publisher relay (component: ``outbox-publisher``, HANDOFF #6).

Polls the ``outbox`` table for unpublished rows, publishes each one to the durable
topic exchange ``shopflow.orders`` with event type ``order.confirmed`` /
``order.cancelled`` as a persistent message with publisher confirms, and only then
sets ``published_at``. A failed publish increments ``attempts``, leaves the row
unpublished and backs off, so

* a broker outage never loses an event: the rows (and therefore the events) stay in
  the owned store and are republished when the broker is back;
* a crash between the broker's confirmation and the ``published_at`` write does not
  lose the event either: the row is still unpublished, and the republish carries the
  same ``eventId``, which is what contracts.md §8 relies on ("delivery is
  at-least-once; consumers deduplicate by ``eventId``");
* a restart is safe at any moment, because all progress lives in the store - the
  relay keeps no state in memory.

The module is also a standalone entry point (``python -m app.workers.outbox_publisher``);
inside shopflow-infra it runs as a background task of the service process.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.order import utc_now
from app.ports.outbox import BrokerUnavailable, MessageBroker, OutboxStore

LOGGER = logging.getLogger("shopflow.orders.outbox_publisher")


@dataclass(frozen=True, slots=True)
class PublishReport:
    """What one relay pass did."""

    published: int = 0
    failed: int = 0
    pending: int = 0
    failure: str | None = None

    @property
    def healthy(self) -> bool:
        return self.failure is None


class OutboxPublisher:
    """Relays the transactional outbox to the broker."""

    def __init__(
        self,
        *,
        store: OutboxStore,
        broker: MessageBroker,
        batch_size: int = 50,
        poll_interval_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        now: Any = utc_now,
    ) -> None:
        self._store = store
        self._broker = broker
        self._batch_size = max(1, int(batch_size))
        self._poll_interval = max(0.01, float(poll_interval_seconds))
        self._max_backoff = max(self._poll_interval, float(max_backoff_seconds))
        self._now = now

    @property
    def poll_interval_seconds(self) -> float:
        return self._poll_interval

    def run_once(self) -> PublishReport:
        """Publish one batch of unpublished rows, oldest first."""

        published = 0
        failed = 0
        failure: str | None = None
        records = self._store.fetch_unpublished(limit=self._batch_size)
        for record in records:
            try:
                self._broker.publish(record.message)
            except BrokerUnavailable as exc:
                failed += 1
                failure = str(exc)
                self._store.record_attempt(record.record_id, failed_at=self._now())
                LOGGER.warning(
                    "publish failed; the outbox row stays unpublished",
                    extra={
                        "eventId": record.message.event_id,
                        "eventType": record.message.event_type,
                        "error": failure,
                    },
                )
                break  # do not skip ahead: every event keeps its order
            except Exception as exc:  # noqa: BLE001 - an unexpected broker client error is still a failure
                failed += 1
                failure = f"{type(exc).__name__}: {exc}"
                self._store.record_attempt(record.record_id, failed_at=self._now())
                LOGGER.exception(
                    "unexpected publish failure; the outbox row stays unpublished",
                    extra={"eventId": record.message.event_id, "eventType": record.message.event_type},
                )
                break
            self._store.mark_published(record.record_id, published_at=self._now())
            published += 1
            LOGGER.info(
                "event published",
                extra={
                    "eventId": record.message.event_id,
                    "eventType": record.message.event_type,
                    "attempts": record.attempts,
                },
            )

        return PublishReport(
            published=published,
            failed=failed,
            pending=self._store.count_unpublished(),
            failure=failure,
        )

    def drain(self, *, max_batches: int = 100) -> int:
        """Publish until no unpublished row is left (bounded), and return the count.

        Used by the tests and by an operator who wants the outbox flushed now.
        """

        published = 0
        for _ in range(max(1, max_batches)):
            report = self.run_once()
            published += report.published
            if report.published == 0:
                break
        return published

    def run_forever(self, stop_event: threading.Event) -> int:
        """Relay until ``stop_event`` is set; returns how many events were published."""

        published_total = 0
        consecutive_failures = 0
        while not stop_event.is_set():
            report = self.run_once()
            published_total += report.published
            if report.healthy:
                consecutive_failures = 0
                delay = self._poll_interval
            else:
                consecutive_failures += 1
                delay = min(self._max_backoff, self._poll_interval * (2 ** (consecutive_failures - 1)))
                LOGGER.warning(
                    "outbox relay backing off",
                    extra={"delaySeconds": delay, "pending": report.pending, "error": report.failure},
                )
            stop_event.wait(delay)
        return published_total

    def close(self) -> None:
        """Close the broker connection."""

        self._broker.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone relay: ``python -m app.workers.outbox_publisher``."""

    del argv
    from app.adapters.postgres_outbox import PostgresOutbox
    from app.adapters.rabbitmq_publisher import RabbitMqPublisher
    from app.config import load_settings
    from app.db.session import create_engine_from_settings, create_session_factory

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    store = PostgresOutbox(create_session_factory(engine))
    broker = RabbitMqPublisher(
        settings.broker_url,
        exchange=settings.broker_exchange,
        connect_timeout_seconds=settings.broker_connect_timeout_seconds,
    )
    publisher = OutboxPublisher(
        store=store,
        broker=broker,
        batch_size=settings.outbox_batch_size,
        poll_interval_seconds=settings.outbox_poll_interval_seconds,
        max_backoff_seconds=settings.outbox_max_backoff_seconds,
    )
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    try:
        publisher.run_forever(stop_event)
    finally:
        publisher.close()
        engine.dispose()
    return 0


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, name, None)
        if candidate is None:
            continue
        try:
            signal.signal(candidate, _stop)
        except ValueError:  # pragma: no cover - only outside the main thread
            continue


__all__ = ["OutboxPublisher", "PublishReport", "main"]


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
