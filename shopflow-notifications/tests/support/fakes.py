"""In-process doubles of the two ports, used by the unit and contract suites.

These are *test doubles*: the production composition root (``app.main``) only ever
wires the real adapters, and no runtime path can fall back to this module. They
live in the test tree on purpose, so the unit and contract suites run without a
database, a broker or an HTTP provider while the integration and failure suites
prove the real behaviour against PostgreSQL, RabbitMQ and the provider emulator.

The repository double mirrors the guarantees the real adapter gets from the store:

* ``(order_id, notification_type)`` is unique, so a second logical notification is
  detected at the store, not in the handler;
* ``event_id`` is a set, so a replayed event is detected at the store;
* both checks happen under one lock, like the single transaction of the adapter.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy.exc import OperationalError

from app.adapters.provider_http_client import deterministic_message_id
from app.domain.notification import (
    NotificationRecord,
    NotificationType,
    OutboundMessage,
    ProviderReceipt,
    format_timestamp,
    utc_now,
)
from app.ports.provider import ProviderUnavailable
from app.ports.repository import DuplicateNotification


class FakeNotificationRepository:
    """In-memory double of the notification-repository-port."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], NotificationRecord] = {}
        self._events: dict[str, datetime] = {}

    # -- port surface ------------------------------------------------------
    def event_processed(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._events

    def find_by_order_and_type(
        self,
        order_id: str,
        notification_type: NotificationType,
    ) -> NotificationRecord | None:
        with self._lock:
            return self._records.get((order_id, _value(notification_type)))

    def list_by_order(self, order_id: str) -> list[NotificationRecord]:
        with self._lock:
            records = [record for key, record in self._records.items() if key[0] == order_id]
        return sorted(records, key=lambda record: (record.created_at, record.notification_type.value))

    def commit_notification(
        self,
        record: NotificationRecord,
        *,
        event_id: str,
        processed_at: datetime,
    ) -> NotificationRecord:
        with self._lock:
            key = (record.order_id, record.notification_type.value)
            if key in self._records:
                self._events.setdefault(event_id, processed_at)
                raise DuplicateNotification(record.order_id, record.notification_type)
            self._records[key] = record
            self._events.setdefault(event_id, processed_at)
        return record

    def mark_event_processed(self, event_id: str, processed_at: datetime) -> None:
        with self._lock:
            self._events.setdefault(event_id, processed_at)

    # -- test helpers ------------------------------------------------------
    @property
    def records(self) -> list[NotificationRecord]:
        with self._lock:
            return list(self._records.values())

    def record_for(self, order_id: str, notification_type: str) -> NotificationRecord | None:
        with self._lock:
            return self._records.get((order_id, notification_type))

    @property
    def processed_event_ids(self) -> set[str]:
        with self._lock:
            return set(self._events)

    def seed(
        self,
        *,
        order_id: str,
        notification_type: NotificationType,
        event_id: str,
        status: str = "SENT",
        provider_message_id: str | None = "msg-seeded",
        attempts: int = 1,
        created_at: datetime | None = None,
    ) -> NotificationRecord:
        """Insert a record directly, to set up a duplicate-delivery scenario."""

        from app.domain.notification import NotificationStatus

        now = created_at or utc_now()
        record = NotificationRecord(
            notification_id=str(uuid.uuid4()),
            order_id=order_id,
            notification_type=notification_type,
            event_id=event_id,
            status=NotificationStatus(status),
            provider_message_id=provider_message_id,
            attempts=attempts,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._records[(order_id, notification_type.value)] = record
        return record


class RecordingProvider:
    """Double of the provider-port that records messages and can fail on demand."""

    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self._lock = threading.RLock()
        self._failures_left = failures
        self._error = error or ProviderUnavailable("provider is unavailable")
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> ProviderReceipt:
        with self._lock:
            self.sent.append(message)
            if self._failures_left > 0:
                self._failures_left -= 1
                raise self._error
        return ProviderReceipt(
            message_id=deterministic_message_id(message.template, message.order_id),
            status="accepted",
            accepted_at=format_timestamp(utc_now()),
        )

    def count(self, template: str | None = None, order_id: str | None = None) -> int:
        with self._lock:
            return len(
                [
                    message
                    for message in self.sent
                    if (template is None or message.template == template)
                    and (order_id is None or message.order_id == order_id)
                ]
            )

    def templates(self, order_id: str | None = None) -> list[str]:
        with self._lock:
            return [
                message.template
                for message in self.sent
                if order_id is None or message.order_id == order_id
            ]

    @property
    def messages(self) -> list[OutboundMessage]:
        with self._lock:
            return list(self.sent)


def _value(notification_type: object) -> str:
    return str(getattr(notification_type, "value", notification_type))


def order_ids(records: Iterable[NotificationRecord]) -> list[str]:
    return [record.order_id for record in records]


class _StubConnection:
    """Minimal connection double for the readiness probe."""

    def __enter__(self) -> _StubConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


class HealthyEngine:
    """An engine double whose store always answers (readiness ``200``)."""

    def connect(self) -> _StubConnection:
        return _StubConnection()

    def dispose(self) -> None:
        return None


class UnreachableEngine:
    """An engine double whose store never answers (readiness ``503``)."""

    def connect(self) -> _StubConnection:
        raise OperationalError("SELECT 1", {}, Exception("notifications store unreachable"))

    def dispose(self) -> None:
        return None


class RecordingProbe:
    """A readiness probe double that records how often it was called."""

    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.reachable


class FakeChannel:
    """Records everything the consumer asks a channel to do (no broker involved)."""

    def __init__(self, *, publish_error: Exception | None = None) -> None:
        self.exchanges: list[dict[str, Any]] = []
        self.queues: list[dict[str, Any]] = []
        self.bindings: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []
        self.purged: list[str] = []
        self.qos: int | None = None
        self.confirms = False
        self.consume: dict[str, Any] | None = None
        self.started = False
        self.stopped = False
        self._publish_error = publish_error

    # -- declaration -------------------------------------------------------
    def exchange_declare(self, **kwargs: Any) -> None:
        self.exchanges.append(kwargs)

    def queue_declare(self, **kwargs: Any) -> Any:
        self.queues.append(kwargs)
        return type("Declared", (), {"method": type("Method", (), {"message_count": 0})()})()

    def queue_bind(self, **kwargs: Any) -> None:
        self.bindings.append(kwargs)

    def queue_purge(self, **kwargs: Any) -> None:
        self.purged.append(str(kwargs.get("queue")))

    # -- consuming ---------------------------------------------------------
    def basic_qos(self, prefetch_count: int = 0, **kwargs: Any) -> None:
        self.qos = prefetch_count

    def confirm_delivery(self, *args: Any, **kwargs: Any) -> bool:
        self.confirms = True
        return True

    def basic_consume(self, **kwargs: Any) -> str:
        self.consume = kwargs
        return "ctag-1"

    def start_consuming(self) -> None:
        self.started = True

    def stop_consuming(self) -> None:
        self.stopped = True

    # -- publishing and acknowledging --------------------------------------
    def basic_publish(self, **kwargs: Any) -> None:
        if self._publish_error is not None:
            raise self._publish_error
        self.published.append(kwargs)

    def basic_ack(self, delivery_tag: int, **kwargs: Any) -> None:
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool = False, **kwargs: Any) -> None:
        self.nacks.append((delivery_tag, requeue))


class FakeConnection:
    """A connection double returning one :class:`FakeChannel`."""

    def __init__(self, channel: FakeChannel | None = None) -> None:
        self._channel = channel or FakeChannel()
        self.closed = False
        self.timers: list[tuple[float, Any]] = []

    @property
    def channel_instance(self) -> FakeChannel:
        return self._channel

    def channel(self) -> FakeChannel:
        return self._channel

    def close(self) -> None:
        self.closed = True

    def call_later(self, delay: float, callback: Any) -> Any:
        self.timers.append((delay, callback))
        return len(self.timers)


class FakeMethod:
    """A delivery's ``method`` frame."""

    def __init__(self, *, routing_key: str, delivery_tag: int = 1, redelivered: bool = False) -> None:
        self.routing_key = routing_key
        self.delivery_tag = delivery_tag
        self.redelivered = redelivered


class FakeProperties:
    """A delivery's ``properties`` frame."""

    def __init__(self, *, headers: dict[str, Any] | None = None, correlation_id: str | None = None) -> None:
        self.headers = headers or {}
        self.correlation_id = correlation_id
        self.content_type = "application/json; charset=utf-8"


__all__ = [
    "FakeChannel",
    "FakeConnection",
    "FakeMethod",
    "FakeNotificationRepository",
    "FakeProperties",
    "HealthyEngine",
    "RecordingProbe",
    "RecordingProvider",
    "UnreachableEngine",
    "order_ids",
]
