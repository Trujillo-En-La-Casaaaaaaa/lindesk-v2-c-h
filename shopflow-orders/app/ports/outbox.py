"""The outbox and broker ports (contracts.md §8, HANDOFF #6).

``OutboxMessage`` is the durable intent: one event, with the stable ``eventId``
that makes at-least-once delivery safe (contracts.md §8: consumers deduplicate by
``eventId``). ``OutboxStore`` is what the publisher relay needs; ``MessageBroker``
is the broker side, and a broker failure is reported as
:class:`BrokerUnavailable` so the relay can leave the row unpublished instead of
losing the event.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.domain.errors import InternalError
from app.domain.order import utc_now


class BrokerUnavailable(InternalError):
    """The broker did not accept (or confirm) a publish right now."""

    def __init__(
        self,
        message: str = "The message broker is unavailable.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """One event of the transactional outbox.

    ``event_id`` is generated when the row is written and never changes, so every
    retry after a crash republishes *the same* event rather than a new one.
    """

    event_id: str
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime = field(default_factory=utc_now)

    @property
    def routing_key(self) -> str:
        """The routing key of contracts.md §8 equals the event type."""

        return self.event_type

    @classmethod
    def new(
        cls,
        event_type: str,
        payload: dict[str, Any],
        *,
        occurred_at: datetime,
        event_id: str | None = None,
    ) -> OutboxMessage:
        """Build a message, generating the ``eventId`` when none is supplied."""

        resolved_id = event_id or str(uuid.uuid4())
        body = dict(payload)
        body.setdefault("eventId", resolved_id)
        return cls(event_id=resolved_id, event_type=event_type, payload=body, occurred_at=occurred_at)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """One unpublished outbox row as the relay sees it."""

    record_id: str
    message: OutboxMessage
    attempts: int = 0
    published_at: datetime | None = None


@runtime_checkable
class OutboxStore(Protocol):
    """The publisher-facing side of the outbox table."""

    def fetch_unpublished(self, *, limit: int) -> Sequence[OutboxRecord]:
        """Return unpublished rows, oldest first."""

    def mark_published(self, record_id: str, *, published_at: datetime) -> None:
        """Record that the broker confirmed the message."""

    def record_attempt(self, record_id: str, *, failed_at: datetime) -> int:
        """Increment ``attempts`` of a failed publish and return the new count."""

    def count_unpublished(self) -> int:
        """How many rows are still waiting (used by diagnostics and tests)."""


@runtime_checkable
class MessageBroker(Protocol):
    """The publisher side of contracts.md §8."""

    def publish(self, message: OutboxMessage) -> None:
        """Publish ``message`` to ``shopflow.orders`` with publisher confirms.

        Raises :class:`BrokerUnavailable` when the message was not confirmed, so
        the caller keeps the outbox row unpublished and retries later.
        """

    def is_reachable(self) -> bool:
        """``True`` when the broker accepts a connection (used by ``/ready``)."""

    def close(self) -> None:
        """Release the connection (called on shutdown)."""


__all__ = [
    "BrokerUnavailable",
    "MessageBroker",
    "OutboxMessage",
    "OutboxRecord",
    "OutboxStore",
]
