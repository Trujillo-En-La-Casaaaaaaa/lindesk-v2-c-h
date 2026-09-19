"""Catalog routes: ``GET /api/products`` and ``GET /api/products/{productId}`` (inventory)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from app.upstream.client import call_upstream
from app.validation.schemas import pagination_params

router = APIRouter(tags=["catalog"])

LimitParam = Annotated[int | None, Query(ge=0, description="Maximum number of products to return.")]
OffsetParam = Annotated[int | None, Query(ge=0, description="Number of products to skip.")]


@router.get("/api/products")
async def list_products(request: Request, limit: LimitParam = None, offset: OffsetParam = None) -> Response:
    """Proxy the product list of ``shopflow-inventory``."""

    return await call_upstream(
        request,
        public_path="/api/products",
        query_params=pagination_params(limit, offset),
    )


@router.get("/api/products/{productId}")
async def get_product(request: Request, productId: str) -> Response:
    """Proxy a single product read of ``shopflow-inventory``."""

    return await call_upstream(
        request,
        public_path="/api/products/{productId}",
        path_params={"productId": productId},
    )
