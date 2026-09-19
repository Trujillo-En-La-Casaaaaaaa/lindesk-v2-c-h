"""Catalog use cases: ``list_products`` and ``get_product`` (contracts.md §4)."""

from __future__ import annotations

from typing import Any

from app.domain.errors import InvalidInput, NotFound
from app.ports.repositories import CatalogRepository, StockRepository

DEFAULT_OFFSET = 0


def list_products(
    *,
    catalog: CatalogRepository,
    stock: StockRepository,
    limit: int | None = None,
    offset: int = DEFAULT_OFFSET,
) -> dict[str, Any]:
    """Return ``{items, total}`` with ``availableQuantity`` read from the ledger."""

    limit, offset = _validate_page(limit=limit, offset=offset)
    products, total = catalog.list_products(limit=limit, offset=offset)
    quantities = stock.available_quantities([product.product_id for product in products])
    items = [product.to_view(quantities.get(product.product_id, 0)) for product in products]
    return {"items": items, "total": int(total)}


def get_product(*, catalog: CatalogRepository, stock: StockRepository, product_id: str) -> dict[str, Any]:
    """Return one product view, or raise :class:`NotFound` (``404 not_found``)."""

    if not isinstance(product_id, str) or not product_id.strip():
        raise InvalidInput("productId must be a non-empty string.")
    normalized = product_id.strip()
    product = catalog.get_product(normalized)
    if product is None:
        raise NotFound(f"Unknown product {normalized}.", details={"productId": normalized})
    quantities = stock.available_quantities([product.product_id])
    return product.to_view(quantities.get(product.product_id, 0))


def _validate_page(*, limit: int | None, offset: int) -> tuple[int | None, int]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise InvalidInput("limit must be a non-negative integer.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidInput("offset must be a non-negative integer.")
    return limit, offset
