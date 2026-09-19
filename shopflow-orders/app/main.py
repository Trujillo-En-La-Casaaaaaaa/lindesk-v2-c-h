"""FastAPI application factory of shopflow-orders (component: ``orders-api``).

This module is the composition root: it reads the configuration (contracts.md §11),
builds the PostgreSQL adapters of the repository/outbox ports, the inventory HTTP
client and the broker client, registers the routes of contracts.md §5 plus
``/health`` and ``/ready``, and starts the two background relays (the outbox
publisher and the release reconciler) inside the service process.

The handlers hold no business logic - they translate HTTP into one use-case call and
map the domain error codes of contracts.md §12 onto HTTP statuses and the shared
error envelope. ``app/domain`` and ``app/usecases`` never import anything from here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapters.inventory_http_client import InventoryHttpClient
from app.adapters.postgres_order_repository import PostgresOrderRepository
from app.adapters.postgres_outbox import PostgresOutbox
from app.adapters.rabbitmq_publisher import RabbitMqPublisher
from app.api import correlation_id_of
from app.api.actions import router as actions_router
from app.api.orders import router as orders_router
from app.config import Settings, get_settings
from app.db.session import create_engine_from_settings, create_session_factory, database_is_reachable
from app.domain.errors import DomainError
from app.ports.inventory import InventoryPort
from app.ports.order_repository import OrderRepository
from app.ports.outbox import MessageBroker, OutboxStore
from app.usecases.cancel_order import cancel_order
from app.usecases.create_order import create_order
from app.usecases.get_order import get_order, list_orders
from app.usecases.ship_order import ship_order
from app.workers.outbox_publisher import OutboxPublisher
from app.workers.release_reconciler import ReleaseReconciler

logger = logging.getLogger("shopflow.orders")

#: contracts.md §12: domain error code -> HTTP status.
STATUS_BY_CODE: dict[str, int] = {
    "validation_error": 400,
    "invalid_reason": 400,
    "not_found": 404,
    "unknown_product": 404,
    "insufficient_stock": 409,
    "order_shipped": 409,
    "order_not_cancellable": 409,
    "invalid_status_transition": 409,
    "idempotency_conflict": 409,
    "inventory_unavailable": 503,
    "internal_error": 500,
}

OK = "ok"
READY = "ready"
NOT_READY = "not_ready"


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
    """Build the JSON error answer for one failure."""

    return JSONResponse(
        error_payload(code=code, message=message, correlation_id=correlation_id, details=details),
        status_code=status_code if status_code is not None else STATUS_BY_CODE.get(code, 500),
    )


def create_app(
    *,
    settings: Settings | None = None,
    engine: Any | None = None,
    order_repository: OrderRepository | None = None,
    inventory: InventoryPort | None = None,
    outbox_store: OutboxStore | None = None,
    broker: MessageBroker | None = None,
    store_probe: Callable[[], bool] | None = None,
    start_workers: bool | None = None,
) -> FastAPI:
    """Build the application.

    Production callers use the defaults: the settings come from the environment and
    every port is backed by its real adapter. Tests inject ports (or their own
    engine/probes) instead of switching the store.
    """

    resolved = settings if settings is not None else get_settings()
    owns_engine = False
    if engine is None and (order_repository is None or outbox_store is None):
        engine = create_engine_from_settings(resolved)
        owns_engine = True
    session_factory = create_session_factory(engine) if engine is not None else None
    if session_factory is not None:
        if outbox_store is None:
            outbox_store = PostgresOutbox(session_factory)
        if order_repository is None:
            order_repository = PostgresOrderRepository(session_factory, outbox_store)  # type: ignore[arg-type]
    if order_repository is None:
        raise ValueError("an order repository is required (either an engine or an injected repository)")
    if inventory is None:
        inventory = InventoryHttpClient(
            resolved.inventory_base_url,
            timeout_seconds=resolved.request_timeout_seconds,
        )
    run_workers = resolved.start_workers if start_workers is None else bool(start_workers)
    if broker is None and run_workers:
        broker = RabbitMqPublisher(
            resolved.broker_url,
            exchange=resolved.broker_exchange,
            connect_timeout_seconds=resolved.broker_connect_timeout_seconds,
        )
    if outbox_store is None:
        raise ValueError("an outbox store is required (either an engine or an injected store)")

    publisher = (
        OutboxPublisher(
            store=outbox_store,
            broker=broker,
            batch_size=resolved.outbox_batch_size,
            poll_interval_seconds=resolved.outbox_poll_interval_seconds,
            max_backoff_seconds=resolved.outbox_max_backoff_seconds,
        )
        if run_workers and broker is not None
        else None
    )
    reconciler = (
        ReleaseReconciler(
            repository=order_repository,
            inventory=inventory,
            interval_seconds=resolved.release_retry_interval_seconds,
            batch_size=resolved.reconciler_batch_size,
            reservation_grace_seconds=resolved.reservation_grace_seconds,
            timeout_seconds=resolved.request_timeout_seconds,
        )
        if run_workers
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop_event = threading.Event()
        tasks: list[asyncio.Task[Any]] = []
        if publisher is not None:
            tasks.append(asyncio.create_task(asyncio.to_thread(publisher.run_forever, stop_event)))
        if reconciler is not None:
            tasks.append(asyncio.create_task(asyncio.to_thread(reconciler.run_forever, stop_event)))
        try:
            yield
        finally:
            stop_event.set()
            for task in tasks:
                await asyncio.gather(task, return_exceptions=True)
            if publisher is not None:
                publisher.close()
            _close_quietly(inventory)
            if owns_engine:
                dispose = getattr(engine, "dispose", None)
                if callable(dispose):
                    dispose()

    app = FastAPI(
        title="shopflow-orders",
        version="1.0.0",
        summary="ShopFlow order lifecycle, cancellation rules and transactional outbox (contracts.md §5)",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.engine = engine
    app.state.order_repository = order_repository
    app.state.inventory = inventory
    app.state.outbox_store = outbox_store
    app.state.broker = broker
    app.state.outbox_publisher = publisher
    app.state.release_reconciler = reconciler

    # The use cases are wired once, as callables the routes invoke in a thread.
    app.state.create_order = partial(
        create_order,
        repository=order_repository,
        inventory=inventory,
        request_timeout_seconds=resolved.request_timeout_seconds,
    )
    app.state.cancel_order = partial(
        cancel_order,
        repository=order_repository,
        inventory=inventory,
        request_timeout_seconds=resolved.request_timeout_seconds,
    )
    app.state.ship_order = partial(ship_order, repository=order_repository)
    app.state.get_order = partial(get_order, repository=order_repository)
    app.state.list_orders = partial(list_orders, repository=order_repository)

    app.include_router(orders_router)
    app.include_router(actions_router)

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
        """Liveness: the process answers. No dependency is contacted."""

        return JSONResponse({"status": OK})

    @app.get("/ready", tags=["health"])
    async def ready() -> JSONResponse:
        """Readiness: the owned store and the broker must both answer.

        The inventory is reported as well (order creation needs it), but the two
        dependencies this service *owns* - the store and the broker - decide the
        status, exactly as HANDOFF #8 requires.
        """

        upstreams = {
            "store": await asyncio.to_thread(_store_reachable, engine, store_probe),
            "broker": await asyncio.to_thread(_broker_reachable, broker),
            "inventory": await asyncio.to_thread(
                _inventory_reachable, inventory, resolved.request_timeout_seconds
            ),
        }
        ready_ = bool(upstreams["store"] and upstreams["broker"])
        return JSONResponse(
            {"status": READY if ready_ else NOT_READY, "upstreams": upstreams},
            status_code=200 if ready_ else 503,
        )

    return app


def _store_reachable(engine: Any | None, store_probe: Callable[[], bool] | None) -> bool:
    if store_probe is not None:
        try:
            return bool(store_probe())
        except Exception:  # noqa: BLE001 - a probe failure is a not-ready answer
            logger.debug("store probe failed", exc_info=True)
            return False
    if engine is None:
        return True
    return database_is_reachable(engine)


def _broker_reachable(broker: MessageBroker | None) -> bool:
    if broker is None:
        return True
    try:
        return bool(broker.is_reachable())
    except Exception:  # noqa: BLE001 - a probe failure is a not-ready answer
        logger.debug("broker probe failed", exc_info=True)
        return False


def _inventory_reachable(inventory: InventoryPort | None, timeout_seconds: float) -> bool:
    if inventory is None:
        return True
    try:
        return bool(inventory.is_reachable(timeout_seconds=timeout_seconds))
    except Exception:  # noqa: BLE001 - a probe failure is a not-ready answer
        logger.debug("inventory probe failed", exc_info=True)
        return False


def _close_quietly(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - closing is best effort
        logger.debug("closing a resource failed", exc_info=True)


app = create_app()


__all__ = ["STATUS_BY_CODE", "app", "create_app", "error_payload", "error_response"]
