"""Correlation id middleware (contracts.md §1).

Every request may carry ``X-Correlation-Id``. When it is missing the gateway
generates a UUID, exposes the value to handlers, forwards it unchanged to the
owning service and echoes it on the response - including on error responses.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar, Token

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.upstream.errors import error_response

CORRELATION_ID_HEADER = "X-Correlation-Id"

_correlation_id: ContextVar[str | None] = ContextVar("shopflow_correlation_id", default=None)

logger = logging.getLogger("shopflow.gateway.correlation")


def new_correlation_id() -> str:
    """Generate a new correlation id."""

    return str(uuid.uuid4())


def extract_correlation_id(raw_header: str | None) -> str:
    """Return the client correlation id, or a fresh UUID when it is absent/blank."""

    if raw_header is None:
        return new_correlation_id()
    candidate = raw_header.strip()
    return candidate or new_correlation_id()


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current request, if any."""

    return _correlation_id.get()


def correlation_id_from_request(request: Request) -> str:
    """Resolve the correlation id of ``request`` without ever returning an empty value."""

    state_value = getattr(request.state, "correlation_id", None)
    if isinstance(state_value, str) and state_value.strip():
        return state_value
    context_value = get_correlation_id()
    if context_value:
        return context_value
    return extract_correlation_id(request.headers.get(CORRELATION_ID_HEADER))


class CorrelationIdMiddleware:
    """Pure ASGI middleware that owns the correlation id of every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = extract_correlation_id(self._incoming_header(scope))
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        token: Token[str | None] = _correlation_id.set(correlation_id)
        response_started = False

        async def send_with_correlation(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        except Exception:
            logger.exception("unhandled gateway failure", extra={"correlationId": correlation_id})
            if response_started:
                raise
            fallback = error_response(
                code="internal_error",
                status_code=500,
                correlation_id=correlation_id,
                message="Unexpected gateway error.",
            )
            await fallback(scope, receive, send_with_correlation)
        finally:
            _correlation_id.reset(token)

    @staticmethod
    def _incoming_header(scope: Scope) -> str | None:
        wanted = CORRELATION_ID_HEADER.lower().encode("latin-1")
        for name, value in scope.get("headers", []):
            if name.lower() == wanted:
                return value.decode("latin-1")
        return None
