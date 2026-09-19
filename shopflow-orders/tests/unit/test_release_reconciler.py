"""Unit tests of the release reconciler (HANDOFF #7, durable cancellation)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.order import InventoryReleaseState, OrderStatus, RequestedLine
from app.events.contracts import EVENT_ORDER_CONFIRMED
from app.workers.release_reconciler import ReleaseReconciler
from tests.support.fakes import FakeInventory, InMemoryOrderRepository, new_order

ORDER_ID = "0f14de0e-6f2e-4b7a-9f5b-1f3f1d9c1111"
MOMENT = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def fixed_clock(moment: datetime = MOMENT):
    def now() -> datetime:
        return moment

    return now


def cancelled_order():
    return new_order(order_id=ORDER_ID, status=OrderStatus.CONFIRMED).cancelled(
        at=MOMENT, reason="customer changed their mind"
    )


def setup(*, reservation_recorded: bool = True):
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    order = cancelled_order()
    repository.orders[ORDER_ID] = order
    if reservation_recorded:
        inventory.reserve(ORDER_ID, [RequestedLine(line.product_id, line.quantity) for line in order.lines])
    inventory.calls.clear()
    inventory.keys_used.clear()
    return repository, inventory


def reconciler(repository, inventory, **kwargs) -> ReleaseReconciler:
    return ReleaseReconciler(
        repository=repository,
        inventory=inventory,
        interval_seconds=kwargs.pop("interval_seconds", 0.01),
        batch_size=kwargs.pop("batch_size", 10),
        reservation_grace_seconds=kwargs.pop("reservation_grace_seconds", 0.0),
        now=fixed_clock(),
    )


def test_a_pending_release_is_retried_and_marked_released() -> None:
    repository, inventory = setup()
    report = reconciler(repository, inventory).run_once()

    assert report.released == 1
    assert report.pending_releases == 0
    assert repository.orders[ORDER_ID].inventory_release_state is InventoryReleaseState.RELEASED
    assert inventory.stock["p-1001"] == 50
    assert inventory.keys_used == [f"order:{ORDER_ID}:release"]


def test_a_failing_release_stays_pending_and_is_retried_until_it_succeeds() -> None:
    repository, inventory = setup()
    inventory.make_unavailable("release")
    worker = reconciler(repository, inventory)

    first = worker.run_once()
    assert first.released == 0
    assert first.pending_releases == 1
    assert repository.orders[ORDER_ID].inventory_release_state is InventoryReleaseState.PENDING
    assert inventory.stock["p-1001"] == 48

    inventory.available_again()
    second = worker.run_once()
    assert second.released == 1
    assert repository.orders[ORDER_ID].inventory_release_state is InventoryReleaseState.RELEASED
    assert inventory.stock["p-1001"] == 50


def test_an_already_released_order_is_not_touched() -> None:
    repository, inventory = setup()
    repository.orders[ORDER_ID] = repository.orders[ORDER_ID].with_release_state(InventoryReleaseState.RELEASED)
    inventory.calls.clear()

    report = reconciler(repository, inventory).run_once()

    assert report.released == 0
    assert inventory.calls == []


def test_only_cancelled_orders_with_a_pending_release_are_selected() -> None:
    repository, inventory = setup()
    repository.orders["11111111-1111-1111-1111-111111111111"] = new_order(
        order_id="11111111-1111-1111-1111-111111111111", status=OrderStatus.CONFIRMED
    )
    reconciler(repository, inventory).run_once()
    assert repository.orders["11111111-1111-1111-1111-111111111111"].status is OrderStatus.CONFIRMED


def test_an_ambiguous_reservation_is_retried_and_confirmed_exactly_once() -> None:
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    created = new_order(order_id=ORDER_ID, status=OrderStatus.CREATED, created_at=MOMENT - timedelta(minutes=5))
    repository.orders[ORDER_ID] = created

    # the reservation happened in the inventory but the answer never arrived
    inventory.reserve(ORDER_ID, [RequestedLine("p-1001", 2)])
    assert inventory.stock["p-1001"] == 48
    inventory.calls.clear()

    report = reconciler(repository, inventory).run_once()

    assert report.confirmed == 1
    assert repository.orders[ORDER_ID].status is OrderStatus.CONFIRMED
    assert repository.orders[ORDER_ID].confirmed_at == MOMENT
    assert inventory.stock["p-1001"] == 48  # the replay reserved nothing a second time
    assert repository.event_types == [EVENT_ORDER_CONFIRMED]

    # a second pass changes nothing: the order is no longer CREATED
    second = reconciler(repository, inventory).run_once()
    assert second.confirmed == 0
    assert repository.event_types == [EVENT_ORDER_CONFIRMED]


def test_a_reservation_that_can_never_succeed_fails_the_order() -> None:
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    inventory.insufficient_at_reserve = True
    repository.orders[ORDER_ID] = new_order(
        order_id=ORDER_ID, status=OrderStatus.CREATED, created_at=MOMENT - timedelta(minutes=5)
    )

    report = reconciler(repository, inventory).run_once()

    assert report.failed == 1
    assert repository.orders[ORDER_ID].status is OrderStatus.FAILED
    assert repository.messages == ()
    assert inventory.stock["p-1001"] == 50  # nothing was reserved


def test_an_unreachable_inventory_leaves_the_order_created() -> None:
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    inventory.make_unavailable("reserve")
    repository.orders[ORDER_ID] = new_order(
        order_id=ORDER_ID, status=OrderStatus.CREATED, created_at=MOMENT - timedelta(minutes=5)
    )

    report = reconciler(repository, inventory).run_once()

    assert report.pending_reservations == 1
    assert report.confirmed == 0
    assert repository.orders[ORDER_ID].status is OrderStatus.CREATED
    assert repository.messages == ()


def test_a_young_created_order_is_left_to_the_request_that_is_creating_it() -> None:
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    repository.orders[ORDER_ID] = new_order(order_id=ORDER_ID, status=OrderStatus.CREATED, created_at=MOMENT)
    worker = ReleaseReconciler(
        repository=repository,
        inventory=inventory,
        interval_seconds=0.01,
        reservation_grace_seconds=30.0,
        now=fixed_clock(),
    )

    report = worker.run_once()

    assert report == report.__class__()
    assert repository.orders[ORDER_ID].status is OrderStatus.CREATED
    assert inventory.calls == []


def test_a_lost_confirmation_does_not_write_a_second_event() -> None:
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    repository.orders[ORDER_ID] = new_order(
        order_id=ORDER_ID, status=OrderStatus.CREATED, created_at=MOMENT - timedelta(minutes=5)
    )

    def someone_confirmed_it() -> None:
        from app.events.contracts import order_confirmed_event

        order = repository.orders[ORDER_ID].confirmed(at=MOMENT - timedelta(seconds=1))
        repository.force_confirm(
            ORDER_ID,
            at=MOMENT - timedelta(seconds=1),
            event=order_confirmed_event(order, occurred_at=MOMENT - timedelta(seconds=1)),
        )

    repository.hooks["mark_confirmed"] = someone_confirmed_it
    report = reconciler(repository, inventory).run_once()

    assert report.confirmed == 0
    assert repository.orders[ORDER_ID].status is OrderStatus.CONFIRMED
    assert repository.event_types == [EVENT_ORDER_CONFIRMED]


def test_the_report_accumulates_over_passes() -> None:
    repository, inventory = setup()
    worker = reconciler(repository, inventory)
    first = worker.run_once()
    second = worker.run_once()
    assert (first + second).released == first.released + second.released == 1
