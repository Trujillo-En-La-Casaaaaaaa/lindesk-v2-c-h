"""Unit tests of the ship and read use cases (HANDOFF #4 and #5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.errors import InvalidStatusTransition, NotFound, ValidationError
from app.domain.order import OrderStatus
from app.usecases.get_order import get_order, list_orders, orders_view
from app.usecases.ship_order import ship_order
from tests.support.fakes import InMemoryOrderRepository, new_order

MOMENT = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
ORDER_ID = "0f14de0e-6f2e-4b7a-9f5b-1f3f1d9c1111"
UNKNOWN = "11111111-2222-3333-4444-555555555555"


def fixed_clock():
    def now() -> datetime:
        return MOMENT

    return now


def repository_with(*orders):
    repository = InMemoryOrderRepository()
    for order in orders:
        repository.orders[order.order_id] = order
    return repository


def test_shipping_a_confirmed_order_sets_status_and_timestamp() -> None:
    repository = repository_with(new_order(order_id=ORDER_ID))
    shipped = ship_order(repository=repository, order_id=ORDER_ID, now=fixed_clock())

    assert shipped.status is OrderStatus.SHIPPED
    assert shipped.shipped_at == MOMENT
    assert shipped.updated_at == MOMENT
    assert repository.orders[ORDER_ID].status is OrderStatus.SHIPPED


def test_shipping_an_unknown_order_is_not_found() -> None:
    repository = repository_with()
    with pytest.raises(NotFound):
        ship_order(repository=repository, order_id=UNKNOWN, now=fixed_clock())


@pytest.mark.parametrize(
    "status",
    [OrderStatus.CREATED, OrderStatus.FAILED, OrderStatus.SHIPPED, OrderStatus.CANCELLED],
)
def test_only_a_confirmed_order_may_ship(status: OrderStatus) -> None:
    repository = repository_with(new_order(order_id=ORDER_ID, status=status))
    with pytest.raises(InvalidStatusTransition) as raised:
        ship_order(repository=repository, order_id=ORDER_ID, now=fixed_clock())

    assert raised.value.code == "invalid_status_transition"
    assert raised.value.details["status"] == str(status)
    assert raised.value.details["targetStatus"] == "SHIPPED"
    assert repository.orders[ORDER_ID].status is status


def test_a_ship_race_is_reported_from_the_fresh_status() -> None:
    repository = repository_with(new_order(order_id=ORDER_ID))
    order = repository.orders[ORDER_ID]
    repository.hooks["mark_shipped"] = lambda: repository.orders.update(
        {ORDER_ID: order.cancelled(at=MOMENT, reason="raced")}
    )

    with pytest.raises(InvalidStatusTransition) as raised:
        ship_order(repository=repository, order_id=ORDER_ID, now=fixed_clock())

    assert raised.value.details["status"] == "CANCELLED"


def test_get_order_returns_the_stored_order_and_the_cancellation_fields() -> None:
    cancelled = new_order(order_id=ORDER_ID).cancelled(at=MOMENT, reason="changed mind")
    repository = repository_with(cancelled)
    view = get_order(repository=repository, order_id=ORDER_ID).to_view()

    assert view["status"] == "CANCELLED"
    assert view["cancelledAt"] == "2026-09-18T12:00:00Z"
    assert view["cancellationReason"] == "changed mind"
    assert view["shippedAt"] is None


def test_get_order_of_an_unknown_order_is_not_found() -> None:
    repository = repository_with()
    with pytest.raises(NotFound):
        get_order(repository=repository, order_id=UNKNOWN)


def test_order_list_is_scoped_to_the_customer_and_paginated() -> None:
    first = new_order(order_id="11111111-1111-1111-1111-111111111111", customer_id="c-1", created_at=MOMENT)
    second = new_order(
        order_id="22222222-2222-2222-2222-222222222222",
        customer_id="c-1",
        created_at=MOMENT + timedelta(minutes=1),
    )
    third = new_order(
        order_id="33333333-3333-3333-3333-333333333333",
        customer_id="c-2",
        created_at=MOMENT + timedelta(minutes=2),
    )
    repository = repository_with(first, second, third)

    page = list_orders(repository=repository, customer_id="c-1")
    assert page.total == 2
    assert [order.order_id for order in page.items] == [first.order_id, second.order_id]

    window = list_orders(repository=repository, customer_id="c-1", limit=1, offset=1)
    assert window.total == 2
    assert [order.order_id for order in window.items] == [second.order_id]

    view = orders_view(page)
    assert set(view) == {"items", "total"}
    assert view["total"] == 2
    assert view["items"][0]["orderId"] == first.order_id


@pytest.mark.parametrize("payload", [{"limit": -1}, {"offset": -1}])
def test_invalid_pagination_is_rejected(payload) -> None:
    repository = repository_with()
    with pytest.raises(ValidationError) as raised:
        list_orders(repository=repository, customer_id="c-1", **payload)
    assert raised.value.code == "validation_error"


def test_a_blank_customer_id_is_rejected() -> None:
    repository = repository_with()
    with pytest.raises(ValidationError):
        list_orders(repository=repository, customer_id="   ")
