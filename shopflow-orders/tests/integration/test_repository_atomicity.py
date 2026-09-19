"""Integration tests of the atomicity and rollback guarantees of the store.

These tests exercise the real adapter against the real PostgreSQL store. They prove
the two invariants the use cases rely on:

* a status change and its outbox row are one transaction - a failure while inserting
  the event rolls the status change back;
* an order and its lines are one transaction - a rejected line leaves no order behind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.postgres_order_repository import PostgresOrderRepository
from app.adapters.postgres_outbox import PostgresOutbox
from app.domain.order import InventoryReleaseState, Order, OrderLine, OrderStatus, line_ids, order_total, utc_now
from app.events.contracts import order_cancelled_event, order_confirmed_event
from app.ports.outbox import OutboxMessage
from tests.support.db import count_orders, line_rows, order_row, outbox_rows

STICKER = OrderLine("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", 1, Decimal("4.25"))


class ExplodingOutbox(PostgresOutbox):
    """An outbox whose insert fails, to prove the surrounding transaction rolls back."""

    def insert(self, session: Session, message: OutboxMessage) -> None:  # noqa: ARG002 - test double
        raise RuntimeError("the outbox insert failed")


def place_order(client: TestClient, *, customer_id: str = "c-atomic") -> dict:
    response = client.post(
        "/orders",
        json={"customerId": customer_id, "lines": [{"productId": "p-1004", "quantity": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_order(order_id: str, *, lines=(STICKER,), status: OrderStatus = OrderStatus.CREATED) -> Order:
    moment = utc_now()
    return Order(
        order_id=order_id,
        customer_id="c-atomic",
        status=status,
        lines=tuple(lines),
        total=order_total(lines),
        created_at=moment,
        updated_at=moment,
    )


def test_a_failed_outbox_insert_rolls_the_cancellation_back(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = place_order(client)
    repository = PostgresOrderRepository(session_factory, ExplodingOutbox(session_factory))
    stored = repository.get(created["orderId"])
    assert stored is not None
    cancelled_at = utc_now()
    event = order_cancelled_event(
        stored.cancelled(at=cancelled_at, reason="changed mind"), occurred_at=cancelled_at
    )

    with pytest.raises(RuntimeError):
        repository.mark_cancelled(
            created["orderId"],
            cancelled_at=cancelled_at,
            reason="changed mind",
            event=event,
        )

    row = order_row(session_factory, created["orderId"])
    assert row is not None
    assert row["status"] == "CONFIRMED"  # the guarded update was rolled back
    assert row["cancelled_at"] is None
    assert row["cancellation_reason"] is None
    assert row["inventory_release_state"] == "NONE"
    assert [event_row["event_type"] for event_row in outbox_rows(session_factory, order_id=created["orderId"])] == [
        "order.confirmed"
    ]


def test_a_failed_outbox_insert_rolls_the_confirmation_back(session_factory: sessionmaker[Session]) -> None:
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    order = make_order("7f0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8")
    repository.create(order)

    broken = PostgresOrderRepository(session_factory, ExplodingOutbox(session_factory))
    confirmed_at = utc_now()
    event = order_confirmed_event(order.confirmed(at=confirmed_at), occurred_at=confirmed_at)
    with pytest.raises(RuntimeError):
        broken.mark_confirmed(order.order_id, confirmed_at=confirmed_at, event=event)

    row = order_row(session_factory, order.order_id)
    assert row is not None
    assert row["status"] == "CREATED"
    assert row["confirmed_at"] is None
    assert outbox_rows(session_factory, order_id=order.order_id) == []


def test_a_rejected_line_leaves_no_order_behind(session_factory: sessionmaker[Session]) -> None:
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    broken = make_order(
        "8f0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8",
        lines=[OrderLine("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", 0, Decimal("4.25"))],
    )

    with pytest.raises(IntegrityError):
        repository.create(broken)

    assert count_orders(session_factory) == 0  # the order row was rolled back with its line
    assert order_row(session_factory, broken.order_id) is None


def test_the_order_lines_keep_the_requested_order(session_factory: sessionmaker[Session]) -> None:
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    lines = (
        OrderLine("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", 1, Decimal("12.50")),
        OrderLine("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", 2, Decimal("19.99")),
        OrderLine("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", 3, Decimal("4.25")),
    )
    order = make_order("9f0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8", lines=lines)
    repository.create(order)

    stored = repository.get(order.order_id)
    assert stored is not None
    assert [line.product_id for line in stored.lines] == ["p-1002", "p-1001", "p-1004"]
    assert [row["product_id"] for row in line_rows(session_factory, order.order_id)] == ["p-1002", "p-1001", "p-1004"]
    assert [row["id"] for row in line_rows(session_factory, order.order_id)] == list(line_ids(order.order_id, 3))


def test_marking_a_terminal_order_is_a_guarded_no_op(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = place_order(client)
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    client.post(f"/orders/{created['orderId']}/cancel", json={"reason": "changed mind"})
    cancelled_before = order_row(session_factory, created["orderId"])
    events_before = outbox_rows(session_factory, order_id=created["orderId"])

    shipped = repository.mark_shipped(created["orderId"], shipped_at=utc_now())
    confirmed_again = repository.mark_confirmed(
        created["orderId"],
        confirmed_at=utc_now(),
        event=OutboxMessage.new("order.confirmed", {"orderId": created["orderId"]}, occurred_at=utc_now()),
    )
    cancelled_again = repository.mark_cancelled(
        created["orderId"],
        cancelled_at=utc_now(),
        reason="another reason",
        event=OutboxMessage.new("order.cancelled", {"orderId": created["orderId"]}, occurred_at=utc_now()),
    )

    assert (shipped, confirmed_again, cancelled_again) == (False, False, False)
    assert order_row(session_factory, created["orderId"]) == cancelled_before
    assert outbox_rows(session_factory, order_id=created["orderId"]) == events_before


def test_the_release_state_move_is_guarded(session_factory: sessionmaker[Session]) -> None:
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    order = make_order("af0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8", status=OrderStatus.CONFIRMED)
    repository.create(order)
    cancelled = order.cancelled(at=utc_now(), reason="changed mind")
    repository.mark_cancelled(
        order.order_id,
        cancelled_at=cancelled.cancelled_at,
        reason="changed mind",
        event=order_cancelled_event(cancelled, occurred_at=cancelled.cancelled_at),
    )

    assert repository.set_release_state(order.order_id, state=InventoryReleaseState.RELEASED) is True
    assert repository.set_release_state(order.order_id, state=InventoryReleaseState.RELEASED) is False
    row = order_row(session_factory, order.order_id)
    assert row is not None and row["inventory_release_state"] == "RELEASED"
    assert repository.find_pending_releases(limit=10) == ()


def test_a_cancelled_order_is_found_as_a_pending_release(session_factory: sessionmaker[Session]) -> None:
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    order = make_order("cf0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8", status=OrderStatus.CONFIRMED)
    repository.create(order)
    cancelled = order.cancelled(at=utc_now(), reason="changed mind")
    repository.mark_cancelled(
        order.order_id,
        cancelled_at=cancelled.cancelled_at,
        reason="changed mind",
        event=order_cancelled_event(cancelled, occurred_at=cancelled.cancelled_at),
    )

    pending = repository.find_pending_releases(limit=10)
    assert [item.order_id for item in pending] == [order.order_id]
    assert pending[0].lines == (STICKER,)


def test_reading_an_unknown_order_is_none(session_factory: sessionmaker[Session]) -> None:
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    assert repository.get("11111111-2222-3333-4444-555555555555") is None


def test_created_at_round_trips_as_utc(session_factory: sessionmaker[Session]) -> None:
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    moment = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    order = Order(
        order_id="bf0a1b2c-3d4e-5f60-7182-93a4b5c6d7e8",
        customer_id="c-atomic",
        status=OrderStatus.CREATED,
        lines=(STICKER,),
        total=order_total((STICKER,)),
        created_at=moment,
        updated_at=moment,
    )
    repository.create(order)

    stored = repository.get(order.order_id)
    assert stored is not None
    assert stored.created_at == moment
    assert stored.created_at.tzinfo is not None
