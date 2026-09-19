"""Order action routes: cancel and ship (orders)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from app.security.admin_guard import require_admin
from app.upstream.client import call_upstream
from app.upstream.errors import GatewayError
from app.validation.schemas import CancelOrderRequest, cancellation_reason_error

router = APIRouter(tags=["orders"])


@router.post("/api/orders/{orderId}/cancel")
async def cancel_order(request: Request, orderId: str, payload: CancelOrderRequest) -> Response:
    """Validate the cancellation reason, then forward it to ``shopflow-orders``."""

    reason_error = cancellation_reason_error(payload.reason)
    if reason_error is not None:
        raise GatewayError("invalid_reason", 400, reason_error)
    return await call_upstream(
        request,
        public_path="/api/orders/{orderId}/cancel",
        path_params={"orderId": orderId},
        json_body={"reason": payload.reason},
    )


@router.post("/api/orders/{orderId}/ship", dependencies=[Depends(require_admin)])
async def ship_order(request: Request, orderId: str) -> Response:
    """Administrative route: guarded by ``X-ShopFlow-Role: admin`` before routing."""

    return await call_upstream(
        request,
        public_path="/api/orders/{orderId}/ship",
        path_params={"orderId": orderId},
    )
