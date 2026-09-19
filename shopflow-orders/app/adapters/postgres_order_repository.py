"""PostgreSQL adapter for the order repository (component: ``postgres-order-repository``).

The adapter owns the transactional mechanics the use cases rely on:

* ``create`` writes the order and its lines in one transaction; the line keys are
  derived from the order id and the requested position, so reading the lines
  ordered by their key returns them in request order (contracts.md §9 declares no
  position column);
* every status change is a guarded ``UPDATE ... WHERE id = :id AND status = :expected``
  - the *store* decides who wins a race, and the matching outbox row is inserted on
  the same session, in the same transaction, only by the writer that actually
  changed the row. A concurrent duplicate therefore cannot produce a second event;
* a cancelled order is written with ``inventory_release_state = 'PENDING'`` in the
  same statement as its terminal status, which is the durable intent the release
  reconciler picks up after a restart.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.adapters.postgres_outbox import PostgresOutbox
from app.db.models import OrderLineRow, OrderRow
from app.domain.order import (
    InventoryReleaseState,
    Order,
    OrderLine,
    OrderStatus,
    line_ids,
    quantize_money,
    utc_now,
)
from app.ports.order_repository import OrderPage
from app.ports.outbox import OutboxMessage


class PostgresOrderRepository:
    """Implements :class:`app.ports.order_repository.OrderRepository`."""

    def __init__(self, session_factory, outbox: PostgresOutbox) -> None:
        self._session_factory = session_factory
        self._outbox = outbox

    # -- writes -------------------------------------------------------------
    def create(self, order: Order, *, event: OutboxMessage | None = None) -> Order:
        line_keys = line_ids(order.order_id, len(order.lines))
        with self._session_factory() as session, session.begin():
            session.add(
                OrderRow(
                    id=uuid.UUID(order.order_id),
                    customer_id=order.customer_id,
                    status=str(order.status),
                    inventory_release_state=str(order.inventory_release_state),
                    total=quantize_money(order.total),
                    created_at=_aware(order.created_at),
                    updated_at=_aware(order.updated_at),
                    confirmed_at=_aware(order.confirmed_at) if order.confirmed_at else None,
                    shipped_at=_aware(order.shipped_at) if order.shipped_at else None,
                    cancelled_at=_aware(order.cancelled_at) if order.cancelled_at else None,
                    cancellation_reason=order.cancellation_reason,
                )
            )
            # The rows carry no relationship() (the schema is what it is), so the
            # unit of work cannot infer the dependency: flush the parent first,
            # otherwise the line rows could be inserted before the order they
            # reference and the foreign key would refuse them.
            session.flush()
            for key, line in zip(line_keys, order.lines, strict=True):
                session.add(
                    OrderLineRow(
                        id=uuid.UUID(key),
                        order_id=uuid.UUID(order.order_id),
                        product_id=line.product_id,
                        sku=line.sku,
                        name=line.name,
                        quantity=line.quantity,
                        unit_price=quantize_money(line.unit_price),
                    )
                )
            if event is not None:
                self._outbox.insert(session, event)
        return order

    def mark_confirmed(self, order_id: str, *, confirmed_at: datetime, event: OutboxMessage) -> bool:
        moment = _aware(confirmed_at)
        with self._session_factory() as session, session.begin():
            changed = _guarded_update(
                session,
                order_id,
                expected=OrderStatus.CREATED,
                values={"status": str(OrderStatus.CONFIRMED), "confirmed_at": moment, "updated_at": moment},
            )
            if changed:
                self._outbox.insert(session, event)
        return changed

    def mark_failed(self, order_id: str, *, failed_at: datetime) -> bool:
        moment = _aware(failed_at)
        with self._session_factory() as session, session.begin():
            return _guarded_update(
                session,
                order_id,
                expected=OrderStatus.CREATED,
                values={"status": str(OrderStatus.FAILED), "updated_at": moment},
            )

    def mark_cancelled(
        self,
        order_id: str,
        *,
        cancelled_at: datetime,
        reason: str,
        event: OutboxMessage,
    ) -> bool:
        moment = _aware(cancelled_at)
        with self._session_factory() as session, session.begin():
            changed = _guarded_update(
                session,
                order_id,
                expected=OrderStatus.CONFIRMED,
                values={
                    "status": str(OrderStatus.CANCELLED),
                    "cancelled_at": moment,
                    "cancellation_reason": reason,
                    "inventory_release_state": str(InventoryReleaseState.PENDING),
                    "updated_at": moment,
                },
            )
            if changed:
                self._outbox.insert(session, event)
        return changed

    def mark_shipped(self, order_id: str, *, shipped_at: datetime) -> bool:
        moment = _aware(shipped_at)
        with self._session_factory() as session, session.begin():
            return _guarded_update(
                session,
                order_id,
                expected=OrderStatus.CONFIRMED,
                values={"status": str(OrderStatus.SHIPPED), "shipped_at": moment, "updated_at": moment},
            )

    def set_release_state(self, order_id: str, *, state: InventoryReleaseState) -> bool:
        conditions = [
            OrderRow.id == uuid.UUID(str(order_id)),
            OrderRow.inventory_release_state != str(state),
        ]
        if state is InventoryReleaseState.RELEASED:
            # Only a pending release may be marked released: the state is written
            # with the cancellation, so it can never jump there from NONE.
            conditions.append(OrderRow.inventory_release_state == str(InventoryReleaseState.PENDING))
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(OrderRow)
                .where(*conditions)
                .values(inventory_release_state=str(state), updated_at=utc_now())
            )
            return result.rowcount == 1

    # -- reads --------------------------------------------------------------
    def get(self, order_id: str) -> Order | None:
        with self._session_factory() as session:
            row = session.execute(
                select(OrderRow).where(OrderRow.id == uuid.UUID(str(order_id)))
            ).scalar_one_or_none()
            if row is None:
                return None
            lines = _lines_of(session, [row.id])
        return _order(row, lines.get(row.id, ()))

    def list_by_customer(
        self,
        customer_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> OrderPage:
        with self._session_factory() as session:
            total = int(
                session.execute(
                    select(func.count()).select_from(OrderRow).where(OrderRow.customer_id == customer_id)
                ).scalar_one()
            )
            statement = (
                select(OrderRow)
                .where(OrderRow.customer_id == customer_id)
                .order_by(OrderRow.created_at, OrderRow.id)
                .offset(max(0, int(offset)))
            )
            if limit is not None:
                statement = statement.limit(max(0, int(limit)))
            rows = list(session.execute(statement).scalars().all())
            lines = _lines_of(session, [row.id for row in rows])
        return OrderPage(items=tuple(_order(row, lines.get(row.id, ())) for row in rows), total=total)

    def find_pending_releases(self, *, limit: int) -> Sequence[Order]:
        with self._session_factory() as session:
            rows = list(
                session.execute(
                    select(OrderRow)
                    .where(
                        OrderRow.status == str(OrderStatus.CANCELLED),
                        OrderRow.inventory_release_state == str(InventoryReleaseState.PENDING),
                    )
                    .order_by(OrderRow.cancelled_at, OrderRow.id)
                    .limit(max(1, int(limit)))
                )
                .scalars()
                .all()
            )
            lines = _lines_of(session, [row.id for row in rows])
        return tuple(_order(row, lines.get(row.id, ())) for row in rows)

    def find_pending_reservations(self, *, limit: int, older_than: datetime) -> Sequence[Order]:
        with self._session_factory() as session:
            rows = list(
                session.execute(
                    select(OrderRow)
                    .where(
                        OrderRow.status == str(OrderStatus.CREATED),
                        OrderRow.created_at < _aware(older_than),
                    )
                    .order_by(OrderRow.created_at, OrderRow.id)
                    .limit(max(1, int(limit)))
                )
                .scalars()
                .all()
            )
            lines = _lines_of(session, [row.id for row in rows])
        return tuple(_order(row, lines.get(row.id, ())) for row in rows)


def _guarded_update(
    session,
    order_id: str,
    *,
    expected: OrderStatus,
    values: dict[str, object],
) -> bool:
    """Apply a status change only when the stored status is still ``expected``."""

    result = session.execute(
        update(OrderRow)
        .where(OrderRow.id == uuid.UUID(str(order_id)), OrderRow.status == str(expected))
        .values(**values)
    )
    return result.rowcount == 1


def _lines_of(session, order_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[OrderLine, ...]]:
    """Load the lines of several orders in one statement, in request order."""

    if not order_ids:
        return {}
    rows = (
        session.execute(
            select(OrderLineRow)
            .where(OrderLineRow.order_id.in_(list(order_ids)))
            .order_by(OrderLineRow.order_id, OrderLineRow.id)
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[OrderLine]] = {}
    for row in rows:
        grouped.setdefault(row.order_id, []).append(
            OrderLine(
                product_id=row.product_id,
                sku=row.sku,
                name=row.name,
                quantity=int(row.quantity),
                unit_price=row.unit_price,
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def _order(row: OrderRow, lines: Sequence[OrderLine]) -> Order:
    return Order(
        order_id=str(row.id),
        customer_id=row.customer_id,
        status=OrderStatus(row.status),
        lines=tuple(lines),
        total=row.total,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
        confirmed_at=_aware(row.confirmed_at) if row.confirmed_at else None,
        shipped_at=_aware(row.shipped_at) if row.shipped_at else None,
        cancelled_at=_aware(row.cancelled_at) if row.cancelled_at else None,
        cancellation_reason=row.cancellation_reason,
        inventory_release_state=InventoryReleaseState(row.inventory_release_state),
    )


def _aware(value: datetime) -> datetime:
    """Return a timezone-aware UTC timestamp (the columns are ``timestamptz``)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["PostgresOrderRepository"]
