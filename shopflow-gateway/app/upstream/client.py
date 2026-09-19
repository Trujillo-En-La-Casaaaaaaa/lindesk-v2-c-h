"""HTTP upstream client: timeout, bounded retry, correlation forwarding.

* the request timeout comes from ``REQUEST_TIMEOUT_SECONDS``;
* idempotent GETs get one bounded retry (transport failure or 502/503/504);
* state-changing POSTs are never retried, so the gateway cannot amplify a
  cancellation or an order creation;
* timeouts and connection errors surface as ``503 upstream_unavailable``;
* every call forwards the request correlation id unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings
from app.middleware.correlation import CORRELATION_ID_HEADER, correlation_id_from_request
from app.routing.route_registry import RouteRegistry, Service, base_url_for
from app.upstream.errors import (
    CODE_MESSAGES,
    GatewayError,
    error_response,
    normalize_upstream_error,
)

#: Logger used for every upstream call; tests assert "no upstream call" on it.
UPSTREAM_REQUEST_LOGGER = "shopflow.gateway.upstream"
DEFAULT_JSON_CONTENT_TYPE = "application/json; charset=utf-8"

logger = logging.getLogger(UPSTREAM_REQUEST_LOGGER)

#: Upstream statuses that are retried once for idempotent GETs only.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({502, 503, 504})


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    """A single call to an owning service."""

    service: Service
    method: str
    path: str
    params: Mapping[str, str] | None = None
    json_body: Any = None
    idempotent: bool = False
    idempotency_key: str | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class UpstreamResult:
    """The raw outcome of an upstream call."""

    service: Service
    method: str
    path: str
    status_code: int
    content: bytes
    content_type: str | None
    body: Any
    attempts: int


class UpstreamUnavailableError(Exception):
    """Raised when an upstream cannot be reached or does not answer in time."""

    def __init__(self, service: Service, kind: str, detail: str = "") -> None:
        self.service = service
        self.kind = kind
        self.detail = detail
        super().__init__(f"{service.value} upstream unavailable ({kind})")


class UpstreamProtocolError(Exception):
    """Raised when an upstream answers with something the gateway cannot proxy."""

    def __init__(self, service: Service, path: str, detail: str = "") -> None:
        self.service = service
        self.path = path
        self.detail = detail
        super().__init__(f"{service.value}{path} returned a non-JSON response body")


def classify_transport_error(exc: httpx.RequestError) -> str:
    """Classify a transport failure without leaking details to the client."""

    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_error"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol_error"
    return "transport_error"


class UpstreamClient:
    """Thin async HTTP client for ``shopflow-orders`` and ``shopflow-inventory``."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._client = client
        self._owns_client = client is None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("upstream client has not been started")
        return self._client

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.request_timeout_seconds),
                transport=self._transport,
                follow_redirects=False,
            )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def call(self, upstream_request: UpstreamRequest, correlation_id: str) -> UpstreamResult:
        """Perform the upstream call, honouring timeout, retry and correlation rules."""

        base_url = base_url_for(upstream_request.service, self._settings)
        url = f"{base_url}{upstream_request.path}"
        headers = {
            "Accept": DEFAULT_JSON_CONTENT_TYPE,
            CORRELATION_ID_HEADER: correlation_id,
        }
        if upstream_request.idempotency_key:
            headers["Idempotency-Key"] = upstream_request.idempotency_key

        timeout = upstream_request.timeout_seconds or self._settings.request_timeout_seconds
        attempts_allowed = 1 + (self._settings.get_retry_attempts if upstream_request.idempotent else 0)
        attempt = 0

        while True:
            attempt += 1
            try:
                response = await self.http.request(
                    upstream_request.method,
                    url,
                    params=upstream_request.params,
                    json=upstream_request.json_body,
                    headers=headers,
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                kind = classify_transport_error(exc)
                logger.warning(
                    "upstream transport failure service=%s method=%s path=%s kind=%s attempt=%s",
                    upstream_request.service.value,
                    upstream_request.method,
                    upstream_request.path,
                    kind,
                    attempt,
                    extra={
                        "correlationId": correlation_id,
                        "event": "upstream_transport_failure",
                        "upstream": {
                            "service": upstream_request.service.value,
                            "method": upstream_request.method,
                            "path": upstream_request.path,
                        },
                    },
                )
                if attempt < attempts_allowed:
                    continue
                raise UpstreamUnavailableError(upstream_request.service, kind, str(exc)) from exc

            if (
                upstream_request.idempotent
                and response.status_code in RETRYABLE_STATUS_CODES
                and attempt < attempts_allowed
            ):
                logger.warning(
                    "upstream transient status service=%s method=%s path=%s status=%s attempt=%s retrying=true",
                    upstream_request.service.value,
                    upstream_request.method,
                    upstream_request.path,
                    response.status_code,
                    attempt,
                    extra={
                        "correlationId": correlation_id,
                        "event": "upstream_retry",
                        "upstream": {
                            "service": upstream_request.service.value,
                            "method": upstream_request.method,
                            "path": upstream_request.path,
                        },
                    },
                )
                continue

            result = self._build_result(response, upstream_request, attempt)
            logger.info(
                "upstream call service=%s method=%s path=%s status=%s attempts=%s",
                result.service.value,
                result.method,
                result.path,
                result.status_code,
                result.attempts,
                extra={
                    "correlationId": correlation_id,
                    "event": "upstream_call",
                    "upstream": {
                        "service": result.service.value,
                        "method": result.method,
                        "path": result.path,
                        "status": result.status_code,
                        "attempts": result.attempts,
                    },
                },
            )
            return result

    @staticmethod
    def _build_result(response: httpx.Response, upstream_request: UpstreamRequest, attempt: int) -> UpstreamResult:
        content = response.content
        body: Any = None
        if content:
            try:
                body = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                body = None
        return UpstreamResult(
            service=upstream_request.service,
            method=upstream_request.method,
            path=upstream_request.path,
            status_code=response.status_code,
            content=content,
            content_type=response.headers.get("content-type"),
            body=body,
            attempts=attempt,
        )


async def call_upstream(
    request: Request,
    *,
    public_path: str,
    path_params: Mapping[str, str] | None = None,
    query_params: Mapping[str, str] | None = None,
    json_body: Any = None,
) -> Response:
    """Proxy one validated public request to the owning service."""

    registry: RouteRegistry = request.app.state.route_registry
    client: UpstreamClient = request.app.state.upstream_client
    correlation_id = correlation_id_from_request(request)

    try:
        route = registry.require_route(request.method, public_path)
    except KeyError:
        logger.error("unregistered gateway route method=%s path=%s", request.method, public_path)
        return error_response(
            code="internal_error",
            status_code=500,
            correlation_id=correlation_id,
            message="Unexpected gateway error.",
        )

    upstream_request = UpstreamRequest(
        service=route.service,
        method=route.method,
        path=registry.build_upstream_path(route, path_params),
        params=dict(query_params) if query_params else None,
        json_body=json_body,
        idempotent=route.idempotent,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )

    try:
        result = await client.call(upstream_request, correlation_id)
    except UpstreamUnavailableError:
        return error_response(
            code="upstream_unavailable",
            status_code=503,
            correlation_id=correlation_id,
            message=CODE_MESSAGES["upstream_unavailable"],
            details={"service": route.service.value},
        )
    except UpstreamProtocolError:
        return error_response(code="internal_error", status_code=500, correlation_id=correlation_id)

    if 200 <= result.status_code < 300:
        if result.content and result.body is None:
            logger.error(
                "upstream returned a non-JSON success body service=%s path=%s",
                result.service.value,
                result.path,
                extra={"correlationId": correlation_id},
            )
            return error_response(code="internal_error", status_code=500, correlation_id=correlation_id)
        return Response(
            content=result.content,
            status_code=result.status_code,
            media_type=result.content_type or DEFAULT_JSON_CONTENT_TYPE,
        )

    return normalize_upstream_error(
        status_code=result.status_code,
        body=result.body,
        correlation_id=correlation_id,
        service=result.service.value,
    )


def gateway_error_response(error: GatewayError, correlation_id: str) -> Response:
    """Render a :class:`GatewayError` as the gateway envelope."""

    return error_response(
        code=error.code,
        status_code=error.status_code,
        message=error.message,
        correlation_id=correlation_id,
        details=error.details,
    )
