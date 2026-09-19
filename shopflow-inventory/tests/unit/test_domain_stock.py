"""Domain tests: the stock ledger, its lines and the never-negative invariant."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.errors import InsufficientStock, InvalidIdempotencyKey, InvalidInput
from app.domain.stock import (
    RELEASE,
    RESERVE,
    StockLevel,
    StockLine,
    StockMovement,
    canonical_lines,
    idempotency_key_for,
    movement_payload,
    normalize_lines,
    plan_decrements,
    plan_restoration,
    require_idempotency_key,
)


class TestStockLine:
    def test_accepts_a_positive_quantity(self) -> None:
        line = StockLine(product_id="p-1001", quantity=2)

        assert line.as_payload() == {"productId": "p-1001", "quantity": 2}

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_rejects_non_positive_quantities(self, quantity: int) -> None:
        with pytest.raises(InvalidInput):
            StockLine(product_id="p-1001", quantity=quantity)

    @pytest.mark.parametrize("product_id", ["", "   "])
    def test_rejects_empty_product_ids(self, product_id: str) -> None:
        with pytest.raises(InvalidInput):
            StockLine(product_id=product_id, quantity=1)


class TestStockLevel:
    def test_decrement_stays_within_the_ledger(self) -> None:
        level = StockLevel(product_id="p-1001", quantity=50)

        assert level.decrement(2).quantity == 48
        assert level.in_stock is True

    def test_decrement_refuses_to_oversell(self) -> None:
        level = StockLevel(product_id="p-1005", quantity=0)

        with pytest.raises(InsufficientStock) as error:
            level.decrement(1)

        assert error.value.product_id == "p-1005"
        assert error.value.available == 0
        assert error.value.code == "insufficient_stock"

    def test_never_negative_invariant_is_enforced_on_construction(self) -> None:
        with pytest.raises(InvalidInput):
            StockLevel(product_id="p-1001", quantity=-1)

    def test_increment_restores_units(self) -> None:
        assert StockLevel(product_id="p-1001", quantity=48).increment(2).quantity == 50


class TestLineNormalization:
    def test_duplicate_lines_are_aggregated_in_first_appearance_order(self) -> None:
        normalized = normalize_lines(
            [StockLine("p-1002", 1), StockLine("p-1001", 2), StockLine("p-1002", 3)]
        )

        assert [(line.product_id, line.quantity) for line in normalized] == [("p-1002", 4), ("p-1001", 2)]

    def test_canonical_lines_are_sorted_for_stable_idempotency_comparison(self) -> None:
        first = canonical_lines([StockLine("p-1002", 1), StockLine("p-1001", 2)])
        second = canonical_lines([StockLine("p-1001", 2), StockLine("p-1002", 1)])

        assert movement_payload(first) == movement_payload(second) == {
            "lines": [
                {"productId": "p-1001", "quantity": 2},
                {"productId": "p-1002", "quantity": 1},
            ]
        }


class TestPlanDecrements:
    def test_returns_one_decrement_per_line(self) -> None:
        plan = plan_decrements(
            [StockLine("p-1001", 2), StockLine("p-1004", 100)],
            {"p-1001": 50, "p-1004": 100},
        )

        assert plan == {"p-1001": 2, "p-1004": 100}

    def test_is_all_or_nothing(self) -> None:
        with pytest.raises(InsufficientStock) as error:
            plan_decrements(
                [StockLine("p-1001", 2), StockLine("p-1005", 1)],
                {"p-1001": 50, "p-1005": 0},
            )

        assert error.value.product_id == "p-1005"

    def test_a_product_without_a_ledger_row_counts_as_zero(self) -> None:
        with pytest.raises(InsufficientStock):
            plan_decrements([StockLine("p-9999", 1)], {})


class TestPlanRestoration:
    def test_restores_exactly_what_was_reserved(self) -> None:
        reserved = canonical_lines([StockLine("p-1001", 2)])

        assert plan_restoration(reserved, canonical_lines([StockLine("p-1001", 2)])) == {"p-1001": 2}

    def test_cannot_restore_more_than_was_reserved(self) -> None:
        reserved = canonical_lines([StockLine("p-1001", 2)])

        assert plan_restoration(reserved, canonical_lines([StockLine("p-1001", 50)])) == {"p-1001": 2}

    def test_products_that_were_never_reserved_restore_nothing(self) -> None:
        reserved = canonical_lines([StockLine("p-1001", 2)])

        assert plan_restoration(reserved, canonical_lines([StockLine("p-1002", 5)])) == {"p-1001": 2}


class TestIdempotencyKey:
    def test_expected_key_format(self) -> None:
        assert idempotency_key_for("o-1", RESERVE) == "order:o-1:reserve"
        assert idempotency_key_for("o-1", RELEASE) == "order:o-1:release"

    def test_accepts_the_expected_key(self) -> None:
        assert require_idempotency_key(order_id="o-1", operation=RESERVE, provided="order:o-1:reserve")

    @pytest.mark.parametrize("provided", [None, "", "order:o-1:release", "order:o-2:reserve"])
    def test_rejects_any_other_key(self, provided: str | None) -> None:
        with pytest.raises(InvalidIdempotencyKey) as error:
            require_idempotency_key(order_id="o-1", operation=RESERVE, provided=provided)

        assert error.value.code == "validation_error"


class TestStockMovement:
    def test_payload_is_the_canonical_line_list(self) -> None:
        movement = StockMovement(
            order_id="o-1",
            operation=RESERVE,
            lines=canonical_lines([StockLine("p-1001", 2)]),
            created_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        )

        assert movement.payload() == {"lines": [{"productId": "p-1001", "quantity": 2}]}

    def test_requires_at_least_one_line_and_a_known_operation(self) -> None:
        now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
        with pytest.raises(InvalidInput):
            StockMovement(order_id="o-1", operation=RESERVE, lines=(), created_at=now)
        with pytest.raises(InvalidInput):
            StockMovement(order_id="o-1", operation="decrement", lines=(StockLine("p-1001", 1),), created_at=now)
