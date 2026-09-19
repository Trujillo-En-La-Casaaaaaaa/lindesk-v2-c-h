"""SQLAlchemy rows of the orders store (contracts.md §9).

Only ``shopflow-orders`` may create, migrate or write these three tables. The
status machine, the release-state vocabulary and the never-empty quantity rule are
declared here as database constraints as well as in the Alembic migration, so the
store itself refuses an invalid state even if application code were bypassed.

The ORM classes stay in ``app/db``; the domain never sees them (adapters map rows
to the domain objects of ``app/domain/order.py``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ORDERS_TABLE = "orders"
ORDER_LINES_TABLE = "order_lines"
OUTBOX_TABLE = "outbox"

#: contracts.md §9: the five statuses and the three release states.
STATUS_VALUES: tuple[str, ...] = ("CREATED", "CONFIRMED", "FAILED", "SHIPPED", "CANCELLED")
RELEASE_STATE_VALUES: tuple[str, ...] = ("NONE", "PENDING", "RELEASED")

STATUS_ALLOWED_CONSTRAINT = "ck_orders_status"
RELEASE_STATE_ALLOWED_CONSTRAINT = "ck_orders_inventory_release_state"
LINE_QUANTITY_POSITIVE_CONSTRAINT = "ck_order_lines_quantity_positive"
OUTBOX_EVENT_ID_UNIQUE_CONSTRAINT = "uq_outbox_event_id"

CUSTOMER_INDEX = "ix_orders_customer_id"
PENDING_RELEASE_INDEX = "ix_orders_status_release_state"
OUTBOX_UNPUBLISHED_INDEX = "ix_outbox_unpublished"

#: contracts.md §9 limits the stored cancellation reason to 200 characters.
REASON_LENGTH = 200


def status_check_sql() -> str:
    values = ", ".join(f"'{value}'" for value in STATUS_VALUES)
    return f"status in ({values})"


def release_state_check_sql() -> str:
    values = ", ".join(f"'{value}'" for value in RELEASE_STATE_VALUES)
    return f"inventory_release_state in ({values})"


class Base(DeclarativeBase):
    """Declarative base of the orders store."""


class OrderRow(Base):
    """``orders`` (contracts.md §9)."""

    __tablename__ = ORDERS_TABLE
    __table_args__ = (
        CheckConstraint(status_check_sql(), name=STATUS_ALLOWED_CONSTRAINT),
        CheckConstraint(release_state_check_sql(), name=RELEASE_STATE_ALLOWED_CONSTRAINT),
        Index(CUSTOMER_INDEX, "customer_id"),
        Index(PENDING_RELEASE_INDEX, "status", "inventory_release_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_release_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'NONE'")
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(REASON_LENGTH))


class OrderLineRow(Base):
    """``order_lines``: the snapshot of the ordered products (contracts.md §9)."""

    __tablename__ = ORDER_LINES_TABLE
    __table_args__ = (
        CheckConstraint("quantity > 0", name=LINE_QUANTITY_POSITIVE_CONSTRAINT),
        Index("ix_order_lines_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey(f"{ORDERS_TABLE}.id", name="fk_order_lines_order_id_orders"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)


class OutboxRow(Base):
    """``outbox``: the transactional outbox of contracts.md §8/§9."""

    __tablename__ = OUTBOX_TABLE
    __table_args__ = (
        Index(OUTBOX_UNPUBLISHED_INDEX, "published_at", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


__all__ = [
    "Base",
    "CUSTOMER_INDEX",
    "LINE_QUANTITY_POSITIVE_CONSTRAINT",
    "ORDERS_TABLE",
    "ORDER_LINES_TABLE",
    "OUTBOX_EVENT_ID_UNIQUE_CONSTRAINT",
    "OUTBOX_TABLE",
    "OUTBOX_UNPUBLISHED_INDEX",
    "OrderLineRow",
    "OrderRow",
    "OutboxRow",
    "PENDING_RELEASE_INDEX",
    "REASON_LENGTH",
    "RELEASE_STATE_ALLOWED_CONSTRAINT",
    "RELEASE_STATE_VALUES",
    "STATUS_ALLOWED_CONSTRAINT",
    "STATUS_VALUES",
]
