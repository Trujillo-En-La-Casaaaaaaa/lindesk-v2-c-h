"""PostgreSQL adapter for the outbox (component: ``outbox``).

Two responsibilities, one table:

* ``insert`` - the write side used by the order repository. It runs on the caller's
  session, so an event row and the status change that produced it are committed or
  rolled back together ("the status change and its outbox row are always written in
  one transaction");
* ``fetch_unpublished`` / ``mark_published`` / ``record_attempt`` - the read side
  used by the publisher relay. ``published_at`` is set only after the broker
  confirmed the message, so a crash in between leaves the row unpublished and the
  event is republished on restart with the **same** ``event_id`` (contracts.md §8:
  at-least-once delivery, consumers deduplicate by ``eventId``).

``ON CONFLICT (event_id) DO NOTHING`` keeps the table idempotent: an event can only
ever be recorded once, no matter how many times the surrounding transaction is
retried.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import OutboxRow
from app.ports.outbox import OutboxMessage, OutboxRecord


class PostgresOutbox:
    """Implements :class:`app.ports.outbox.OutboxStore` and the write side."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # -- write side (used inside the order repository's transaction) --------
    def insert(self, session: Session, message: OutboxMessage) -> None:
        """Record ``message`` in the caller's transaction (idempotent on ``event_id``)."""

        statement = (
            pg_insert(OutboxRow)
            .values(
                id=uuid.uuid4(),
                event_id=uuid.UUID(str(message.event_id)),
                event_type=message.event_type,
                payload=message.payload,
                occurred_at=_aware(message.occurred_at),
                published_at=None,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
        session.execute(statement)

    # -- read side (used by the publisher relay) ---------------------------
    def fetch_unpublished(self, *, limit: int) -> Sequence[OutboxRecord]:
        """Return unpublished rows, oldest first."""

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(OutboxRow)
                    .where(OutboxRow.published_at.is_(None))
                    .order_by(OutboxRow.occurred_at, OutboxRow.id)
                    .limit(max(1, int(limit)))
                )
                .scalars()
                .all()
            )
        return [_record(row) for row in rows]

    def mark_published(self, record_id: str, *, published_at: datetime) -> None:
        """Set ``published_at`` after the broker confirmed the message."""

        with self._session_factory() as session, session.begin():
            session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == uuid.UUID(str(record_id)), OutboxRow.published_at.is_(None))
                .values(published_at=_aware(published_at))
            )

    def record_attempt(self, record_id: str, *, failed_at: datetime) -> int:
        """Increment ``attempts`` of a row whose publish did not get confirmed."""

        del failed_at  # the failure instant is carried by the log, not by the schema
        with self._session_factory() as session, session.begin():
            attempts = session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == uuid.UUID(str(record_id)))
                .values(attempts=OutboxRow.attempts + 1)
                .returning(OutboxRow.attempts)
            ).scalar_one()
        return int(attempts)

    def count_unpublished(self) -> int:
        """How many rows are still waiting to be published."""

        with self._session_factory() as session:
            return int(
                session.execute(
                    select(func.count()).select_from(OutboxRow).where(OutboxRow.published_at.is_(None))
                ).scalar_one()
            )


def _record(row: OutboxRow) -> OutboxRecord:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return OutboxRecord(
        record_id=str(row.id),
        message=OutboxMessage(
            event_id=str(row.event_id),
            event_type=row.event_type,
            payload=payload,
            occurred_at=_aware(row.occurred_at),
        ),
        attempts=int(row.attempts),
        published_at=None,
    )


def _aware(value: datetime) -> datetime:
    """Return a timezone-aware UTC timestamp (the column is ``timestamptz``)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["PostgresOutbox"]
