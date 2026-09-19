"""Request recording and fault injection for the harness upstream services."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def iso_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix (contracts.md §1)."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def upstream_error(
    status_code: int,
    code: str,
    message: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the shared error envelope of contracts.md §1."""

    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "correlationId": correlation_id,
                "details": details or {},
            }
        },
        status_code=status_code,
    )


def correlation_id_of(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() == b"x-correlation-id":
            return value.decode("latin-1")
    return ""


@dataclass(slots=True)
class RecordedRequest:
    """One request received by a harness upstream service."""

    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes = b""
    json: Any = None
    received_at: str = field(default_factory=iso_now)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def correlation_id(self) -> str | None:
        return self.header("x-correlation-id")


class RequestLog:
    """Thread-safe request log of a harness upstream service."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[RecordedRequest] = []

    def record(self, method: str, path: str, query: str, headers: dict[str, str]) -> RecordedRequest:
        request = RecordedRequest(method=method, path=path, query=query, headers=headers)
        with self._lock:
            self._records.append(request)
        return request

    def records(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def matching(self, method: str | None = None, path: str | None = None) -> list[RecordedRequest]:
        wanted_method = method.upper() if method else None
        return [
            record
            for record in self.records()
            if (wanted_method is None or record.method == wanted_method) and (path is None or record.path == path)
        ]

    def count(self, method: str | None = None, path: str | None = None) -> int:
        return len(self.matching(method, path))


@dataclass(slots=True)
class Fault:
    """A canned response that replaces the real handler for matching requests."""

    method: str | None = None
    path: str | None = None
    status: int = 502
    body: Any = None
    raw_body: str | None = None
    content_type: str = "application/json; charset=utf-8"
    delay_seconds: float = 0.0
    remaining: int | None = 1

    def matches(self, method: str, path: str) -> bool:
        if self.method is not None and self.method.upper() != method.upper():
            return False
        return self.path is None or self.path == path

    def render(self) -> Response:
        if self.raw_body is not None:
            payload = self.raw_body.encode("utf-8")
        elif self.body is None:
            payload = b""
        else:
            payload = json.dumps(self.body).encode("utf-8")
        return Response(content=payload, status_code=self.status, headers={"content-type": self.content_type})


class FaultController:
    """Schedules fault responses on a harness upstream service."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: list[Fault] = []

    def add(
        self,
        method: str | None = None,
        path: str | None = None,
        *,
        status: int = 502,
        body: Any = None,
        raw_body: str | None = None,
        content_type: str = "application/json; charset=utf-8",
        delay_seconds: float = 0.0,
        times: int | None = 1,
    ) -> Fault:
        fault = Fault(
            method=method,
            path=path,
            status=status,
            body=body,
            raw_body=raw_body,
            content_type=content_type,
            delay_seconds=delay_seconds,
            remaining=times,
        )
        with self._lock:
            self._faults.append(fault)
        return fault

    def clear(self) -> None:
        with self._lock:
            self._faults.clear()

    def match(self, method: str, path: str) -> Fault | None:
        with self._lock:
            for fault in list(self._faults):
                if not fault.matches(method, path):
                    continue
                if fault.remaining is not None:
                    fault.remaining -= 1
                    if fault.remaining <= 0:
                        self._faults.remove(fault)
                return fault
        return None

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._faults)


class InstrumentationMiddleware:
    """Records every request and injects configured faults (test harness only)."""

    def __init__(self, app: ASGIApp, *, log: RequestLog, faults: FaultController) -> None:
        self.app = app
        self.log = log
        self.faults = faults

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope["method"])
        path = str(scope["path"])
        query = bytes(scope.get("query_string", b"")).decode("latin-1")
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        record = self.log.record(method, path, query, headers)

        fault = self.faults.match(method, path)
        if fault is not None:
            if fault.delay_seconds:
                await asyncio.sleep(fault.delay_seconds)
            await fault.render()(scope, receive, send)
            return

        chunks: list[bytes] = []

        async def receive_with_body() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    body = b"".join(chunks)
                    record.body = body
                    if body:
                        try:
                            record.json = json.loads(body.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError):
                            record.json = None
            return message

        await self.app(scope, receive_with_body, send)
