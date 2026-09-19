"""SQLAlchemy rows of the owned notifications store (contracts.md §9).

The shape below is the authoritative schema of contracts.md §9, "Notifications
store (owner: shopflow-notifications)" - two tables and nothing else:

* ``notifications`` - one row per (order, notification type), the unique
  constraint being the logical half of exactly-once delivery;
* ``processed_events`` - the deduplication state per consumed ``eventId``.

Only this repository may create, migrate or write these tables. The ORM classes
live in ``app/db``; the domain never sees them (the adapter maps rows onto the
domain records), which is why ``app/domain`` and ``app/handlers`` do not import
SQLAlchemy (acceptance criterion 8).

The Alembic migration declares the same tables, columns and constraint names, so
the migrated schema and the ORM metadata agree by construction - the integration
suite asserts exactly that against the live store.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NOTIFICATIONS_TABLE = "notifications"
PROCESSED_EVENTS_TABLE = "processed_events"

#: contracts.md §9: ``unique (order_id, notification_type)``.
ORDER_TYPE_UNIQUE_CONSTRAINT = "uq_notifications_order_type"
#: contracts.md §9: ``check (notification_type in ('confirmation','cancellation'))``.
NOTIFICATION_TYPE_CHECK = "ck_notifications_notification_type"
#: contracts.md §9: ``check (status in ('SENT','FAILED'))``.
STATUS_CHECK = "ck_notifications_status"
NOTIFICATIONS_PK = "pk_notifications"
PROCESSED_EVENTS_PK = "pk_processed_events"


class Base(DeclarativeBase):
    """Declarative base of the notifications store."""


class NotificationRow(Base):
    """``notifications``: one row per ``(order_id, notification_type)``.

    ``notifications(id, order_id, notification_type, event_id, status,
    provider_message_id, attempts, created_at, updated_at)`` per contracts.md §9.
    """

    __tablename__ = NOTIFICATIONS_TABLE
    __table_args__ = (
        PrimaryKeyConstraint("id", name=NOTIFICATIONS_PK),
        UniqueConstraint("order_id", "notification_type", name=ORDER_TYPE_UNIQUE_CONSTRAINT),
        CheckConstraint("notification_type in ('confirmation', 'cancellation')", name=NOTIFICATION_TYPE_CHECK),
        CheckConstraint("status in ('SENT', 'FAILED')", name=STATUS_CHECK),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), nullable=False)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    notification_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessedEventRow(Base):
    """``processed_events(event_id, processed_at)`` - the deduplication state."""

    __tablename__ = PROCESSED_EVENTS_TABLE
    __table_args__ = (PrimaryKeyConstraint("event_id", name=PROCESSED_EVENTS_PK),)

    event_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "Base",
    "NOTIFICATION_TYPE_CHECK",
    "NOTIFICATIONS_PK",
    "NOTIFICATIONS_TABLE",
    "NotificationRow",
    "ORDER_TYPE_UNIQUE_CONSTRAINT",
    "PROCESSED_EVENTS_PK",
    "PROCESSED_EVENTS_TABLE",
    "ProcessedEventRow",
    "STATUS_CHECK",
]
