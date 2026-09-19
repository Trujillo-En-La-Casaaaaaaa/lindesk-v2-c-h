"""SQLAlchemy rows of the inventory store (contracts.md §9).

Only ``shopflow-inventory`` may create, migrate or write these three tables. The
never-negative rule and the movement uniqueness rule are declared here as well as
in the Alembic migration, so the schema and the ORM agree by construction.

The ORM classes stay in ``app/db``; the domain never sees them (the adapters map
rows to domain objects).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PRODUCTS_TABLE = "products"
STOCK_TABLE = "stock"
STOCK_MOVEMENTS_TABLE = "stock_movements"

QUANTITY_NON_NEGATIVE_CONSTRAINT = "ck_stock_quantity_non_negative"
OPERATION_ALLOWED_CONSTRAINT = "ck_stock_movements_operation"
MOVEMENT_UNIQUE_CONSTRAINT = "uq_stock_movements_order_operation"
PRODUCT_SKU_UNIQUE_CONSTRAINT = "uq_products_sku"


class Base(DeclarativeBase):
    """Declarative base of the inventory store."""


class ProductRow(Base):
    """``products(id, sku, name, description, price, currency, created_at, updated_at)``."""

    __tablename__ = PRODUCTS_TABLE
    __table_args__ = (UniqueConstraint("sku", name=PRODUCT_SKU_UNIQUE_CONSTRAINT),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StockRow(Base):
    """``stock(product_id, quantity, updated_at)`` with ``CHECK (quantity >= 0)``."""

    __tablename__ = STOCK_TABLE
    __table_args__ = (CheckConstraint("quantity >= 0", name=QUANTITY_NON_NEGATIVE_CONSTRAINT),)

    product_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{PRODUCTS_TABLE}.id"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StockMovementRow(Base):
    """``stock_movements`` with ``unique(order_id, operation)`` as the idempotency guard."""

    __tablename__ = STOCK_MOVEMENTS_TABLE
    __table_args__ = (
        CheckConstraint("operation in ('reserve', 'release')", name=OPERATION_ALLOWED_CONSTRAINT),
        UniqueConstraint("order_id", "operation", name=MOVEMENT_UNIQUE_CONSTRAINT),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
