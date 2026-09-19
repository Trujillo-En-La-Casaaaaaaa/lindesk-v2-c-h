"""Domain tests: the product entity and its validation (contracts.md §4/§9)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.errors import InvalidInput
from app.domain.product import Product


def make_product(**overrides: object) -> Product:
    values: dict[str, object] = {
        "product_id": "p-1001",
        "sku": "SF-TEE-001",
        "name": "ShopFlow Classic Tee",
        "description": "Heavyweight cotton t-shirt.",
        "price": Decimal("19.99"),
        "currency": "USD",
    }
    values.update(overrides)
    return Product(**values)  # type: ignore[arg-type]


def test_product_view_matches_the_contract_shape() -> None:
    view = make_product().to_view(50)

    assert list(view) == [
        "productId",
        "sku",
        "name",
        "description",
        "price",
        "currency",
        "availableQuantity",
        "inStock",
    ]
    assert view == {
        "productId": "p-1001",
        "sku": "SF-TEE-001",
        "name": "ShopFlow Classic Tee",
        "description": "Heavyweight cotton t-shirt.",
        "price": 19.99,
        "currency": "USD",
        "availableQuantity": 50,
        "inStock": True,
    }


def test_in_stock_is_false_at_zero_and_never_negative() -> None:
    assert make_product().to_view(0)["inStock"] is False
    assert make_product().to_view(0)["availableQuantity"] == 0
    # Defensive: even a negative reading is reported as zero available.
    assert make_product().to_view(-3)["availableQuantity"] == 0


def test_price_is_quantized_to_two_decimals() -> None:
    product = make_product(price=Decimal("19.9"))

    assert product.price == Decimal("19.90")
    assert product.to_view(1)["price"] == 19.9


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_id", ""),
        ("product_id", "   "),
        ("sku", ""),
        ("name", ""),
        ("currency", "US"),
        ("currency", "USDD"),
        ("price", Decimal("-0.01")),
    ],
)
def test_invalid_products_are_rejected(field: str, value: object) -> None:
    with pytest.raises(InvalidInput):
        make_product(**{field: value})


def test_description_defaults_to_empty_string() -> None:
    product = make_product(description=None)

    assert product.description == ""
