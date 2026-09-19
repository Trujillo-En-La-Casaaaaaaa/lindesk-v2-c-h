"""Inventory store schema: products, stock, stock_movements (contracts.md §9).

Revision ID: 0001_inventory_schema
Revises:
Create Date: 2026-09-18

This migration is owned by ``shopflow-inventory`` and creates the entire store of
the service. Two invariants are enforced by the database itself, so no writer can
break them:

* ``stock.quantity >= 0`` - the service never oversells, even if a bug or a
  concurrent writer bypassed the application logic;
* ``unique(order_id, operation)`` on ``stock_movements`` - a reservation or a
  release is recorded at most once per order, which makes both commands
  idempotent across restarts and concurrent retries.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_inventory_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'USD'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("sku", name="uq_products_sku"),
    )

    op.create_table(
        "stock",
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("product_id", name="pk_stock"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_stock_product_id_products"),
        sa.CheckConstraint("quantity >= 0", name="ck_stock_quantity_non_negative"),
    )

    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_stock_movements"),
        sa.CheckConstraint("operation in ('reserve', 'release')", name="ck_stock_movements_operation"),
        sa.UniqueConstraint("order_id", "operation", name="uq_stock_movements_order_operation"),
    )


def downgrade() -> None:
    op.drop_table("stock_movements")
    op.drop_table("stock")
    op.drop_table("products")
