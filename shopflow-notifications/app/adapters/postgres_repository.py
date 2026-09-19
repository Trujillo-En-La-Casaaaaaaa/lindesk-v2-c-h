"""PostgreSQL adapter of the notification-repository-port.

Component: ``postgres-notification-repository``. It maps the rows of
``app.db.models`` (the contracts.md §9 schema) onto the domain records and back,
and it is the only place in this repository that issues SQL for the notifications
store.

Both deduplication guards are enforced by the store, not by application code:

* ``processed_events.event_id`` is the primary key;
* ``unique (order_id, notification_type)`` on ``notifications``.

The adapter reaches for them with ``ON CONFLICT DO NOTHING`` plus ``RETURNING``,
which makes the outcome race-free: if two deliveries of the same logical
notification arrive at the same moment, exactly one insert returns a row and the
other is reported as :class:`DuplicateNotification` with nothing written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import NotificationRow, ProcessedEventRow
from app.domain.notification import (
    NotificationRecord,
    NotificationStatus,
    NotificationType,
    as_utc,
)
from app.ports.repository import DuplicateNotification, RepositoryError

#: The contracts.md §9 schema allows NULL timestamps; a record still needs an instant.
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

NOTIFICATION_COLUMNS = ("order_id", "notification_type")
EVENT_COLUMNS = ("event_id",)


def _column_value(value: Any) -> str:
    """Store an enum member by its contracted value (never ``str(Enum)``)."""

    return str(getattr(value, "value", value))


def _timestamp(value: datetime | None) -> datetime:
    return as_utc(value) if value is not None else UNIX_EPOCH


class PostgresNotificationRepository:
    """The only writable owner of ``notifications`` and ``processed_events``."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def event_processed(self, event_id: str) -> bool:
        statement = select(ProcessedEventRow.event_id).where(ProcessedEventRow.event_id == event_id)
        try:
            with self._session_factory() as session:
                return session.execute(statement).first() is not None
        except SQLAlchemyError as exc:  # pragma: no cover - only when the store is down
            raise RepositoryError(f"could not read processed_events for event '{event_id}'") from exc

    def find_by_order_and_type(
        self,
        order_id: str,
        notification_type: NotificationType,
    ) -> NotificationRecord | None:
        statement = select(NotificationRow).where(
            NotificationRow.order_id == order_id,
            NotificationRow.notification_type == _column_value(notification_type),
        )
        try:
            with self._session_factory() as session:
                row = session.execute(statement).scalars().one_or_none()
        except SQLAlchemyError as exc:  # pragma: no cover - only when the store is down
            raise RepositoryError(
                f"could not read notifications for order '{order_id}' ({_column_value(notification_type)})"
            ) from exc
        return None if row is None else self._record_of(row)

    def list_by_order(self, order_id: str) -> list[NotificationRecord]:
        statement = (
            select(NotificationRow)
            .where(NotificationRow.order_id == order_id)
            .order_by(NotificationRow.created_at, NotificationRow.notification_type)
        )
        try:
            with self._session_factory() as session:
                rows = session.execute(statement).scalars().all()
        except SQLAlchemyError as exc:  # pragma: no cover - only when the store is down
            raise RepositoryError(f"could not list notifications for order '{order_id}'") from exc
        return [self._record_of(row) for row in rows]

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def commit_notification(
        self,
        record: NotificationRecord,
        *,
        event_id: str,
        processed_at: datetime,
    ) -> NotificationRecord:
        """Insert the notification and mark ``event_id`` processed, in one transaction."""

        notification_statement = (
            pg_insert(NotificationRow)
            .values(
                id=record.notification_id,
                order_id=record.order_id,
                notification_type=_column_value(record.notification_type),
                event_id=event_id,
                status=_column_value(record.status),
                provider_message_id=record.provider_message_id,
                attempts=record.attempts,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            .on_conflict_do_nothing(index_elements=list(NOTIFICATION_COLUMNS))
            .returning(NotificationRow.id)
        )
        event_statement = (
            pg_insert(ProcessedEventRow)
            .values(event_id=event_id, processed_at=processed_at)
            .on_conflict_do_nothing(index_elements=list(EVENT_COLUMNS))
        )
        try:
            with self._session_factory() as session, session.begin():
                inserted = session.execute(notification_statement).scalar_one_or_none()
                session.execute(event_statement)
        except SQLAlchemyError as exc:
            raise RepositoryError(
                f"could not commit the notification for order '{record.order_id}' "
                f"({_column_value(record.notification_type)})"
            ) from exc

        if inserted is None:
            raise DuplicateNotification(record.order_id, record.notification_type)
        return record

    def mark_event_processed(self, event_id: str, processed_at: datetime) -> None:
        statement = (
            pg_insert(ProcessedEventRow)
            .values(event_id=event_id, processed_at=processed_at)
            .on_conflict_do_nothing(index_elements=list(EVENT_COLUMNS))
        )
        try:
            with self._session_factory() as session, session.begin():
                session.execute(statement)
        except SQLAlchemyError as exc:  # pragma: no cover - only when the store is down
            raise RepositoryError(f"could not mark event '{event_id}' processed") from exc

    # ------------------------------------------------------------------ #
    # mapping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _record_of(row: NotificationRow) -> NotificationRecord:
        return NotificationRecord(
            notification_id=str(row.id),
            order_id=row.order_id,
            notification_type=NotificationType(row.notification_type),
            event_id=str(row.event_id),
            status=NotificationStatus(row.status),
            provider_message_id=row.provider_message_id,
            attempts=int(row.attempts),
            created_at=_timestamp(row.created_at),
            updated_at=_timestamp(row.updated_at),
        )


__all__ = ["PostgresNotificationRepository"]
