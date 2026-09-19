"""Unit tests: catalogue and availability use cases against port doubles."""

from __future__ import annotations

import pytest

from app.domain.errors import InvalidInput, NotFound, UnknownProduct
from app.domain.stock import StockLine
from app.seed import SEED_PRODUCTS
from app.usecases.availability import check_availability
from app.usecases.catalog import get_product, list_products
from tests.support.fakes import FakeCatalogRepository, FakeInventoryStore, FakeStockRepository


def documented_levels() -> dict[str, int]:
    return {entry.product.product_id: entry.quantity for entry in SEED_PRODUCTS}


@pytest.fixture()
def store() -> FakeInventoryStore:
    return FakeInventoryStore.seeded()


@pytest.fixture()
def catalog(store: FakeInventoryStore) -> FakeCatalogRepository:
    return FakeCatalogRepository(list(store.products.values()))


@pytest.fixture()
def stock(store: FakeInventoryStore) -> FakeStockRepository:
    return FakeStockRepository(store)


class TestListProducts:
    def test_returns_the_five_seeded_products_with_their_stock(
        self, catalog: FakeCatalogRepository, stock: FakeStockRepository
    ) -> None:
        page = list_products(catalog=catalog, stock=stock)

        assert page["total"] == 5
        assert [item["productId"] for item in page["items"]] == ["p-1001", "p-1002", "p-1003", "p-1004", "p-1005"]
        assert page["items"][0]["availableQuantity"] == 50
        assert page["items"][0]["inStock"] is True
        assert page["items"][4]["availableQuantity"] == 0
        assert page["items"][4]["inStock"] is False

    def test_paginates_without_changing_the_total(
        self, catalog: FakeCatalogRepository, stock: FakeStockRepository
    ) -> None:
        page = list_products(catalog=catalog, stock=stock, limit=2, offset=1)

        assert page["total"] == 5
        assert [item["productId"] for item in page["items"]] == ["p-1002", "p-1003"]

    @pytest.mark.parametrize(("limit", "offset"), [(-1, 0), (None, -1)])
    def test_rejects_invalid_pagination(
        self, catalog: FakeCatalogRepository, stock: FakeStockRepository, limit: int | None, offset: int
    ) -> None:
        with pytest.raises(InvalidInput):
            list_products(catalog=catalog, stock=stock, limit=limit, offset=offset)


class TestGetProduct:
    def test_returns_the_product_view(self, catalog: FakeCatalogRepository, stock: FakeStockRepository) -> None:
        view = get_product(catalog=catalog, stock=stock, product_id="p-1003")

        assert view["productId"] == "p-1003"
        assert view["sku"] == "SF-HOOD-003"
        assert view["availableQuantity"] == 10
        assert view["inStock"] is True

    def test_unknown_product_raises_not_found(self, catalog: FakeCatalogRepository, stock: FakeStockRepository) -> None:
        with pytest.raises(NotFound) as error:
            get_product(catalog=catalog, stock=stock, product_id="p-9999")

        assert error.value.code == "not_found"


class TestCheckAvailability:
    def test_reports_sufficiency_per_line_without_touching_stock(
        self, catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
    ) -> None:
        before = store.snapshot()

        result = check_availability(
            catalog=catalog,
            stock=stock,
            lines=[StockLine("p-1001", 2), StockLine("p-1003", 11)],
        )

        assert result == {
            "available": False,
            "items": [
                {"productId": "p-1001", "requested": 2, "available": 50, "sufficient": True},
                {"productId": "p-1003", "requested": 11, "available": 10, "sufficient": False},
            ],
        }
        assert store.snapshot() == before
        assert store.movement_keys() == set()

    def test_all_lines_sufficient_means_available(
        self, catalog: FakeCatalogRepository, stock: FakeStockRepository
    ) -> None:
        result = check_availability(catalog=catalog, stock=stock, lines=[StockLine("p-1004", 100)])

        assert result["available"] is True
        assert result["items"][0]["sufficient"] is True

    def test_zero_stock_product_is_never_sufficient(
        self, catalog: FakeCatalogRepository, stock: FakeStockRepository
    ) -> None:
        result = check_availability(catalog=catalog, stock=stock, lines=[StockLine("p-1005", 1)])

        assert result["available"] is False
        assert result["items"][0] == {"productId": "p-1005", "requested": 1, "available": 0, "sufficient": False}

    def test_unknown_product_raises_and_changes_nothing(
        self, catalog: FakeCatalogRepository, stock: FakeStockRepository, store: FakeInventoryStore
    ) -> None:
        with pytest.raises(UnknownProduct):
            check_availability(catalog=catalog, stock=stock, lines=[StockLine("p-1001", 1), StockLine("p-9999", 1)])

        assert store.snapshot() == documented_levels()
