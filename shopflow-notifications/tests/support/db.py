"""Direct store assertions used by the integration and failure suites (evidence, not decoration).

The suites read ``notifications`` and ``processed_events`` straight from
PostgreSQL, so an assertion about exactly-once delivery is checked against the
durable state and not only against an in-process object.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import NotificationRow, ProcessedEventRow


def truncate_store(session_factory: sessionmaker[Session]) -> None:
    """Empty the notifications store between tests."""

    with session_factory() as session, session.begin():
        session.execute(text("TRUNCATE TABLE notifications, processed_events"))


def notification_rows(
    session_factory: sessionmaker[Session],
    *,
    order_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read ``notifications`` straight from the store, oldest first."""

    statement = select(NotificationRow).order_by(NotificationRow.created_at, NotificationRow.notification_type)
    if order_id is not None:
        statement = statement.where(NotificationRow.order_id == order_id)
    with session_factory() as session:
        rows = session.execute(statement).scalars().all()
    return [
        {
            "id": str(row.id),
            "orderId": row.order_id,
            "notificationType": row.notification_type,
            "eventId": str(row.event_id),
            "status": row.status,
            "providerMessageId": row.provider_message_id,
            "attempts": int(row.attempts),
            "createdAt": row.created_at,
            "updatedAt": row.updated_at,
        }
        for row in rows
    ]


def processed_event_ids(session_factory: sessionmaker[Session]) -> set[str]:
    """Read the deduplication state straight from the store."""

    with session_factory() as session:
        rows = session.execute(select(ProcessedEventRow.event_id)).scalars().all()
    return {str(row) for row in rows}


def count_notifications(session_factory: sessionmaker[Session], *, order_id: str) -> int:
    return len(notification_rows(session_factory, order_id=order_id))


__all__ = [
    "count_notifications",
    "notification_rows",
    "processed_event_ids",
    "truncate_store",
]
