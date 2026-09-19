"""Unit tests: the reservation use case is all-or-nothing and idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.errors import IdempotencyConflict, InsufficientStock, InvalidIdempotencyKey, UnknownProduct
from app.domain.stock import RESERVE, StockLine
from app.usecases.reserve import reserve_stock
from tests.support.fakes import FakeCatalogRepository, FakeInventoryStore, FakeStockRepository


@pytest.fixture()
def store() -> FakeInventoryStore:
    return FakeInventoryStore.seeded()


@pytest.fixture()
def catalog(store: FakeInventoryStore) -> FakeCatalogRepository:
    return FakeCatalogRepository(list(store.products.values()))


@pytest.fixture()
def stock(store: FakeInventoryStore) -> FakeStockRepository:
    return FakeStockRepository(store)


def reserve(
    catalog: FakeCatalogRepository,
    stock: FakeStockRepository,
    *,
    order_id: str = "o-1",
    lines: tuple[StockLine, ...] = (StockLine("p-1001", 2),),
    key: str | None = "order:o-1:reserve",
):
    return reserve_stock(
        catalog=catalog,
        stock=stock,
        order_id=order_id,
        lines=lines,
        idempotency_key=key,
        now=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
    )


def test_reservation_decrements_stock_and_records_one_movement(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    result = reserve(catalog, stock)

    assert result == {"orderId": "o-1", "reserved": True, "replayed": False}
    assert store.quantity_of("p-1001") == 48
    movements = store.movements_for("o-1", RESERVE)
    assert len(movements) == 1
    assert movements[0].payload() == {"lines": [{"productId": "p-1001", "quantity": 2}]}


def test_multiple_lines_are_decremented_together(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock, lines=(StockLine("p-1003", 4), StockLine("p-1002", 5)))

    assert store.quantity_of("p-1003") == 6
    assert store.quantity_of("p-1002") == 20


def test_replaying_the_same_body_changes_nothing(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock)
    replayed = reserve(catalog, stock)

    assert replayed == {"orderId": "o-1", "reserved": True, "replayed": True}
    assert store.quantity_of("p-1001") == 48
    assert len(store.movements_for("o-1", RESERVE)) == 1


def test_a_line_order_permutation_is_still_the_same_body(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    lines = (StockLine("p-1002", 5), StockLine("p-1001", 2))
    reserve(catalog, stock, lines=lines)

    replayed = reserve(catalog, stock, lines=tuple(reversed(lines)))

    assert replayed["replayed"] is True
    assert store.quantity_of("p-1001") == 48
    assert store.quantity_of("p-1002") == 20


def test_a_different_body_conflicts_without_changing_stock(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock)

    with pytest.raises(IdempotencyConflict) as error:
        reserve(catalog, stock, lines=(StockLine("p-1001", 3),))

    assert error.value.code == "idempotency_conflict"
    assert store.quantity_of("p-1001") == 48
    assert len(store.movements_for("o-1", RESERVE)) == 1


def test_insufficient_stock_changes_nothing_at_all(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    before = store.snapshot()

    with pytest.raises(InsufficientStock) as error:
        reserve(
            catalog,
            stock,
            order_id="o-2",
            key="order:o-2:reserve",
            lines=(StockLine("p-1001", 2), StockLine("p-1005", 1)),
        )

    assert error.value.product_id == "p-1005"
    assert store.snapshot() == before
    assert store.movement_keys() == set()


def test_reserving_zero_stock_leaves_no_movement_row(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    with pytest.raises(InsufficientStock):
        reserve(catalog, stock, order_id="o-3", key="order:o-3:reserve", lines=(StockLine("p-1005", 1),))

    assert store.movement_keys() == set()
    assert store.quantity_of("p-1005") == 0


def test_unknown_product_changes_nothing(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    before = store.snapshot()

    with pytest.raises(UnknownProduct):
        reserve(catalog, stock, order_id="o-4", key="order:o-4:reserve", lines=(StockLine("p-9999", 1),))

    assert store.snapshot() == before
    assert store.movement_keys() == set()


@pytest.mark.parametrize("key", [None, "", "order:o-1:release", "order:other:reserve"])
def test_a_wrong_idempotency_key_is_refused(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore, key: str | None
) -> None:
    with pytest.raises(InvalidIdempotencyKey):
        reserve(catalog, stock, key=key)

    assert store.snapshot()["p-1001"] == 50
    assert store.movement_keys() == set()


def test_the_same_order_can_reserve_again_after_a_failed_attempt(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    with pytest.raises(InsufficientStock):
        reserve(catalog, stock, lines=(StockLine("p-1001", 51),))

    result = reserve(catalog, stock)

    assert result["reserved"] is True
    assert store.quantity_of("p-1001") == 48
