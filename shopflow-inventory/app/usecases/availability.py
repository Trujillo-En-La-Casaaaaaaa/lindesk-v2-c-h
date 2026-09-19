"""Availability use case: ``check_availability`` (contracts.md §4).

Reads only: the stock ledger is never written here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.domain.errors import UnknownProduct
from app.domain.stock import StockLine, normalize_lines
from app.ports.repositories import CatalogRepository, StockRepository


def check_availability(
    *,
    catalog: CatalogRepository,
    stock: StockRepository,
    lines: Sequence[StockLine],
) -> dict[str, Any]:
    """Report per line whether the requested quantity is available right now."""

    requested = normalize_lines(lines)
    for line in requested:
        if catalog.get_product(line.product_id) is None:
            raise UnknownProduct(line.product_id)

    quantities = stock.available_quantities([line.product_id for line in requested])
    items = []
    for line in requested:
        available = max(int(quantities.get(line.product_id, 0)), 0)
        items.append(
            {
                "productId": line.product_id,
                "requested": line.quantity,
                "available": available,
                "sufficient": available >= line.quantity,
            }
        )
    return {"available": all(item["sufficient"] for item in items), "items": items}
