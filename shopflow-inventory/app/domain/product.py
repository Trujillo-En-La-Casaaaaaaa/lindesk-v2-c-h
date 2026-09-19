"""Product entity and its validation (contracts.md §9/§10).

Pure domain code: nothing here imports a web framework or a database library.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.errors import InvalidInput

DEFAULT_CURRENCY = "USD"
_CURRENCY_LENGTH = 3


@dataclass(frozen=True, slots=True)
class Product:
    """One catalog entry.

    ``price`` is kept as :class:`~decimal.Decimal` (the store column is
    ``numeric(12,2)``, contracts.md §9) so no binary-float rounding can leak into
    the catalog. ``to_view`` is the only place that converts to the JSON number
    of the API contract (§4).
    """

    product_id: str
    sku: str
    name: str
    description: str = ""
    price: Decimal = Decimal("0.00")
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_id", _require_text(self.product_id, "productId"))
        object.__setattr__(self, "sku", _require_text(self.sku, "sku"))
        object.__setattr__(self, "name", _require_text(self.name, "name"))
        object.__setattr__(self, "description", _optional_text(self.description))
        object.__setattr__(self, "price", _require_price(self.price))
        object.__setattr__(self, "currency", _require_currency(self.currency))

    def to_view(self, available_quantity: int) -> dict[str, Any]:
        """Render the product view of contracts.md §4 with its current stock."""

        available = int(available_quantity)
        if available < 0:  # defensive: the ledger itself never goes negative
            available = 0
        return {
            "productId": self.product_id,
            "sku": self.sku,
            "name": self.name,
            "description": self.description,
            "price": float(self.price),
            "currency": self.currency,
            "availableQuantity": available,
            "inStock": available > 0,
        }


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string.")
    return value.strip()


def _optional_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidInput("description must be a string.")
    return value


def _require_price(value: object) -> Decimal:
    if isinstance(value, Decimal):
        price = value
    else:
        try:
            price = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise InvalidInput(f"price must be a decimal number, got {value!r}.") from exc
    if not price.is_finite():
        raise InvalidInput("price must be finite.")
    if price < 0:
        raise InvalidInput("price must not be negative.")
    return price.quantize(Decimal("0.01"))


def _require_currency(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) != _CURRENCY_LENGTH or not value.strip().isalpha():
        raise InvalidInput(f"currency must be a {_CURRENCY_LENGTH}-letter code.")
    return value.strip().upper()
