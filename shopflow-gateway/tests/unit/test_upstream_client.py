"""Timeout, bounded retry and correlation rules of the upstream client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.routing.route_registry import Service
from app.upstream.client import (
    UpstreamClient,
    UpstreamRequest,
    UpstreamUnavailableError,
    classify_transport_error,
)

BASE_SETTINGS = Settings(
    orders_base_url="http://orders.test:8000",
    inventory_base_url="http://inventory.test:8000",
    request_timeout_seconds=1.0,
    get_retry_attempts=1,
)


def run(
    scenario: Callable[[UpstreamClient], Coroutine[Any, Any, Any]],
    transport: httpx.AsyncBaseTransport,
    settings: Settings = BASE_SETTINGS,
) -> Any:
    async def main() -> Any:
        client = UpstreamClient(settings, transport=transport)
        await client.start()
        try:
            return await scenario(client)
        finally:
            await client.aclose()

    return asyncio.run(main())


def test_correlation_and_idempotency_headers_are_forwarded() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"orderId": "o-1"})

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(
            service=Service.ORDERS,
            method="POST",
            path="/orders",
            json_body={"customerId": "c-1"},
            idempotency_key="order:o-1:reserve",
        )
        return await client.call(request, "cid-abc")

    result = run(scenario, httpx.MockTransport(handler))

    assert seen["headers"]["x-correlation-id"] == "cid-abc"
    assert seen["headers"]["idempotency-key"] == "order:o-1:reserve"
    assert seen["url"] == "http://orders.test:8000/orders"
    assert result.body == {"orderId": "o-1"}
    assert result.attempts == 1
    assert result.status_code == 200


def test_query_parameters_are_forwarded() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(
            service=Service.INVENTORY,
            method="GET",
            path="/products",
            params={"limit": "5", "offset": "0"},
            idempotent=True,
        )
        return await client.call(request, "cid")

    run(scenario, httpx.MockTransport(handler))

    assert seen["params"] == {"limit": "5", "offset": "0"}


def test_idempotent_get_is_retried_once_after_a_transport_failure() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"items": [], "total": 0})

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(service=Service.INVENTORY, method="GET", path="/products", idempotent=True)
        return await client.call(request, "cid")

    result = run(scenario, httpx.MockTransport(handler))

    assert len(calls) == 2
    assert result.attempts == 2
    assert result.status_code == 200


def test_idempotent_get_gives_up_after_the_bounded_retry() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(service=Service.INVENTORY, method="GET", path="/products", idempotent=True)
        return await client.call(request, "cid")

    with pytest.raises(UpstreamUnavailableError) as excinfo:
        run(scenario, httpx.MockTransport(handler))

    assert len(calls) == 2
    assert excinfo.value.kind == "connection_error"
    assert excinfo.value.service is Service.INVENTORY


def test_state_changing_post_is_never_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(
            service=Service.ORDERS,
            method="POST",
            path="/orders/o-1/cancel",
            json_body={"reason": "x"},
        )
        return await client.call(request, "cid")

    with pytest.raises(UpstreamUnavailableError):
        run(scenario, httpx.MockTransport(handler))

    assert len(calls) == 1


def test_timeout_surfaces_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(service=Service.ORDERS, method="GET", path="/orders/o-1", idempotent=True)
        return await client.call(request, "cid")

    with pytest.raises(UpstreamUnavailableError) as excinfo:
        run(scenario, httpx.MockTransport(handler))

    assert excinfo.value.kind == "timeout"


def test_retry_can_be_disabled() -> None:
    calls: list[str] = []
    settings = BASE_SETTINGS.model_copy(update={"get_retry_attempts": 0})

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(service=Service.INVENTORY, method="GET", path="/products", idempotent=True)
        return await client.call(request, "cid")

    with pytest.raises(UpstreamUnavailableError):
        run(scenario, httpx.MockTransport(handler), settings)

    assert len(calls) == 1


def test_idempotent_get_is_retried_once_after_a_transient_status() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, text="<html>bad gateway</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, json={"items": [], "total": 0})

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(service=Service.INVENTORY, method="GET", path="/products", idempotent=True)
        return await client.call(request, "cid")

    result = run(scenario, httpx.MockTransport(handler))

    assert len(calls) == 2
    assert result.attempts == 2
    assert result.status_code == 200


def test_transient_status_is_not_retried_for_posts() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="<html>unavailable</html>", headers={"content-type": "text/html"})

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(service=Service.ORDERS, method="POST", path="/orders", json_body={"customerId": "c"})
        return await client.call(request, "cid")

    result = run(scenario, httpx.MockTransport(handler))

    assert len(calls) == 1
    assert result.attempts == 1
    assert result.status_code == 503
    assert result.body is None


def test_non_json_success_body_is_not_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json", headers={"content-type": "text/plain"})

    async def scenario(client: UpstreamClient) -> Any:
        request = UpstreamRequest(service=Service.ORDERS, method="GET", path="/orders/o-1", idempotent=True)
        return await client.call(request, "cid")

    result = run(scenario, httpx.MockTransport(handler))

    assert result.body is None
    assert result.content == b"not json"


def test_client_must_be_started_before_use_and_closes_cleanly() -> None:
    client = UpstreamClient(BASE_SETTINGS)
    with pytest.raises(RuntimeError):
        _ = client.http

    async def main() -> bool:
        await client.start()
        http = client.http
        await client.aclose()
        return http.is_closed

    assert asyncio.run(main()) is True


@pytest.mark.parametrize(
    ("exception", "kind"),
    [
        (httpx.ConnectTimeout("t"), "timeout"),
        (httpx.ReadTimeout("t"), "timeout"),
        (httpx.ConnectError("c"), "connection_error"),
        (httpx.ReadError("r"), "transport_error"),
    ],
)
def test_transport_failures_are_classified(exception: httpx.RequestError, kind: str) -> None:
    assert classify_transport_error(exception) == kind
