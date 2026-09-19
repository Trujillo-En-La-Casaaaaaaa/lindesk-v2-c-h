"""FastAPI application factory of shopflow-notifications (component: notification-api).

This module is the composition root. It reads the configuration (contracts.md §11),
wire the PostgreSQL adapter of the ``notification-repository-port`` and the HTTP
adapter of the ``provider-port``, build the router of the two handlers, and start
the ``order-event-consumer`` in a background thread of the same process (the
``shopflow-infra`` compose file runs one process per service, so the consumer must
live next to - and never instead of - the HTTP surface).

Routes (contracts.md §6): ``GET /health``, ``GET /ready`` and
``GET /notifications?orderId=``. Nothing else is exposed: the route handlers
translate HTTP into a port call and map the domain error codes of contracts.md §12
onto HTTP statuses and the shared error envelope of contracts.md §1.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapters.postgres_repository import PostgresNotificationRepository
from app.adapters.provider_http_client import HttpNotificationProvider, provider_is_reachable
from app.api.notifications import router as notifications_router
from app.config import Settings, get_settings
from app.consumer.consumer import (
    ConsumerRunner,
    FaultHooks,
    OrderEventConsumer,
    broker_is_reachable,
    fault_hooks_from_env,
)
from app.consumer.router import EventRouter
from app.db.session import create_engine_from_settings, create_session_factory, database_is_reachable
from app.domain.notification import DomainError
from app.handlers.orchestrator import NotificationOrchestrator
from app.ports.provider import NotificationProvider
from app.ports.repository import NotificationRepository

LOGGER = logging.getLogger("shopflow.notifications")

#: Domain error code (contracts.md §12) -> HTTP status.
STATUS_BY_CODE: dict[str, int] = {
    "validation_error": 400,
    "forbidden": 403,
    "not_found": 404,
    "idempotency_conflict": 409,
    "internal_error": 500,
}

OK = "ok"
READY = "ready"
NOT_READY = "not_ready"

#: Readiness probes: name -> callable answering "is this upstream reachable?".
ReadinessProbes = Mapping[str, Callable[[], bool]]


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


def _probe(probes: ReadinessProbes) -> dict[str, bool]:
    """Run every readiness probe; an exploding probe counts as unreachable."""

    return {name: _probe_one(probe) for name, probe in probes.items()}


def _probe_one(probe: Callable[[], bool]) -> bool:
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - a failing probe is exactly "not reachable"
        return False


def default_readiness_probes(*, settings: Settings, engine: Any) -> ReadinessProbes:
    """The three reachability checks required by handoff #7."""

    return {
        "store": lambda: engine is not None and database_is_reachable(engine),
        "broker": lambda: broker_is_reachable(
            settings.broker_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        "provider": lambda: provider_is_reachable(
            settings.provider_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
    }


def create_app(
    *,
    settings: Settings | None = None,
    engine: Any | None = None,
    repository: NotificationRepository | None = None,
    provider: NotificationProvider | None = None,
    readiness: ReadinessProbes | None = None,
    start_consumer: bool = True,
    fault_hooks: FaultHooks | None = None,
) -> FastAPI:
    """Build the application.

    Production callers use the defaults: settings from the environment, both ports
    backed by their real adapter, the consumer running in this process. Tests
    inject the ports (or their own engine) instead of switching the store, and can
    turn the consumer off with ``start_consumer=False``.
    """

    resolved_settings = settings if settings is not None else get_settings()

    created_engine = False
    if repository is None and engine is None:
        engine = create_engine_from_settings(resolved_settings)
        created_engine = True
    if repository is None:
        assert engine is not None  # guaranteed by the branch above
        repository = PostgresNotificationRepository(create_session_factory(engine))

    created_provider = provider is None
    if provider is None:
        provider = HttpNotificationProvider(
            resolved_settings.provider_base_url,
            timeout_seconds=resolved_settings.request_timeout_seconds,
            max_attempts=resolved_settings.provider_max_attempts,
            backoff_seconds=resolved_settings.provider_backoff_seconds,
            backoff_max_seconds=resolved_settings.provider_backoff_max_seconds,
        )

    hooks = fault_hooks if fault_hooks is not None else fault_hooks_from_env()
    if hooks.enabled:  # pragma: no cover - only under explicit fault injection
        LOGGER.warning("fault injection is enabled: %s", hooks)

    orchestrator = NotificationOrchestrator(
        repository=repository,
        provider=provider,
        recipient_domain=resolved_settings.recipient_domain,
        after_send=hooks.after_send,
    )
    event_router = EventRouter.from_orchestrator(orchestrator)

    consumer: OrderEventConsumer | None = None
    if resolved_settings.consumer_enabled:
        consumer = OrderEventConsumer(settings=resolved_settings, router=event_router, hooks=hooks)
    runner = ConsumerRunner(consumer) if (consumer is not None and start_consumer) else None

    probes: ReadinessProbes = dict(
        readiness
        if readiness is not None
        else default_readiness_probes(settings=resolved_settings, engine=engine)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if runner is not None:
            runner.start()
        try:
            yield
        finally:
            if runner is not None:
                await asyncio.to_thread(runner.stop)
            if created_provider:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()
            if created_engine and engine is not None:
                dispose = getattr(engine, "dispose", None)
                if callable(dispose):
                    dispose()

    app = FastAPI(
        title="shopflow-notifications",
        version="1.0.0",
        summary="ShopFlow customer-notification owner (contracts.md §6, §7, §8, §9)",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.notification_repository = repository
    app.state.notification_provider = provider
    app.state.event_router = event_router
    app.state.order_event_consumer = consumer
    app.state.consumer_runner = runner

    app.include_router(notifications_router)

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Echo ``X-Correlation-Id`` when the caller sent one (contracts.md §1)."""

        correlation_id = request.headers.get("X-Correlation-Id")
        response = await call_next(request)
        if correlation_id:
            response.headers["X-Correlation-Id"] = correlation_id
        return response

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
        LOGGER.exception(
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
        """Liveness: the process answers. No upstream is contacted."""

        return JSONResponse({"status": OK})

    @app.get("/ready", tags=["health"])
    async def ready() -> JSONResponse:
        """Readiness: ``200`` only when the store, the broker and the provider answer."""

        upstreams = await asyncio.to_thread(_probe, probes)
        if runner is not None:
            consumer_state = runner.status()
        elif consumer is not None:
            consumer_state = "separate-process"
        else:
            consumer_state = "disabled"
        body: dict[str, Any] = {
            "status": READY if all(upstreams.values()) else NOT_READY,
            "upstreams": upstreams,
            "consumer": consumer_state,
        }
        return JSONResponse(body, status_code=200 if all(upstreams.values()) else 503)

    return app


#: ASGI entry point used by uvicorn/Docker (``uvicorn app.main:app``). Building it
#: opens no connection: the engine connects lazily and the consumer starts on the
#: lifespan startup event.
app = create_app()
