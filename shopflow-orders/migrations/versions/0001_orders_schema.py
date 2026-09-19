"""Orders store schema: orders, order_lines, outbox (contracts.md §9).

Revision ID: 0001_orders_schema
Revises:
Create Date: 2026-09-18

This migration is owned by ``shopflow-orders`` and creates the entire store of the
service. Three invariants are enforced by the database itself, so no writer can
break them:

* ``status`` may only hold the five statuses of the order state machine and
  ``inventory_release_state`` only ``NONE``/``PENDING``/``RELEASED`` - a code path
  that tried to invent a status fails at the store instead of silently corrupting
  the lifecycle;
* ``order_lines.quantity > 0`` - an order line is never empty or negative;
* ``outbox.event_id`` is unique - one row per event, so publishing can never
  duplicate the *intent* (delivery is at-least-once, contracts.md §8, and the
  ``eventId`` stays stable across every retry);
* ``cancellation_reason`` is ``varchar(200)`` - the storage layer bounds the
  cancellation reason as well as the domain policy does.

Indexes back the three access patterns of the service: the customer order list,
the pending-release reconciliation and the outbox relay.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import REASON_LENGTH

revision: str = "0001_orders_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "inventory_release_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'NONE'"),
        ),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(REASON_LENGTH), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.CheckConstraint(
            "status in ('CREATED', 'CONFIRMED', 'FAILED', 'SHIPPED', 'CANCELLED')",
            name="ck_orders_status",
        ),
        sa.CheckConstraint(
            "inventory_release_state in ('NONE', 'PENDING', 'RELEASED')",
            name="ck_orders_inventory_release_state",
        ),
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_index("ix_orders_status_release_state", "orders", ["status", "inventory_release_state"])

    op.create_table(
        "order_lines",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("order_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_order_lines"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_order_lines_order_id_orders"),
        sa.CheckConstraint("quantity > 0", name="ck_order_lines_quantity_positive"),
    )
    op.create_index("ix_order_lines_order_id", "order_lines", ["order_id"])

    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
        sa.UniqueConstraint("event_id", name="uq_outbox_event_id"),
    )
    op.create_index("ix_outbox_unpublished", "outbox", ["published_at", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_unpublished", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_order_lines_order_id", table_name="order_lines")
    op.drop_table("order_lines")
    op.drop_index("ix_orders_status_release_state", table_name="orders")
    op.drop_index("ix_orders_customer_id", table_name="orders")
    op.drop_table("orders")
