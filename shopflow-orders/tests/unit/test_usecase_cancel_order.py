"""Unit tests of the cancel-order use case (HANDOFF #3, acceptance criteria 3-7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.cancellation import MAX_REASON_LENGTH
from app.domain.errors import InvalidReason, NotFound, OrderNotCancellable, OrderShipped
from app.domain.order import InventoryReleaseState, OrderStatus, RequestedLine
from app.events.contracts import EVENT_ORDER_CANCELLED, order_cancelled_event
from app.usecases.cancel_order import cancel_order
from tests.support.fakes import FakeInventory, InMemoryOrderRepository, new_order

MOMENT = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
ORDER_ID = "0f14de0e-6f2e-4b7a-9f5b-1f3f1d9c1111"
REASON = "customer changed their mind"  # exactly 28 characters
TEN_CHAR_REASON = "not wanted"


def setup(order_status: OrderStatus = OrderStatus.CONFIRMED, *, reserved: bool = True):
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    order = new_order(order_id=ORDER_ID, status=order_status)
    repository.orders[ORDER_ID] = order
    if reserved:
        inventory.reserve(ORDER_ID, [RequestedLine(line.product_id, line.quantity) for line in order.lines])
        inventory.calls.clear()
        inventory.keys_used.clear()
    return repository, inventory, order


def fixed_clock(moment: datetime = MOMENT):
    def now() -> datetime:
        return moment

    return now


def test_cancels_a_confirmed_order_and_releases_the_stock() -> None:
    repository, inventory, order = setup()
    cancelled = cancel_order(
        repository=repository,
        inventory=inventory,
        order_id=ORDER_ID,
        reason=REASON,
        correlation_id="corr-1",
        now=fixed_clock(),
    )

    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.cancelled_at == MOMENT
    assert cancelled.cancellation_reason == REASON
    assert cancelled.inventory_release_state is InventoryReleaseState.RELEASED
    assert repository.orders[ORDER_ID].status is OrderStatus.CANCELLED
    assert inventory.stock["p-1001"] == 50  # restored
    assert [call[0] for call in inventory.calls] == ["release"]
    assert inventory.keys_used == [f"order:{ORDER_ID}:release"]
    assert repository.event_types == [EVENT_ORDER_CANCELLED]
    payload = repository.messages[0].payload
    assert payload["orderId"] == ORDER_ID
    assert payload["reason"] == REASON
    assert payload["lines"] == [{"productId": "p-1001", "quantity": order.lines[0].quantity, "unitPrice": 19.99}]
    assert payload["correlationId"] == "corr-1"


def test_the_trimmed_reason_is_stored_and_published() -> None:
    repository, _inventory, _order = setup()
    cancelled = cancel_order(
        repository=repository,
        inventory=_inventory,
        order_id=ORDER_ID,
        reason=f"   {TEN_CHAR_REASON}   ",
        now=fixed_clock(),
    )
    assert cancelled.cancellation_reason == TEN_CHAR_REASON
    assert repository.messages[0].payload["reason"] == TEN_CHAR_REASON


def test_a_repeated_cancellation_returns_the_stored_facts_and_does_nothing() -> None:
    repository, inventory, _order = setup()
    first = cancel_order(
        repository=repository,
        inventory=inventory,
        order_id=ORDER_ID,
        reason=TEN_CHAR_REASON,
        now=fixed_clock(),
    )
    inventory.calls.clear()
    inventory.keys_used.clear()
    stock_after_first = dict(inventory.stock)

    second = cancel_order(
        repository=repository,
        inventory=inventory,
        order_id=ORDER_ID,
        reason="a completely different reason",
        now=fixed_clock(MOMENT + timedelta(minutes=5)),
    )

    assert second.status is OrderStatus.CANCELLED
    assert second.cancelled_at == first.cancelled_at == MOMENT
    assert second.cancellation_reason == TEN_CHAR_REASON  # the stored reason, not the new one
    assert inventory.calls == []  # no second release
    assert inventory.stock == stock_after_first
    assert repository.event_types == [EVENT_ORDER_CANCELLED]  # no second event


@pytest.mark.parametrize("reason", ["", "   ", "\t", "a" * (MAX_REASON_LENGTH + 1)])
def test_invalid_reason_changes_nothing(reason: str) -> None:
    repository, inventory, _order = setup()
    with pytest.raises(InvalidReason) as raised:
        cancel_order(repository=repository, inventory=inventory, order_id=ORDER_ID, reason=reason, now=fixed_clock())

    assert raised.value.code == "invalid_reason"
    assert repository.orders[ORDER_ID].status is OrderStatus.CONFIRMED
    assert repository.messages == ()
    assert inventory.calls == []
    assert inventory.stock["p-1001"] == 48


def test_a_200_character_reason_is_accepted() -> None:
    repository, inventory, _order = setup()
    reason = "a" * MAX_REASON_LENGTH
    cancelled = cancel_order(
        repository=repository,
        inventory=inventory,
        order_id=ORDER_ID,
        reason=reason,
        now=fixed_clock(),
    )
    assert cancelled.cancellation_reason == reason
    assert repository.messages[0].payload["reason"] == reason


def test_reason_validation_precedes_the_unknown_order_rule() -> None:
    repository, inventory, _order = setup()
    with pytest.raises(InvalidReason):
        cancel_order(
            repository=repository,
            inventory=inventory,
            order_id="11111111-2222-3333-4444-555555555555",
            reason="   ",
            now=fixed_clock(),
        )


def test_unknown_order_is_not_found() -> None:
    repository, inventory, _order = setup()
    unknown = "11111111-2222-3333-4444-555555555555"
    with pytest.raises(NotFound) as raised:
        cancel_order(repository=repository, inventory=inventory, order_id=unknown, reason=REASON, now=fixed_clock())
    assert raised.value.code == "not_found"
    assert raised.value.details["orderId"] == unknown


def test_a_shipped_order_cannot_be_cancelled() -> None:
    repository, inventory, order = setup(OrderStatus.SHIPPED)
    with pytest.raises(OrderShipped) as raised:
        cancel_order(repository=repository, inventory=inventory, order_id=ORDER_ID, reason=REASON, now=fixed_clock())

    assert raised.value.code == "order_shipped"
    assert repository.orders[ORDER_ID].status is OrderStatus.SHIPPED
    assert repository.messages == ()
    assert inventory.calls == []


@pytest.mark.parametrize("status", [OrderStatus.CREATED, OrderStatus.FAILED])
def test_other_statuses_are_not_cancellable(status: OrderStatus) -> None:
    repository, inventory, _order = setup(status)
    with pytest.raises(OrderNotCancellable) as raised:
        cancel_order(repository=repository, inventory=inventory, order_id=ORDER_ID, reason=REASON, now=fixed_clock())
    assert raised.value.code == "order_not_cancellable"
    assert repository.orders[ORDER_ID].status is status
    assert repository.messages == ()


def test_a_failing_release_leaves_the_order_cancelled_with_a_pending_release() -> None:
    repository, inventory, _order = setup()
    inventory.make_unavailable("release")

    cancelled = cancel_order(
        repository=repository,
        inventory=inventory,
        order_id=ORDER_ID,
        reason=REASON,
        now=fixed_clock(),
    )

    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.inventory_release_state is InventoryReleaseState.PENDING
    assert repository.orders[ORDER_ID].inventory_release_state is InventoryReleaseState.PENDING
    assert repository.event_types == [EVENT_ORDER_CANCELLED]  # the event is still durable intent
    assert inventory.stock["p-1001"] == 48  # not restored yet; the reconciler will


def test_a_lost_guarded_update_re_reads_and_reports_the_stored_cancellation() -> None:
    """The concurrent duplicate: exactly one writer wins, the loser reports the stored facts."""

    repository, inventory, _order = setup()

    def competing_writer() -> None:
        winner_at = MOMENT - timedelta(seconds=1)
        winner = new_order(order_id=ORDER_ID, status=OrderStatus.CONFIRMED).cancelled(at=winner_at, reason="winner")
        repository.force_cancel(
            ORDER_ID,
            at=winner_at,
            reason="winner",
            event=order_cancelled_event(winner, occurred_at=winner_at),
        )

    repository.hooks["mark_cancelled"] = competing_writer
    result = cancel_order(
        repository=repository,
        inventory=inventory,
        order_id=ORDER_ID,
        reason=REASON,
        now=fixed_clock(),
    )

    assert result.status is OrderStatus.CANCELLED
    assert result.cancelled_at == MOMENT - timedelta(seconds=1)
    assert result.cancellation_reason == "winner"
    assert inventory.calls == []  # the loser releases nothing
    assert repository.event_types == [EVENT_ORDER_CANCELLED]  # exactly one event


def test_a_shipped_order_wins_the_race_against_a_cancellation() -> None:
    repository, inventory, order = setup()

    def ship_it() -> None:
        repository.orders[ORDER_ID] = order.shipped(at=MOMENT)

    repository.hooks["mark_cancelled"] = ship_it
    with pytest.raises(OrderShipped):
        cancel_order(repository=repository, inventory=inventory, order_id=ORDER_ID, reason=REASON, now=fixed_clock())

    assert repository.orders[ORDER_ID].status is OrderStatus.SHIPPED
    assert repository.messages == ()
    assert inventory.calls == []
