"""Liveness and readiness of the gateway (contracts.md §2)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.middleware.correlation import correlation_id_from_request
from app.routing.route_registry import SERVICE_HEALTH_PATHS, Service
from app.upstream.client import UpstreamClient, UpstreamRequest, UpstreamUnavailableError
from app.upstream.errors import error_response

logger = logging.getLogger("shopflow.gateway.health")

router = APIRouter(tags=["health"])

OK = "ok"
UNAVAILABLE = "unavailable"


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness: the gateway itself is up. No upstream is contacted."""

    return JSONResponse({"status": "ok"})


@router.get("/ready")
async def ready(request: Request) -> Response:
    """Readiness: both owning services answer their own ``/health``."""

    correlation_id = correlation_id_from_request(request)
    client: UpstreamClient = request.app.state.upstream_client

    services = (Service.ORDERS, Service.INVENTORY)
    states = await asyncio.gather(
        *(probe_upstream(client, service, correlation_id) for service in services)
    )
    upstreams = {service.value: state for service, state in zip(services, states, strict=True)}

    if all(state == OK for state in upstreams.values()):
        return JSONResponse({"status": "ready", "upstreams": upstreams})

    return error_response(
        code="upstream_unavailable",
        status_code=503,
        correlation_id=correlation_id,
        message="One or more upstream services are not ready.",
        details={"upstreams": upstreams},
    )


async def probe_upstream(client: UpstreamClient, service: Service, correlation_id: str) -> str:
    """Return ``ok`` when ``service`` answers its health path with a 2xx."""

    upstream_request = UpstreamRequest(
        service=service,
        method="GET",
        path=SERVICE_HEALTH_PATHS[service],
        idempotent=True,
    )
    try:
        result = await client.call(upstream_request, correlation_id)
    except UpstreamUnavailableError:
        return UNAVAILABLE
    except Exception:  # pragma: no cover - defensive: readiness must never raise
        logger.exception("readiness probe failed service=%s", service.value, extra={"correlationId": correlation_id})
        return UNAVAILABLE
    return OK if 200 <= result.status_code < 300 else UNAVAILABLE
