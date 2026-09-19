"""Order read/write routes: ``/api/orders`` (orders)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from app.upstream.client import call_upstream
from app.upstream.errors import GatewayError
from app.validation.schemas import CreateOrderRequest, pagination_params

router = APIRouter(tags=["orders"])

CustomerIdQuery = Annotated[str, Query(min_length=1, description="Customer whose orders are listed.")]
LimitParam = Annotated[int | None, Query(ge=0)]
OffsetParam = Annotated[int | None, Query(ge=0)]


@router.post("/api/orders")
async def create_order(request: Request, payload: CreateOrderRequest) -> Response:
    """Validate the body, then forward it unchanged to ``shopflow-orders``."""

    return await call_upstream(
        request,
        public_path="/api/orders",
        json_body=payload.to_upstream_payload(),
    )


@router.get("/api/orders")
async def list_orders(
    request: Request,
    customerId: CustomerIdQuery,
    limit: LimitParam = None,
    offset: OffsetParam = None,
) -> Response:
    """Proxy the order list of a customer."""

    if not customerId.strip():
        raise GatewayError("validation_error", 400, "customerId must be a non-empty string.")
    params = {"customerId": customerId}
    params.update(pagination_params(limit, offset))
    return await call_upstream(request, public_path="/api/orders", query_params=params)


@router.get("/api/orders/{orderId}")
async def get_order(request: Request, orderId: str) -> Response:
    """Proxy a single order read."""

    return await call_upstream(
        request,
        public_path="/api/orders/{orderId}",
        path_params={"orderId": orderId},
    )
