"""Catalog HTTP API (FastAPI) of shopflow-inventory (contracts.md §4)."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.domain.errors import InvalidInput
from app.ports.repositories import CatalogRepository, StockRepository
from app.usecases.catalog import get_product as get_product_usecase
from app.usecases.catalog import list_products as list_products_usecase

router = APIRouter(tags=["catalog"])

LimitParam = Annotated[str | None, Query(description="Maximum number of products to return.")]
OffsetParam = Annotated[str | None, Query(description="Number of products to skip.")]


def catalog_of(request: Request) -> CatalogRepository:
    return request.app.state.catalog_repository


def stock_of(request: Request) -> StockRepository:
    return request.app.state.stock_repository


def parse_pagination(limit: str | None, offset: str | None) -> tuple[int | None, int]:
    """Parse the documented ``?limit=&offset=`` parameters of contracts.md §4.

    An absent *or blank* value means "not provided" (the contract documents the
    request line as ``/products?limit=&offset=``, and the gateway omits both by
    default); a non-numeric or negative value is a malformed parameter, which is
    reported as ``400 validation_error`` by the domain rule.
    """

    parsed_limit = _optional_count(limit, "limit")
    parsed_offset = _optional_count(offset, "offset")
    return parsed_limit, 0 if parsed_offset is None else parsed_offset


def _optional_count(raw: str | None, field: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        raise InvalidInput(f"{field} must be an integer.", details={"parameter": field, "value": raw}) from None
    if value < 0:
        raise InvalidInput(
            f"{field} must be a non-negative integer.",
            details={"parameter": field, "value": raw},
        )
    return value


@router.get("/products")
async def list_products(
    request: Request,
    limit: LimitParam = None,
    offset: OffsetParam = None,
) -> JSONResponse:
    """Return ``{items, total}`` with every product's current stock."""

    parsed_limit, parsed_offset = parse_pagination(limit, offset)
    view: dict[str, Any] = await asyncio.to_thread(
        partial(
            list_products_usecase,
            catalog=catalog_of(request),
            stock=stock_of(request),
            limit=parsed_limit,
            offset=parsed_offset,
        )
    )
    return JSONResponse(view)


@router.get("/products/{productId}")
async def get_product(request: Request, productId: str) -> JSONResponse:
    """Return one product view, or ``404 not_found``."""

    view: dict[str, Any] = await asyncio.to_thread(
        partial(
            get_product_usecase,
            catalog=catalog_of(request),
            stock=stock_of(request),
            product_id=productId,
        )
    )
    return JSONResponse(view)
