"""Administrative route guard (contracts.md §2).

``POST /api/orders/{orderId}/ship`` is the only administrative route in scope.
It requires ``X-ShopFlow-Role: admin``; the guard runs before routing, so a
request without the role never reaches ``shopflow-orders``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header

from app.upstream.errors import GatewayError

ADMIN_ROLE_HEADER = "X-ShopFlow-Role"
ADMIN_ROLE = "admin"


async def require_admin(
    x_shopflow_role: Annotated[str | None, Header(alias=ADMIN_ROLE_HEADER)] = None,
) -> None:
    """FastAPI dependency: fail closed unless the caller presents the admin role."""

    if x_shopflow_role is None or x_shopflow_role.strip() != ADMIN_ROLE:
        raise GatewayError(
            code="forbidden",
            status_code=403,
            message=f"Administrative route requires header {ADMIN_ROLE_HEADER}: {ADMIN_ROLE}.",
        )
