"""Unit tests of the order aggregate and its status machine (HANDOFF #1)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.errors import InvalidStatusTransition, ValidationError
from app.domain.order import (
    ALLOWED_TRANSITIONS,
    InventoryReleaseState,
    Order,
    OrderLine,
    OrderStatus,
    RequestedLine,
    format_timestamp,
    line_ids,
    money_as_number,
    order_total,
    quantize_money,
    transition_allowed,
    validate_order_request,
)

MOMENT = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
ORDER_ID = "5b1f3c22-0a1e-4a8f-9b0e-2f4a6c8d0e11"


def make_order(status: OrderStatus = OrderStatus.CREATED) -> Order:
    lines = (
        OrderLine("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", 2, Decimal("19.99")),
        OrderLine("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", 1, Decimal("12.50")),
    )
    return Order(
        order_id=ORDER_ID,
        customer_id="c-1",
        status=status,
        lines=lines,
        total=order_total(lines),
        created_at=MOMENT,
        updated_at=MOMENT,
    )


def test_status_vocabulary_is_the_documented_one() -> None:
    assert [str(status) for status in OrderStatus] == ["CREATED", "CONFIRMED", "FAILED", "SHIPPED", "CANCELLED"]
    assert [str(state) for state in InventoryReleaseState] == ["NONE", "PENDING", "RELEASED"]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.CREATED, OrderStatus.CONFIRMED),
        (OrderStatus.CREATED, OrderStatus.FAILED),
        (OrderStatus.CONFIRMED, OrderStatus.SHIPPED),
        (OrderStatus.CONFIRMED, OrderStatus.CANCELLED),
    ],
)
def test_allowed_transitions(current: OrderStatus, target: OrderStatus) -> None:
    assert transition_allowed(current, target) is True
    order = make_order(current)
    moved = {
        OrderStatus.CONFIRMED: lambda: order.confirmed(at=MOMENT + timedelta(seconds=1)),
        OrderStatus.FAILED: lambda: order.failed(at=MOMENT + timedelta(seconds=1)),
        OrderStatus.SHIPPED: lambda: order.shipped(at=MOMENT + timedelta(seconds=1)),
        OrderStatus.CANCELLED: lambda: order.cancelled(at=MOMENT + timedelta(seconds=1), reason="changed mind"),
    }[target]()
    assert moved.status is target
    assert moved.updated_at == MOMENT + timedelta(seconds=1)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.CREATED, OrderStatus.SHIPPED),
        (OrderStatus.CREATED, OrderStatus.CANCELLED),
        (OrderStatus.CONFIRMED, OrderStatus.CONFIRMED),
        (OrderStatus.CONFIRMED, OrderStatus.FAILED),
        (OrderStatus.FAILED, OrderStatus.CONFIRMED),
        (OrderStatus.SHIPPED, OrderStatus.CANCELLED),
        (OrderStatus.CANCELLED, OrderStatus.SHIPPED),
    ],
)
def test_rejected_transitions_raise_invalid_status_transition(current: OrderStatus, target: OrderStatus) -> None:
    assert transition_allowed(current, target) is False
    order = make_order(current)
    with pytest.raises(InvalidStatusTransition) as raised:
        if target is OrderStatus.SHIPPED:
            order.shipped(at=MOMENT)
        elif target is OrderStatus.CANCELLED:
            order.cancelled(at=MOMENT, reason="why not")
        elif target is OrderStatus.CONFIRMED:
            order.confirmed(at=MOMENT)
        else:
            order.failed(at=MOMENT)
    assert raised.value.code == "invalid_status_transition"
    assert raised.value.details["orderId"] == ORDER_ID
    assert raised.value.details["status"] == str(current)
    assert raised.value.details["targetStatus"] == str(target)


def test_failed_and_terminal_states_have_no_successor() -> None:
    assert ALLOWED_TRANSITIONS[OrderStatus.FAILED] == frozenset()
    assert ALLOWED_TRANSITIONS[OrderStatus.SHIPPED] == frozenset()
    assert ALLOWED_TRANSITIONS[OrderStatus.CANCELLED] == frozenset()


def test_cancellation_sets_the_pending_release_state() -> None:
    cancelled = make_order(OrderStatus.CONFIRMED).cancelled(at=MOMENT, reason="customer changed their mind")
    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.cancelled_at == MOMENT
    assert cancelled.cancellation_reason == "customer changed their mind"
    assert cancelled.inventory_release_state is InventoryReleaseState.PENDING
    assert cancelled.with_release_state(InventoryReleaseState.RELEASED).inventory_release_state is (
        InventoryReleaseState.RELEASED
    )


def test_order_view_matches_contracts_section_3() -> None:
    order = replace(make_order(OrderStatus.CONFIRMED), confirmed_at=MOMENT)
    view = order.to_view()
    assert list(view) == [
        "orderId",
        "status",
        "customerId",
        "createdAt",
        "updatedAt",
        "confirmedAt",
        "shippedAt",
        "cancelledAt",
        "cancellationReason",
        "total",
        "lines",
    ]
    assert view["orderId"] == ORDER_ID
    assert view["status"] == "CONFIRMED"
    assert view["customerId"] == "c-1"
    assert view["createdAt"] == "2026-09-18T12:00:00Z"
    assert view["updatedAt"] == "2026-09-18T12:00:00Z"
    assert view["confirmedAt"] == "2026-09-18T12:00:00Z"
    assert view["shippedAt"] is None
    assert view["cancelledAt"] is None
    assert view["cancellationReason"] is None
    assert view["total"] == 52.48
    assert [line["productId"] for line in view["lines"]] == ["p-1001", "p-1002"]
    assert view["lines"][0] == {
        "productId": "p-1001",
        "sku": "SF-TEE-001",
        "name": "ShopFlow Classic Tee",
        "quantity": 2,
        "unitPrice": 19.99,
        "lineTotal": 39.98,
    }
    assert view["lines"][1]["lineTotal"] == 12.5


def test_money_helpers_quantise_to_two_decimals() -> None:
    assert quantize_money(Decimal("39.9800")) == Decimal("39.98")
    assert money_as_number(Decimal("39.98")) == 39.98
    assert order_total([OrderLine("p-1", "s", "n", 3, Decimal("0.005"))]) == Decimal("0.02")


def test_timestamps_are_iso_8601_utc_with_z() -> None:
    assert format_timestamp(datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)) == "2026-09-18T12:00:00Z"
    naive = datetime(2026, 9, 18, 12, 0, 0)
    assert format_timestamp(naive) == "2026-09-18T12:00:00Z"
    other_zone = datetime(2026, 9, 18, 14, 0, 0, tzinfo=UTC).astimezone(UTC)
    assert format_timestamp(other_zone) == "2026-09-18T14:00:00Z"


def test_event_lines_and_inventory_lines_use_the_documented_field_names() -> None:
    line = OrderLine("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", 2, Decimal("19.99"))
    assert line.to_event_line() == {"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}
    assert line.to_inventory_line() == {"productId": "p-1001", "quantity": 2}


def test_line_ids_preserve_the_requested_order() -> None:
    keys = line_ids(ORDER_ID, 3)
    assert sorted(keys) == list(keys)  # reading order_lines by id = request order
    assert len(set(keys)) == 3
    other = line_ids("11111111-2222-3333-4444-555555555555", 3)
    assert set(keys) & set(other) == set()


def test_validate_order_request_accepts_a_well_formed_request() -> None:
    requested = validate_order_request("c-1", [{"productId": "p-1001", "quantity": 2}])
    assert requested == (RequestedLine("p-1001", 2),)
    assert validate_order_request("c-1", [RequestedLine("p-1", 1)]) == (RequestedLine("p-1", 1),)


@pytest.mark.parametrize(
    "payload",
    [
        ("", [{"productId": "p-1001", "quantity": 1}]),
        ("   ", [{"productId": "p-1001", "quantity": 1}]),
        ("c-1", []),
        ("c-1", None),
        ("c-1", [{"productId": "p-1001", "quantity": 0}]),
        ("c-1", [{"productId": "p-1001", "quantity": -3}]),
        ("c-1", [{"productId": "p-1001", "quantity": 1.5}]),
        ("c-1", [{"productId": "p-1001", "quantity": True}]),
        ("c-1", [{"productId": "", "quantity": 1}]),
        ("c-1", ["p-1001"]),
    ],
)
def test_validate_order_request_rejects_every_violation(payload) -> None:
    customer_id, lines = payload
    with pytest.raises(ValidationError) as raised:
        validate_order_request(customer_id, lines)
    assert raised.value.code == "validation_error"
