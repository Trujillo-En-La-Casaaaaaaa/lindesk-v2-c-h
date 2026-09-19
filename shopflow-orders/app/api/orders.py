"""Order routes: ``POST /orders``, ``GET /orders``, ``GET /orders/{orderId}``.

The handlers translate HTTP into one use-case call and nothing else: all rules live
in ``app/usecases`` and ``app/domain``, and every failure leaves as the snake_case
error envelope of contracts.md §1 (see the exception handlers in ``app/main.py``).
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api import correlation_id_of
from app.domain.order import RequestedLine

router = APIRouter(tags=["orders"])

#: contracts.md §1 identifiers are strings; the gateway bounds them the same way.
MAX_IDENTIFIER_LENGTH = 256

CustomerIdQuery = Annotated[str, Query(min_length=1, description="Customer whose orders are listed.")]
LimitQuery = Annotated[int | None, Query(ge=0, description="Optional page size.")]
OffsetQuery = Annotated[int | None, Query(ge=0, description="Optional page offset.")]
OrderIdPath = Annotated[str, Path(min_length=1)]


class LineIn(BaseModel):
    """One ``{productId, quantity}`` line of the request body."""

    model_config = ConfigDict(extra="ignore")

    productId: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    quantity: int = Field(..., strict=True, gt=0, description="Positive integer quantity.")


class CreateOrderBody(BaseModel):
    """Body of ``POST /orders`` (contracts.md §5)."""

    model_config = ConfigDict(extra="ignore")

    customerId: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    lines: list[LineIn] = Field(..., min_length=1)


def requested_lines(body: CreateOrderBody) -> tuple[RequestedLine, ...]:
    """Turn the validated body into the domain's requested lines."""

    return tuple(RequestedLine(product_id=line.productId, quantity=line.quantity) for line in body.lines)


@router.post("/orders", status_code=201)
async def create_order(request: Request, body: CreateOrderBody) -> JSONResponse:
    """Create an order after validating availability, then reserve its stock."""

    order = await asyncio.to_thread(
        partial(
            request.app.state.create_order,
            customer_id=body.customerId,
            lines=requested_lines(body),
            correlation_id=correlation_id_of(request),
        )
    )
    return JSONResponse(order.to_view(), status_code=201)


@router.get("/orders")
async def list_orders(
    request: Request,
    customerId: CustomerIdQuery,
    limit: LimitQuery = None,
    offset: OffsetQuery = None,
) -> JSONResponse:
    """Return one page of a customer's orders (``{items, total}``)."""

    page = await asyncio.to_thread(
        partial(
            request.app.state.list_orders,
            customer_id=customerId,
            limit=limit,
            offset=offset,
        )
    )
    return JSONResponse({"items": [order.to_view() for order in page.items], "total": page.total})


@router.get("/orders/{orderId}")
async def get_order(request: Request, orderId: OrderIdPath) -> JSONResponse:
    """Return the order view of contracts.md §3 (including the cancellation fields)."""

    order = await asyncio.to_thread(partial(request.app.state.get_order, order_id=orderId))
    return JSONResponse(order.to_view())


__all__ = [
    "CustomerIdQuery",
    "LineIn",
    "LimitQuery",
    "OffsetQuery",
    "OrderIdPath",
    "CreateOrderBody",
    "requested_lines",
    "router",
]
