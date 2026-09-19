"""Notifications store schema: notifications, processed_events (contracts.md §9).

Revision ID: 0001_notifications_schema
Revises:
Create Date: 2026-09-18

This migration is owned by ``shopflow-notifications`` and creates the entire store
of the service - the two tables of contracts.md §9 and nothing else. Two rules are
enforced by the database itself, so no bug and no concurrent writer can break the
exactly-once guarantee:

* ``unique (order_id, notification_type)`` - an order can never receive a second
  confirmation or a second cancellation, even if two events with different
  ``eventId`` values arrive at the same moment;
* ``processed_events.event_id`` as the primary key - an event that was already
  handled can never be handled (and therefore delivered) twice.

``attempts`` counts the delivery attempts of the recorded notification, so a
``FAILED`` record shows how hard the service tried before dead-lettering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_notifications_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("notification_type", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint("order_id", "notification_type", name="uq_notifications_order_type"),
        sa.CheckConstraint(
            "notification_type in ('confirmation', 'cancellation')",
            name="ck_notifications_notification_type",
        ),
        sa.CheckConstraint("status in ('SENT', 'FAILED')", name="ck_notifications_status"),
    )

    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_processed_events"),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_table("notifications")
