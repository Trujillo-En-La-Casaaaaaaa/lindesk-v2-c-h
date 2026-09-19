"""FastAPI application factory of the ShopFlow gateway."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app import __version__
from app.config import Settings, get_settings
from app.middleware.correlation import CorrelationIdMiddleware, correlation_id_from_request
from app.routes import actions, health, orders, products
from app.routing.route_registry import RouteRegistry
from app.upstream.client import UpstreamClient
from app.upstream.errors import (
    CODE_MESSAGES,
    GatewayError,
    error_response,
    http_status_to_code,
)

logger = logging.getLogger("shopflow.gateway")

LIFESPAN_LOGGER = "shopflow.gateway.lifespan"
GATEWAY_LOGGER_NAME = "shopflow.gateway"


def configure_logging(level: int | str = logging.INFO) -> logging.Logger:
    """Make the gateway's own log records visible.

    The gateway logs every upstream call, retry and normalized failure. Without
    an explicit level those records would be dropped by the default root level
    (WARNING). A stream handler is only added when nothing else is attached, so
    the gateway never duplicates the host's logging configuration.
    """

    gateway_logger = logging.getLogger(GATEWAY_LOGGER_NAME)
    gateway_logger.setLevel(level)
    if not gateway_logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s"))
        gateway_logger.addHandler(handler)
    return gateway_logger


configure_logging()


def create_app(
    settings: Settings | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the gateway application.

    ``settings`` defaults to the process environment (contracts.md §11) and
    ``upstream_transport`` allows the upstream HTTP transport to be injected by
    tests. The gateway keeps no database connection and no durable state.
    """

    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.getLogger(LIFESPAN_LOGGER).info(
            "gateway starting orders_base_url=%s inventory_base_url=%s timeout_seconds=%s",
            resolved_settings.orders_base_url,
            resolved_settings.inventory_base_url,
            resolved_settings.request_timeout_seconds,
        )
        upstream_client = UpstreamClient(resolved_settings, transport=upstream_transport)
        await upstream_client.start()
        app.state.upstream_client = upstream_client
        try:
            yield
        finally:
            await upstream_client.aclose()

    app = FastAPI(
        title="ShopFlow Gateway",
        version=__version__,
        lifespan=lifespan,
        # The gateway exposes exactly the public API of contracts.md §2.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved_settings
    app.state.route_registry = RouteRegistry()

    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(GatewayError, _gateway_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unexpected_error_handler)

    app.include_router(health.router)
    app.include_router(products.router)
    app.include_router(orders.router)
    app.include_router(actions.router)
    return app


async def _gateway_error_handler(request: Request, exc: GatewayError) -> Response:
    correlation_id = correlation_id_from_request(request)
    logger.warning(
        "gateway error code=%s status=%s",
        exc.code,
        exc.status_code,
        extra={"correlationId": correlation_id},
    )
    return error_response(
        code=exc.code,
        status_code=exc.status_code,
        message=exc.message,
        correlation_id=correlation_id,
        details=exc.details,
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    correlation_id = correlation_id_from_request(request)
    errors: list[dict[str, Any]] = []
    for error in exc.errors():
        errors.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    logger.warning("request validation failed errors=%s", errors, extra={"correlationId": correlation_id})
    return error_response(
        code="validation_error",
        status_code=400,
        correlation_id=correlation_id,
        details={"errors": errors},
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    correlation_id = correlation_id_from_request(request)
    code = http_status_to_code(exc.status_code)
    message = CODE_MESSAGES[code] if code != "internal_error" else CODE_MESSAGES["internal_error"]
    return error_response(code=code, status_code=exc.status_code, message=message, correlation_id=correlation_id)


async def _unexpected_error_handler(request: Request, exc: Exception) -> Response:
    correlation_id = correlation_id_from_request(request)
    logger.exception("unhandled gateway failure", extra={"correlationId": correlation_id})
    return error_response(code="internal_error", status_code=500, correlation_id=correlation_id)


app = create_app()
