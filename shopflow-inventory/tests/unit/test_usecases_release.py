"""Unit tests: the release use case restores exactly once and never fails a retry."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.errors import IdempotencyConflict, InvalidIdempotencyKey
from app.domain.stock import RELEASE, StockLine
from app.usecases.release import release_stock
from app.usecases.reserve import reserve_stock
from tests.support.fakes import FakeCatalogRepository, FakeInventoryStore, FakeStockRepository

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


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
):
    return reserve_stock(
        catalog=catalog,
        stock=stock,
        order_id=order_id,
        lines=lines,
        idempotency_key=f"order:{order_id}:reserve",
        now=NOW,
    )


_UNSET = object()


def release(
    stock: FakeStockRepository,
    *,
    order_id: str = "o-1",
    lines: tuple[StockLine, ...] = (StockLine("p-1001", 2),),
    key: object = _UNSET,
):
    provided = f"order:{order_id}:release" if key is _UNSET else key
    return release_stock(
        stock=stock,
        order_id=order_id,
        lines=lines,
        idempotency_key=provided,  # type: ignore[arg-type]
        now=NOW,
    )


def test_release_restores_the_reserved_quantities(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock)
    assert store.quantity_of("p-1001") == 48

    result = release(stock)

    assert result == {"orderId": "o-1", "restored": True, "replayed": False}
    assert store.quantity_of("p-1001") == 50
    assert len(store.movements_for("o-1", RELEASE)) == 1


def test_repeated_release_is_a_replay_without_stock_change(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock)
    release(stock)

    replayed = release(stock)

    assert replayed == {"orderId": "o-1", "restored": False, "replayed": True}
    assert store.quantity_of("p-1001") == 50
    assert len(store.movements_for("o-1", RELEASE)) == 1


def test_release_of_an_order_that_was_never_reserved_is_a_no_op(
    stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    before = store.snapshot()

    result = release(stock, order_id="o-never")

    assert result == {"orderId": "o-never", "restored": False, "replayed": False}
    assert store.snapshot() == before
    assert store.movement_keys() == set()


def test_a_no_op_release_does_not_block_a_later_reservation_and_release(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    release(stock, order_id="o-late")

    reserve(catalog, stock, order_id="o-late")
    assert store.quantity_of("p-1001") == 48

    restored = release(stock, order_id="o-late")

    assert restored == {"orderId": "o-late", "restored": True, "replayed": False}
    assert store.quantity_of("p-1001") == 50


def test_release_never_restores_more_than_was_reserved(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock, lines=(StockLine("p-1001", 2),))

    release(stock, lines=(StockLine("p-1001", 50),))

    assert store.quantity_of("p-1001") == 50


def test_release_with_a_different_body_conflicts(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock)
    release(stock)

    with pytest.raises(IdempotencyConflict) as error:
        release(stock, lines=(StockLine("p-1001", 1),))

    assert error.value.code == "idempotency_conflict"
    assert store.quantity_of("p-1001") == 50


@pytest.mark.parametrize("key", [None, "", "order:o-1:reserve"])
def test_a_wrong_idempotency_key_is_refused(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore, key: str | None
) -> None:
    reserve(catalog, stock)

    with pytest.raises(InvalidIdempotencyKey):
        release(stock, key=key)

    assert store.quantity_of("p-1001") == 48
    assert store.movements_for("o-1", RELEASE) == []


def test_a_release_of_an_unknown_product_is_a_no_op(
    catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    reserve(catalog, stock)

    result = release(stock, lines=(StockLine("p-9999", 1),))

    assert result == {"orderId": "o-1", "restored": True, "replayed": False}
    # only the reserved product is restored
    assert store.quantity_of("p-1001") == 50
    assert store.snapshot().get("p-9999") is None


def test_release_after_an_unknown_product_reservation_is_a_no_op(
    stock: FakeStockRepository, store: FakeInventoryStore
) -> None:
    """A reservation that never happened leaves nothing to release."""

    result = release(stock, order_id="o-unknown")

    assert result == {"orderId": "o-unknown", "restored": False, "replayed": False}
    assert store.movement_keys() == set()
