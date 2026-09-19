"""Inventory HTTP API (FastAPI) of shopflow-inventory."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.domain.stock import StockLine
from app.ports.repositories import CatalogRepository, StockRepository
from app.usecases.availability import check_availability
from app.usecases.release import release_stock
from app.usecases.reserve import reserve_stock

router = APIRouter(tags=["inventory"])

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


class LineIn(BaseModel):
    """One ``{productId, quantity}`` line of a request body (contracts.md §4)."""

    model_config = ConfigDict(extra="ignore")

    productId: str = Field(min_length=1, description="Seeded product id, for example 'p-1001'.")
    quantity: int = Field(gt=0, description="Requested units; must be greater than zero.")


class LinesBody(BaseModel):
    """Body of ``POST /inventory/availability``."""

    model_config = ConfigDict(extra="ignore")

    lines: list[LineIn] = Field(min_length=1)


class OrderLinesBody(BaseModel):
    """Body of ``POST /inventory/reservations`` and ``POST /inventory/releases``."""

    model_config = ConfigDict(extra="ignore")

    orderId: str = Field(min_length=1)
    lines: list[LineIn] = Field(min_length=1)


def catalog_of(request: Request) -> CatalogRepository:
    return request.app.state.catalog_repository


def stock_of(request: Request) -> StockRepository:
    return request.app.state.stock_repository


def _lines(body: LinesBody | OrderLinesBody) -> tuple[StockLine, ...]:
    return tuple(StockLine(product_id=line.productId, quantity=line.quantity) for line in body.lines)


@router.post("/inventory/availability")
async def availability(request: Request, body: LinesBody) -> JSONResponse:
    """Report availability of every line. Reads only: no stock is changed."""

    view: dict[str, Any] = await asyncio.to_thread(
        partial(
            check_availability,
            catalog=catalog_of(request),
            stock=stock_of(request),
            lines=_lines(body),
        )
    )
    return JSONResponse(view)


@router.post("/inventory/reservations")
async def reservations(
    request: Request,
    body: OrderLinesBody,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Reserve every line of an order, all-or-nothing and idempotent."""

    view: dict[str, Any] = await asyncio.to_thread(
        partial(
            reserve_stock,
            catalog=catalog_of(request),
            stock=stock_of(request),
            order_id=body.orderId,
            lines=_lines(body),
            idempotency_key=idempotency_key,
        )
    )
    return JSONResponse(view)


@router.post("/inventory/releases")
async def releases(
    request: Request,
    body: OrderLinesBody,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Restore what an order reserved, exactly once, and never fail on a retry."""

    view: dict[str, Any] = await asyncio.to_thread(
        partial(
            release_stock,
            stock=stock_of(request),
            order_id=body.orderId,
            lines=_lines(body),
            idempotency_key=idempotency_key,
        )
    )
    return JSONResponse(view)


def routes() -> Sequence[str]:
    """The paths this router owns (used by the structure tests)."""

    return tuple(route.path for route in router.routes)
