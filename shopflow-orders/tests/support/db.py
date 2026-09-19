"""Direct store assertions of the integration suite (evidence, not decoration).

Everything here reads the owned store with raw SQL and the exact column names of
contracts.md §9, bypassing the ORM and the adapters: an assertion made with these
helpers is an assertion about what is really persisted.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

ORDERS_COLUMNS = (
    "id",
    "customer_id",
    "status",
    "inventory_release_state",
    "total",
    "created_at",
    "updated_at",
    "confirmed_at",
    "shipped_at",
    "cancelled_at",
    "cancellation_reason",
)

ORDER_LINES_COLUMNS = ("id", "order_id", "product_id", "sku", "name", "quantity", "unit_price")

OUTBOX_COLUMNS = ("id", "event_id", "event_type", "payload", "occurred_at", "published_at", "attempts")


def order_row(session_factory: sessionmaker[Session], order_id: str) -> dict[str, Any] | None:
    """Read one ``orders`` row straight from the store."""

    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT id::text, customer_id, status, inventory_release_state, total::text, created_at, updated_at, "
                "confirmed_at, shipped_at, cancelled_at, cancellation_reason "
                "FROM orders WHERE id = :order_id"
            ),
            {"order_id": order_id},
        ).mappings()
        first = row.first()
    return dict(first) if first is not None else None


def order_rows(session_factory: sessionmaker[Session], *, customer_id: str | None = None) -> list[dict[str, Any]]:
    """Read every ``orders`` row (optionally for one customer)."""

    statement = (
        "SELECT id::text, customer_id, status, inventory_release_state, total::text, created_at, updated_at, "
        "confirmed_at, shipped_at, cancelled_at, cancellation_reason FROM orders"
    )
    parameters: dict[str, Any] = {}
    if customer_id is not None:
        statement += " WHERE customer_id = :customer_id"
        parameters["customer_id"] = customer_id
    statement += " ORDER BY created_at, id"
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(statement), parameters).mappings()]


def count_orders(session_factory: sessionmaker[Session]) -> int:
    """How many orders the store holds."""

    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM orders")).scalar_one())


def line_rows(session_factory: sessionmaker[Session], order_id: str) -> list[dict[str, Any]]:
    """Read the ``order_lines`` of one order, ordered by their key."""

    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT id::text, order_id::text, product_id, sku, name, quantity, unit_price::text "
                    "FROM order_lines WHERE order_id = :order_id ORDER BY id"
                ),
                {"order_id": order_id},
            ).mappings()
        ]


def outbox_rows(session_factory: sessionmaker[Session], *, order_id: str | None = None) -> list[dict[str, Any]]:
    """Read ``outbox`` rows, optionally only those of one order."""

    statement = (
        "SELECT id::text, event_id::text, event_type, payload, occurred_at, published_at, attempts FROM outbox"
    )
    parameters: dict[str, Any] = {}
    if order_id is not None:
        statement += " WHERE payload ->> 'orderId' = :order_id"
        parameters["order_id"] = order_id
    statement += " ORDER BY occurred_at, id"
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(statement), parameters).mappings()]


def unpublished_outbox_rows(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    """``outbox`` rows the broker has not confirmed yet."""

    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT id::text, event_id::text, event_type, payload, occurred_at, published_at, attempts "
                    "FROM outbox WHERE published_at IS NULL ORDER BY occurred_at, id"
                )
            ).mappings()
        ]


def table_columns(session_factory: sessionmaker[Session], table: str) -> dict[str, dict[str, Any]]:
    """Read ``information_schema.columns`` for one table of the owned store."""

    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT column_name, data_type, is_nullable, character_maximum_length, column_default "
                "FROM information_schema.columns WHERE table_name = :table AND table_schema = current_schema() "
                "ORDER BY ordinal_position"
            ),
            {"table": table},
        ).mappings()
        return {str(row["column_name"]): dict(row) for row in rows}


def check_constraints(session_factory: sessionmaker[Session], table: str) -> dict[str, str]:
    """Read the ``CHECK`` constraints of one table (name -> definition)."""

    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT c.conname AS name, pg_get_constraintdef(c.oid) AS definition "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :table AND c.contype = 'c' ORDER BY c.conname"
            ),
            {"table": table},
        ).mappings()
        return {str(row["name"]): str(row["definition"]) for row in rows}


def unique_constraints(session_factory: sessionmaker[Session], table: str) -> dict[str, str]:
    """Read the unique constraints/indexes of one table (name -> definition)."""

    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT c.conname AS name, pg_get_constraintdef(c.oid) AS definition "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :table AND c.contype IN ('u', 'p') ORDER BY c.conname"
            ),
            {"table": table},
        ).mappings()
        return {str(row["name"]): str(row["definition"]) for row in rows}


def foreign_keys(session_factory: sessionmaker[Session], table: str) -> list[str]:
    """Read the foreign keys of one table."""

    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) AS definition "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :table AND c.contype = 'f'"
            ),
            {"table": table},
        ).mappings()
        return [str(row["definition"]) for row in rows]


def delete_order(session_factory: sessionmaker[Session], order_id: str) -> None:
    """Remove one order, its lines and its outbox rows (test cleanup)."""

    if not _is_uuid(order_id):  # pragma: no cover - defensive
        raise ValueError(f"not an order id: {order_id!r}")
    with session_factory() as session, session.begin():
        session.execute(text("DELETE FROM outbox WHERE payload ->> 'orderId' = :order_id"), {"order_id": order_id})
        session.execute(text("DELETE FROM order_lines WHERE order_id = :order_id"), {"order_id": order_id})
        session.execute(text("DELETE FROM orders WHERE id = :order_id"), {"order_id": order_id})


def truncate_store(session_factory: sessionmaker[Session]) -> None:
    """Empty the owned store (test isolation)."""

    with session_factory() as session, session.begin():
        session.execute(text("TRUNCATE TABLE outbox, order_lines, orders"))


def _is_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


__all__ = [
    "ORDERS_COLUMNS",
    "ORDER_LINES_COLUMNS",
    "OUTBOX_COLUMNS",
    "check_constraints",
    "count_orders",
    "delete_order",
    "foreign_keys",
    "line_rows",
    "order_row",
    "order_rows",
    "outbox_rows",
    "table_columns",
    "truncate_store",
    "unique_constraints",
    "unpublished_outbox_rows",
]
