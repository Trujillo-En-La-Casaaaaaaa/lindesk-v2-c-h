"""Direct store assertions used by the integration suite (evidence, not decoration)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ProductRow, StockMovementRow, StockRow


def movement_rows(
    session_factory: sessionmaker[Session],
    *,
    order_id: str | None = None,
    operation: str | None = None,
) -> list[dict[str, Any]]:
    """Read ``stock_movements`` straight from the store."""

    statement = select(
        StockMovementRow.order_id, StockMovementRow.operation, StockMovementRow.payload
    ).order_by(StockMovementRow.created_at, StockMovementRow.order_id)
    if order_id is not None:
        statement = statement.where(StockMovementRow.order_id == order_id)
    if operation is not None:
        statement = statement.where(StockMovementRow.operation == operation)
    with session_factory() as session:
        return [
            {"orderId": row.order_id, "operation": row.operation, "payload": row.payload}
            for row in session.execute(statement).all()
        ]


def stock_rows(session_factory: sessionmaker[Session]) -> dict[str, int]:
    """Read the stock ledger straight from the store."""

    with session_factory() as session:
        rows = session.execute(select(StockRow)).scalars().all()
        return {row.product_id: int(row.quantity) for row in rows}


def product_rows(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    """Read the product catalogue straight from the store."""

    with session_factory() as session:
        rows = session.execute(select(ProductRow).order_by(ProductRow.id)).scalars().all()
        return [
            {
                "productId": row.id,
                "sku": row.sku,
                "name": row.name,
                "price": str(row.price),
                "currency": row.currency,
            }
            for row in rows
        ]
