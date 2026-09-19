"""Unit tests of the create-order use case (HANDOFF #2, acceptance criteria 1, 2, 11)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.errors import (
    InsufficientStock,
    InventoryUnavailable,
    UnknownProduct,
    ValidationError,
)
from app.domain.order import OrderStatus, RequestedLine
from app.events.contracts import EVENT_ORDER_CONFIRMED
from app.usecases.create_order import create_order
from tests.support.fakes import FakeInventory, InMemoryOrderRepository

MOMENT = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
CUSTOMER = "c-1"


def fixed_clock():
    def now() -> datetime:
        return MOMENT

    return now


def make_use_case():
    repository = InMemoryOrderRepository()
    inventory = FakeInventory()
    return repository, inventory


def test_creates_a_confirmed_order_with_the_product_snapshot() -> None:
    repository, inventory = make_use_case()
    order = create_order(
        repository=repository,
        inventory=inventory,
        customer_id=CUSTOMER,
        lines=[RequestedLine("p-1001", 2)],
        correlation_id="corr-1",
        now=fixed_clock(),
    )

    assert order.status is OrderStatus.CONFIRMED
    assert order.confirmed_at == MOMENT
    assert order.customer_id == CUSTOMER
    assert order.total == Decimal("39.98")
    assert order.inventory_release_state.value == "NONE"
    line = order.lines[0]
    assert (line.product_id, line.sku, line.name, line.quantity, line.unit_price) == (
        "p-1001",
        "SF-TEE-001",
        "ShopFlow Classic Tee",
        2,
        Decimal("19.99"),
    )

    # availability -> snapshot -> persist -> reserve
    assert [call[0] for call in inventory.calls] == ["availability", "reserve"]
    assert inventory.stock["p-1001"] == 48
    stored = repository.orders[order.order_id]
    assert stored.status is OrderStatus.CONFIRMED

    # exactly one order.confirmed outbox row, with the full §8 envelope
    assert repository.event_types == [EVENT_ORDER_CONFIRMED]
    message = repository.messages[0]
    assert message.payload["orderId"] == order.order_id
    assert message.payload["correlationId"] == "corr-1"
    assert message.payload["reason"] is None
    assert message.payload["lines"] == [{"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}]
    assert message.payload["eventId"] == message.event_id


def test_insufficient_stock_creates_no_order_and_reserves_nothing() -> None:
    repository, inventory = make_use_case()
    before = dict(inventory.stock)

    with pytest.raises(InsufficientStock) as raised:
        create_order(
            repository=repository,
            inventory=inventory,
            customer_id=CUSTOMER,
            lines=[RequestedLine("p-1005", 1)],
            now=fixed_clock(),
        )

    assert raised.value.code == "insufficient_stock"
    assert raised.value.details["productId"] == "p-1005"
    assert raised.value.details["requested"] == 1
    assert raised.value.details["available"] == 0
    assert repository.orders == {}
    assert repository.messages == ()
    assert inventory.stock == before
    assert [call[0] for call in inventory.calls] == ["availability"]


def test_unreachable_inventory_creates_no_order() -> None:
    repository, inventory = make_use_case()
    inventory.make_unavailable("availability")

    with pytest.raises(InventoryUnavailable) as raised:
        create_order(
            repository=repository,
            inventory=inventory,
            customer_id=CUSTOMER,
            lines=[RequestedLine("p-1001", 2)],
            now=fixed_clock(),
        )

    assert raised.value.code == "inventory_unavailable"
    assert repository.orders == {}
    assert repository.messages == ()
    assert inventory.stock["p-1001"] == 50


def test_unknown_product_is_reported_and_writes_nothing() -> None:
    repository, inventory = make_use_case()

    with pytest.raises(UnknownProduct):
        create_order(
            repository=repository,
            inventory=inventory,
            customer_id=CUSTOMER,
            lines=[RequestedLine("p-9999", 1)],
            now=fixed_clock(),
        )

    assert repository.orders == {}


def test_lost_reservation_race_fails_the_order_and_reserves_nothing() -> None:
    repository, inventory = make_use_case()
    inventory.insufficient_at_reserve = True

    with pytest.raises(InsufficientStock):
        create_order(
            repository=repository,
            inventory=inventory,
            customer_id=CUSTOMER,
            lines=[RequestedLine("p-1001", 2)],
            now=fixed_clock(),
        )

    assert len(repository.orders) == 1
    stored = next(iter(repository.orders.values()))
    assert stored.status is OrderStatus.FAILED
    assert repository.messages == ()  # a failed order publishes nothing
    assert inventory.stock["p-1001"] == 50  # all-or-nothing: nothing was reserved


def test_ambiguous_reservation_leaves_the_order_created_and_never_confirms_it() -> None:
    repository, inventory = make_use_case()
    inventory.make_unavailable("reserve")

    with pytest.raises(InventoryUnavailable) as raised:
        create_order(
            repository=repository,
            inventory=inventory,
            customer_id=CUSTOMER,
            lines=[RequestedLine("p-1001", 2)],
            now=fixed_clock(),
        )

    assert raised.value.code == "inventory_unavailable"
    stored = next(iter(repository.orders.values()))
    assert stored.status is OrderStatus.CREATED
    assert stored.confirmed_at is None
    assert repository.messages == ()


def test_the_idempotency_key_of_the_reservation_is_derived_from_the_order_id() -> None:
    repository, inventory = make_use_case()
    order = create_order(
        repository=repository,
        inventory=inventory,
        customer_id=CUSTOMER,
        lines=[RequestedLine("p-1002", 1)],
        now=fixed_clock(),
    )
    assert inventory.keys_used == [f"order:{order.order_id}:reserve"]


def test_a_lost_confirmation_race_reports_the_stored_order() -> None:
    repository, inventory = make_use_case()

    def lose_the_race() -> None:
        from app.events.contracts import order_confirmed_event

        order = next(iter(repository.orders.values())).confirmed(at=MOMENT)
        repository.force_confirm(
            order.order_id,
            at=MOMENT,
            event=order_confirmed_event(order, occurred_at=MOMENT),
        )

    repository.hooks["mark_confirmed"] = lose_the_race
    order = create_order(
        repository=repository,
        inventory=inventory,
        customer_id=CUSTOMER,
        lines=[RequestedLine("p-1001", 2)],
        now=fixed_clock(),
    )

    assert order.status is OrderStatus.CONFIRMED
    # one event only: the guarded update of the winner owns the outbox row
    assert repository.event_types == [EVENT_ORDER_CONFIRMED]


def test_request_timeout_budget_stops_the_inventory_calls() -> None:
    repository, inventory = make_use_case()
    ticks = iter([0.0, 1.0, 99.0, 99.0, 99.0])

    with pytest.raises(InventoryUnavailable) as raised:
        create_order(
            repository=repository,
            inventory=inventory,
            customer_id=CUSTOMER,
            lines=[RequestedLine("p-1001", 2)],
            request_timeout_seconds=5.0,
            monotonic=lambda: next(ticks),
            now=fixed_clock(),
        )

    assert "budget" in str(raised.value).lower()
    # the budget ran out while snapshotting the product: nothing was written yet
    assert repository.orders == {}
    assert inventory.stock["p-1001"] == 50


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [RequestedLine("p-1001", 0)],
        [RequestedLine("p-1001", -1)],
    ],
)
def test_invalid_payload_is_rejected_before_any_store_call(lines) -> None:
    repository, inventory = make_use_case()

    with pytest.raises(ValidationError):
        create_order(
            repository=repository,
            inventory=inventory,
            customer_id=CUSTOMER,
            lines=lines,
            now=fixed_clock(),
        )

    assert repository.calls == []
    assert inventory.calls == []
