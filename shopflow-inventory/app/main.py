"""FastAPI application factory of shopflow-inventory (``inventory-api``).

This module is the composition root: it reads the configuration (contracts.md
§11), builds the PostgreSQL adapters of the two ports and registers the routes of
contracts.md §4 plus ``/health`` and ``/ready``. The route handlers hold no
business logic - they translate HTTP into a use-case call and map the domain error
codes of contracts.md §12 onto HTTP statuses and the shared error envelope.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapters.postgres_catalog import PostgresCatalogRepository
from app.adapters.postgres_stock import PostgresStockRepository
from app.api.inventory import router as inventory_router
from app.api.products import router as products_router
from app.config import Settings, get_settings
from app.db.session import create_engine_from_settings, create_session_factory, database_is_reachable
from app.domain.errors import DomainError
from app.ports.repositories import CatalogRepository, StockRepository

logger = logging.getLogger("shopflow.inventory")

#: Domain error code (contracts.md §12) -> HTTP status.
STATUS_BY_CODE: dict[str, int] = {
    "validation_error": 400,
    "forbidden": 403,
    "not_found": 404,
    "unknown_product": 404,
    "insufficient_stock": 409,
    "idempotency_conflict": 409,
    "internal_error": 500,
}

OK = "ok"
READY = "ready"


def correlation_id_of(request: Request) -> str:
    """Echo ``X-Correlation-Id`` unchanged, or the empty string when absent (§1)."""

    return request.headers.get("X-Correlation-Id", "")


def error_payload(
    *,
    code: str,
    message: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The shared error envelope of contracts.md §1."""

    return {
        "error": {
            "code": code,
            "message": message,
            "correlationId": correlation_id,
            "details": dict(details or {}),
        }
    }


def error_response(
    *,
    code: str,
    message: str,
    correlation_id: str,
    status_code: int | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        error_payload(code=code, message=message, correlation_id=correlation_id, details=details),
        status_code=status_code if status_code is not None else STATUS_BY_CODE.get(code, 500),
    )


def create_app(
    *,
    settings: Settings | None = None,
    engine: Any | None = None,
    catalog_repository: CatalogRepository | None = None,
    stock_repository: StockRepository | None = None,
) -> FastAPI:
    """Build the application.

    Production callers use the defaults: the settings come from the environment
    and both ports are backed by the owned PostgreSQL store. Tests inject the
    ports (or their own engine) instead of switching the store.
    """

    resolved_settings = settings if settings is not None else get_settings()
    if engine is None and (catalog_repository is None or stock_repository is None):
        engine = create_engine_from_settings(resolved_settings)
    session_factory = create_session_factory(engine) if engine is not None else None
    if catalog_repository is None:
        assert session_factory is not None  # guaranteed by the branch above
        catalog_repository = PostgresCatalogRepository(session_factory)
    if stock_repository is None:
        assert session_factory is not None
        stock_repository = PostgresStockRepository(session_factory)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            dispose = getattr(engine, "dispose", None)
            if callable(dispose):
                dispose()

    app = FastAPI(
        title="shopflow-inventory",
        version="1.0.0",
        summary="ShopFlow product catalog and stock owner (contracts.md §4)",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.catalog_repository = catalog_repository
    app.state.stock_repository = stock_repository

    app.include_router(products_router)
    app.include_router(inventory_router)

    @app.exception_handler(DomainError)
    async def _handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return error_response(
            code=exc.code,
            message=exc.message,
            correlation_id=correlation_id_of(request),
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            code="validation_error",
            message="Malformed request payload or parameters.",
            correlation_id=correlation_id_of(request),
            details={"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 403:
            code = "forbidden"
        elif 400 <= exc.status_code < 500:
            code = "validation_error"
        else:
            code = "internal_error"
        return error_response(
            code=code,
            message=str(exc.detail),
            correlation_id=correlation_id_of(request),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error",
            extra={"correlationId": correlation_id_of(request), "path": request.url.path},
        )
        return error_response(
            code="internal_error",
            message="Unexpected server error.",
            correlation_id=correlation_id_of(request),
            status_code=500,
        )

    @app.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        """Liveness: the process answers. The store is not contacted."""

        return JSONResponse({"status": OK})

    @app.get("/ready", tags=["health"])
    async def ready(request: Request) -> JSONResponse:
        """Readiness: ``200`` only when the owned store answers, ``503`` otherwise."""

        if engine is None:
            return error_response(
                code="internal_error",
                message="Inventory store is not configured.",
                correlation_id=correlation_id_of(request),
                status_code=503,
            )
        reachable = await asyncio.to_thread(database_is_reachable, engine)
        if reachable:
            return JSONResponse({"status": READY})
        return error_response(
            code="internal_error",
            message="Inventory store is unreachable.",
            correlation_id=correlation_id_of(request),
            status_code=503,
        )

    return app


#: ASGI entry point used by uvicorn/Docker (``uvicorn app.main:app``). Building it
#: creates no connection: the engine connects lazily on the first request.
app = create_app()
