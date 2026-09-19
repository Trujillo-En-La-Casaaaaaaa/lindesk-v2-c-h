"""Unit tests of the cancellation policy (HANDOFF #3 rules 1-6)."""

from __future__ import annotations

import pytest

from app.domain.cancellation import (
    MAX_REASON_LENGTH,
    CancellationAction,
    decide_cancellation,
    normalize_reason,
    raise_for_decision,
)
from app.domain.errors import InvalidReason, OrderNotCancellable, OrderShipped
from app.domain.order import OrderStatus
from tests.support.fakes import new_order


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "   ",
        "\t\n ",
        123,
        [],
    ],
)
def test_invalid_reason_is_rejected(reason: object) -> None:
    with pytest.raises(InvalidReason) as raised:
        normalize_reason(reason)
    assert raised.value.code == "invalid_reason"


def test_reason_is_trimmed_before_validation_and_storage() -> None:
    assert normalize_reason("  customer changed their mind  ") == "customer changed their mind"
    assert normalize_reason("x") == "x"
    assert normalize_reason("  " + "a" * MAX_REASON_LENGTH + "  ") == "a" * MAX_REASON_LENGTH


def test_reason_length_is_bounded_by_the_trimmed_value() -> None:
    assert normalize_reason("a" * MAX_REASON_LENGTH) == "a" * MAX_REASON_LENGTH
    with pytest.raises(InvalidReason) as raised:
        normalize_reason("a" * (MAX_REASON_LENGTH + 1))
    assert raised.value.details["length"] == MAX_REASON_LENGTH + 1
    assert raised.value.details["maxLength"] == MAX_REASON_LENGTH


def test_reason_validation_happens_before_any_status_rule() -> None:
    """Rule 1 precedes rules 2-6: a bad reason is rejected whatever the order is."""

    shipped = new_order(status=OrderStatus.SHIPPED)
    with pytest.raises(InvalidReason):
        # normalize_reason runs first (as the use case does), so the shipped status
        # is never even inspected for an empty reason.
        raise_for_decision(decide_cancellation(shipped, reason=normalize_reason("   ")))


def test_shipped_order_is_rejected_with_order_shipped() -> None:
    decision = decide_cancellation(new_order(status=OrderStatus.SHIPPED), reason="too late")
    assert decision.action is CancellationAction.REJECT_SHIPPED
    with pytest.raises(OrderShipped) as raised:
        raise_for_decision(decision)
    assert raised.value.code == "order_shipped"


def test_already_cancelled_order_is_a_safe_repeat() -> None:
    cancelled = new_order(status=OrderStatus.CONFIRMED).cancelled(at=new_order().created_at, reason="changed mind")
    decision = decide_cancellation(cancelled, reason="changed mind")
    assert decision.action is CancellationAction.ALREADY_CANCELLED
    assert decision.is_repeat is True
    raise_for_decision(decision)  # does not raise


@pytest.mark.parametrize("status", [OrderStatus.CREATED, OrderStatus.FAILED])
def test_other_non_cancellable_statuses_are_rejected(status: OrderStatus) -> None:
    decision = decide_cancellation(new_order(status=status), reason="changed mind")
    assert decision.action is CancellationAction.REJECT_NOT_CANCELLABLE
    with pytest.raises(OrderNotCancellable) as raised:
        raise_for_decision(decision)
    assert raised.value.code == "order_not_cancellable"
    assert raised.value.details["status"] == str(status)


def test_only_confirmed_orders_are_cancelled() -> None:
    decision = decide_cancellation(new_order(status=OrderStatus.CONFIRMED), reason="changed mind")
    assert decision.action is CancellationAction.APPLY
    assert decision.applies is True


def test_the_reason_is_carried_by_the_decision() -> None:
    decision = decide_cancellation(new_order(status=OrderStatus.CONFIRMED), reason="trimmed")
    assert decision.reason == "trimmed"
    assert decision.order.status is OrderStatus.CONFIRMED
