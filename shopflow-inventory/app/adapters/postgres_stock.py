"""PostgreSQL adapter for the stock port: ``postgres-stock-repository``.

The adapter owns the transactional mechanics the reservation/release use cases
rely on:

* ``lock_levels`` performs ``SELECT ... FOR UPDATE`` on the affected rows, always
  in the same (product id) order, so two orders racing for the same units are
  serialized and cannot deadlock each other;
* ``record_movement`` inserts the movement row with
  ``ON CONFLICT (order_id, operation) DO NOTHING RETURNING id``. The unique
  constraint of ``stock_movements`` is therefore the idempotency authority: a
  retried command learns from the store that it is a replay, never from process
  memory, so the guarantee survives restarts;
* a ``CHECK (quantity >= 0)`` violation (SQLSTATE 23514) is translated into the
  domain's :class:`InsufficientStock`, so an oversell attempt can never leak a
  database error to the caller.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import StockMovementRow, StockRow
from app.domain.errors import InsufficientStock, UnknownProduct
from app.domain.stock import StockLine, StockMovement

CHECK_VIOLATION_SQLSTATE = "23514"


class PostgresStockRepository:
    """Implements :class:`app.ports.repositories.StockRepository`."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def available_quantities(self, product_ids: Sequence[str]) -> dict[str, int]:
        wanted = sorted(set(product_ids))
        if not wanted:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(StockRow.product_id, StockRow.quantity).where(StockRow.product_id.in_(wanted))
            ).all()
        return {str(row.product_id): int(row.quantity) for row in rows}

    @contextmanager
    def transaction(self) -> Iterator[PostgresStockTransaction]:
        """Open one transaction; commit on clean exit, roll back on any error."""

        session = self._session_factory()
        transaction = PostgresStockTransaction(session)
        try:
            yield transaction
        except BaseException:
            transaction.close(rollback=True)
            raise
        else:
            transaction.close(rollback=transaction.rolled_back)


class PostgresStockTransaction:
    """One ``stock``/``stock_movements`` transaction of the pinned store."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._closed = False
        self.rolled_back = False

    # -- movements ---------------------------------------------------------
    def record_movement(self, movement: StockMovement) -> bool:
        statement = (
            pg_insert(StockMovementRow)
            .values(
                order_id=movement.order_id,
                operation=movement.operation,
                payload=movement.payload(),
                created_at=movement.created_at,
            )
            .on_conflict_do_nothing(index_elements=["order_id", "operation"])
            .returning(StockMovementRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def find_movement(self, order_id: str, operation: str) -> StockMovement | None:
        row = (
            self._session.execute(
                select(StockMovementRow)
                .where(StockMovementRow.order_id == order_id, StockMovementRow.operation == operation)
                .order_by(StockMovementRow.created_at)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return StockMovement(
            order_id=row.order_id,
            operation=row.operation,
            lines=_lines_from_payload(row.payload),
            created_at=_aware(row.created_at),
        )

    # -- ledger ------------------------------------------------------------
    def lock_levels(self, product_ids: Sequence[str]) -> dict[str, int]:
        wanted = sorted(set(product_ids))
        if not wanted:
            return {}
        rows = self._session.execute(
            select(StockRow.product_id, StockRow.quantity)
            .where(StockRow.product_id.in_(wanted))
            .order_by(StockRow.product_id)
            .with_for_update()
        ).all()
        return {str(row.product_id): int(row.quantity) for row in rows}

    def decrement(self, decrements: dict[str, int]) -> None:
        for product_id in sorted(decrements):
            amount = int(decrements[product_id])
            try:
                result = self._session.execute(
                    update(StockRow)
                    .where(StockRow.product_id == product_id)
                    .values(quantity=StockRow.quantity - amount, updated_at=func.now())
                )
            except IntegrityError as exc:
                raise _translate_ledger_integrity_error(exc, product_id, amount) from exc
            if result.rowcount != 1:
                raise UnknownProduct(product_id)

    def increment(self, increments: dict[str, int]) -> None:
        for product_id in sorted(increments):
            amount = int(increments[product_id])
            result = self._session.execute(
                update(StockRow)
                .where(StockRow.product_id == product_id)
                .values(quantity=StockRow.quantity + amount, updated_at=func.now())
            )
            if result.rowcount != 1:
                raise UnknownProduct(product_id)

    # -- lifecycle ---------------------------------------------------------
    def rollback(self) -> None:
        """Discard this transaction (the use cases call it for no-op outcomes)."""

        self.rolled_back = True
        self._session.rollback()

    def close(self, *, rollback: bool) -> None:
        """End the transaction exactly once: commit unless a rollback was asked for."""

        if self._closed:
            return
        self._closed = True
        try:
            if rollback:
                self._session.rollback()
            else:
                self._session.commit()
        finally:
            self._session.close()


def _translate_ledger_integrity_error(exc: IntegrityError, product_id: str, amount: int) -> BaseException:
    """Map the store's never-negative guard onto the domain error it protects."""

    if _sqlstate(exc) == CHECK_VIOLATION_SQLSTATE:
        return InsufficientStock(product_id, amount, 0)
    return exc


def _sqlstate(exc: IntegrityError) -> str | None:
    for candidate in (getattr(exc, "orig", None), getattr(getattr(exc, "orig", None), "__cause__", None)):
        state = getattr(candidate, "pgcode", None)
        if state:
            return str(state)
    return None


def _lines_from_payload(payload: Any) -> tuple[StockLine, ...]:
    if not isinstance(payload, dict):
        return ()
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        return ()
    lines: list[StockLine] = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        lines.append(StockLine(product_id=str(raw.get("productId", "")), quantity=int(raw.get("quantity", 0))))
    return tuple(lines)


def _aware(value: datetime) -> datetime:
    """Return a timezone-aware UTC timestamp (the column is ``timestamptz``)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
