"""Correlation ids: accept, generate, forward, echo (contracts.md §1)."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse

from app.middleware.correlation import (
    CORRELATION_ID_HEADER,
    CorrelationIdMiddleware,
    extract_correlation_id,
    get_correlation_id,
)


def request(app: object, method: str, url: str, **kwargs: object) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


def build_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    async def echo() -> JSONResponse:
        return JSONResponse({"seen": get_correlation_id(), "bound": get_correlation_id()})

    return app


def test_missing_header_generates_a_uuid() -> None:
    response = request(build_app(), "GET", "/echo")

    correlation_id = response.headers[CORRELATION_ID_HEADER]
    assert uuid.UUID(correlation_id).version == 4
    assert response.json()["seen"] == correlation_id


def test_supplied_header_is_echoed_unchanged() -> None:
    response = request(build_app(), "GET", "/echo", headers={CORRELATION_ID_HEADER: "client-correlation-1"})

    assert response.headers[CORRELATION_ID_HEADER] == "client-correlation-1"
    assert response.json()["seen"] == "client-correlation-1"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_header_is_replaced_by_a_uuid(value: str) -> None:
    response = request(build_app(), "GET", "/echo", headers={CORRELATION_ID_HEADER: value})

    correlation_id = response.headers[CORRELATION_ID_HEADER]
    assert correlation_id.strip()
    assert uuid.UUID(correlation_id).version == 4


def test_context_var_is_cleared_after_the_request() -> None:
    request(build_app(), "GET", "/echo")

    assert get_correlation_id() is None


def test_unhandled_failure_returns_the_envelope_with_the_correlation_id() -> None:
    async def boom(scope: object, receive: object, send: object) -> None:
        raise RuntimeError("kaboom with internal host gateway-db:5432")

    response = request(CorrelationIdMiddleware(boom), "GET", "/api/products", headers={CORRELATION_ID_HEADER: "cid-9"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["correlationId"] == "cid-9"
    assert response.headers[CORRELATION_ID_HEADER] == "cid-9"
    assert "kaboom" not in response.text
    assert "gateway-db" not in response.text


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_extract_correlation_id_never_returns_blank(raw: str | None) -> None:
    value = extract_correlation_id(raw)

    assert value.strip()
    if raw is None or not raw.strip():
        assert uuid.UUID(value).version == 4
