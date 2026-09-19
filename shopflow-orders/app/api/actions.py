"""Order action routes: ``POST /orders/{orderId}/cancel`` and ``/ship``.

The cancellation reason is validated by the domain (``400 invalid_reason`` after
trimming, at most 200 characters), never by the request model: an empty or
whitespace-only reason is a business failure, not a malformed payload.
"""

from __future__ import annotations

import asyncio
from functools import partial

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.api import correlation_id_of
from app.api.orders import OrderIdPath

router = APIRouter(tags=["orders"])


class CancelOrderBody(BaseModel):
    """Body of ``POST /orders/{orderId}/cancel`` (contracts.md §5)."""

    model_config = ConfigDict(extra="ignore")

    reason: str | None = None


@router.post("/orders/{orderId}/cancel")
async def cancel_order(request: Request, orderId: OrderIdPath, body: CancelOrderBody) -> JSONResponse:
    """Cancel a confirmed order, restoring its stock exactly once."""

    order = await asyncio.to_thread(
        partial(
            request.app.state.cancel_order,
            order_id=orderId,
            reason=body.reason,
            correlation_id=correlation_id_of(request),
        )
    )
    return JSONResponse(order.to_view(), status_code=200)


@router.post("/orders/{orderId}/ship")
async def ship_order(request: Request, orderId: OrderIdPath) -> JSONResponse:
    """Administrative action: mark a confirmed order as shipped."""

    order = await asyncio.to_thread(partial(request.app.state.ship_order, order_id=orderId))
    return JSONResponse(order.to_view(), status_code=200)


__all__ = ["CancelOrderBody", "router"]
