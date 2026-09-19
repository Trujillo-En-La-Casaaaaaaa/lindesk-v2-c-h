"""ShopFlow notification-provider emulator — HTTP surface (contracts.md §7).

Routes (exactly, nothing else is exposed):

======================  ====================================================
``POST /messages``      accept one notification message (idempotent)
``GET /messages``       deterministic read-back affordance for tests
``GET /health``         liveness
``GET /ready``          readiness
======================  ====================================================

The emulator replaces the production notification provider in the local
ShopFlow stack. It is deterministic, idempotent by ``Idempotency-Key``, keeps
state in memory only, and never performs network I/O.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.store import (
    SUPPORTED_CHANNELS,
    SUPPORTED_TEMPLATES,
    IdempotencyConflict,
    MessageStore,
    deterministic_message_id,
)

LOGGER = logging.getLogger("shopflow.notification-provider-emulator")

CORRELATION_HEADER = "X-Correlation-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: The emulator's whole state. Module level so the ASGI app and the tests share it;
#: ``docker compose restart`` (or ``down -v``) resets it, which is expected for a
#: test double.
store = MessageStore()

app = FastAPI(
    title="ShopFlow notification-provider emulator",
    version="1.0.0",
    # Expose exactly the routes of contracts.md §7 and nothing else.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class MessageRequest(BaseModel):
    """Body of ``POST /messages`` (contracts.md §7)."""

    model_config = ConfigDict(extra="ignore")

    channel: str = Field(default="email")
    to: str
    template: str
    orderId: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: str) -> str:
        candidate = (value or "").strip().lower()
        if candidate not in SUPPORTED_CHANNELS:
            raise ValueError(f"channel must be one of {sorted(SUPPORTED_CHANNELS)}")
        return candidate

    @field_validator("template")
    @classmethod
    def _validate_template(cls, value: str) -> str:
        candidate = (value or "").strip()
        if candidate not in SUPPORTED_TEMPLATES:
            raise ValueError(f"template must be one of {sorted(SUPPORTED_TEMPLATES)}")
        return candidate

    @field_validator("to")
    @classmethod
    def _validate_to(cls, value: str) -> str:
        candidate = (value or "").strip()
        if not candidate:
            raise ValueError("to must be a non-empty string")
        return candidate

    @field_validator("orderId")
    @classmethod
    def _validate_order_id(cls, value: str) -> str:
        candidate = (value or "").strip()
        if not candidate:
            raise ValueError("orderId must be a non-empty string")
        return candidate

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON object")
        return value


# --------------------------------------------------------------------------- #
# Correlation id (contracts.md §1): accept or generate, always echo
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    correlation_id = (request.headers.get(CORRELATION_HEADER) or "").strip() or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = correlation_id
    return response


def correlation_id_of(request: Request) -> str:
    """Return the correlation id established by the middleware."""

    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the single error envelope of contracts.md §1."""

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "correlationId": correlation_id,
                "details": details or {},
            }
        },
    )


# --------------------------------------------------------------------------- #
# Exception handlers — every failure uses the contracts.md §1 envelope
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    problems = [
        {"loc": list(problem.get("loc", [])), "msg": problem.get("msg", ""), "type": problem.get("type", "")}
        for problem in exc.errors()
    ]
    return error_response(
        status_code=400,
        code="validation_error",
        message="request body is not a valid provider message",
        correlation_id=correlation_id_of(request),
        details={"problems": jsonable_encoder(problems)},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "not_found" if exc.status_code in (404, 405) else "internal_error"
    message = str(exc.detail) if exc.detail else "request could not be handled"
    return error_response(
        status_code=exc.status_code,
        code=code,
        message=message,
        correlation_id=correlation_id_of(request),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    LOGGER.exception("unhandled provider-emulator error: %s", exc)
    return error_response(
        status_code=500,
        code="internal_error",
        message="the notification-provider emulator failed to handle the request",
        correlation_id=correlation_id_of(request),
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.post("/messages")
async def post_messages(
    request: Request,
    message: MessageRequest,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
) -> JSONResponse:
    """Accept one notification message (contracts.md §7).

    * Requires ``Idempotency-Key: order:{orderId}:{template}``.
    * A repeated key returns the original ``messageId`` and records no second message.
    * A repeated key with a different body returns ``409 idempotency_conflict``.
    """

    correlation_id = correlation_id_of(request)
    key = (idempotency_key or "").strip()
    if not key:
        return error_response(
            status_code=400,
            code="validation_error",
            message=f"{IDEMPOTENCY_HEADER} header is required",
            correlation_id=correlation_id,
        )

    try:
        recorded, replayed = store.accept(
            idempotency_key=key,
            order_id=message.orderId,
            template=message.template,
            to=message.to,
            channel=message.channel,
            payload=message.payload,
        )
    except IdempotencyConflict:
        return error_response(
            status_code=409,
            code="idempotency_conflict",
            message=f"{IDEMPOTENCY_HEADER} '{key}' was already used with a different body",
            correlation_id=correlation_id,
        )

    LOGGER.info(
        "accepted message %s (orderId=%s template=%s channel=%s replayed=%s correlationId=%s)",
        recorded.message_id,
        recorded.order_id,
        recorded.template,
        recorded.channel,
        replayed,
        correlation_id,
    )
    return JSONResponse(status_code=200, content=recorded.accepted_body())


@app.get("/messages")
async def get_messages(
    request: Request,
    orderId: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return accepted messages, optionally filtered by ``orderId`` (contracts.md §7)."""

    items = [message.summary() for message in store.messages(orderId)]
    return {"items": items, "total": len(items)}


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up and serving."""

    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness: the in-memory store needs no dependency, so this is always ready."""

    return {"status": "ready"}


__all__ = ["app", "store", "deterministic_message_id", "MessageRequest"]
