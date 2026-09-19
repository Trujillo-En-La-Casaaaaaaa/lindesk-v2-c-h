"""A real HTTP double of the notification provider (contracts.md §7).

The provider is an *external system* in this architecture: the unit suite uses a
port double to assert the decision logic, and this module gives the integration and
failure suites a real HTTP server that speaks the §7 contract verbatim so that

* the payload the adapter really sends can be asserted (the emulator only exposes
  ``messageId``, ``orderId``, ``template``, ``to`` and ``receivedAt``, so the
  emulator alone cannot prove that the cancellation body carries the reason);
* the failure modes the emulator cannot produce on demand - ``5xx`` and ``4xx`` -
  are exercised deterministically.

It implements the same idempotency rule as the emulator: a repeated
``Idempotency-Key`` returns the original ``messageId`` and records no second
message, a repeated key with a different body answers ``409 idempotency_conflict``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODE_OK = "ok"
MODE_UNAVAILABLE = "unavailable"
MODE_REJECTED = "rejected"

IDEMPOTENCY_HEADER = "Idempotency-Key"
CORRELATION_HEADER = "X-Correlation-Id"


def deterministic_message_id(template: str, order_id: str) -> str:
    """contracts.md §7: ``"msg-" + sha256(template + ":" + orderId).hexdigest()[:16]``."""

    digest = hashlib.sha256(f"{template}:{order_id}".encode()).hexdigest()
    return f"msg-{digest[:16]}"


class CaptureProvider:
    """A local, thread-safe HTTP provider implementing contracts.md §7."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._messages: list[dict[str, Any]] = []
        self._by_key: dict[str, dict[str, Any]] = {}
        self._requests = 0
        self.mode = MODE_OK
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.capture = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> CaptureProvider:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def __enter__(self) -> CaptureProvider:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    def reset(self, *, mode: str = MODE_OK) -> None:
        with self._lock:
            self._messages.clear()
            self._by_key.clear()
            self._requests = 0
            self.mode = mode

    @property
    def messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._messages)

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._requests

    def count(self, template: str | None = None, order_id: str | None = None) -> int:
        with self._lock:
            return len(
                [
                    message
                    for message in self._messages
                    if (template is None or message["body"].get("template") == template)
                    and (order_id is None or message["body"].get("orderId") == order_id)
                ]
            )

    def payloads(self, template: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(message["body"].get("payload") or {})
                for message in self._messages
                if template is None or message["body"].get("template") == template
            ]

    # ------------------------------------------------------------------ #
    # request handling (called by the handler thread)
    # ------------------------------------------------------------------ #
    def record(self, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            self._requests += 1
            if self.mode == MODE_UNAVAILABLE:
                return 503, {"error": {"code": "internal_error", "message": "provider is unavailable"}}
            if self.mode == MODE_REJECTED:
                return 400, {"error": {"code": "validation_error", "message": "provider rejected the message"}}

            key = (headers.get(IDEMPOTENCY_HEADER) or "").strip()
            if not key:
                return 400, {"error": {"code": "validation_error", "message": "Idempotency-Key is required"}}

            record = {
                "body": dict(body),
                "idempotencyKey": key,
                "correlationId": headers.get(CORRELATION_HEADER),
                "receivedAt": _now(),
            }
            digest = _fingerprint(body)
            existing = self._by_key.get(key)
            if existing is not None:
                if existing["fingerprint"] != digest:
                    return 409, {
                        "error": {
                            "code": "idempotency_conflict",
                            "message": f"Idempotency-Key '{key}' was already used with a different body",
                        }
                    }
                return 200, dict(existing["response"])

            message_id = deterministic_message_id(str(body.get("template")), str(body.get("orderId")))
            response = {"messageId": message_id, "status": "accepted", "acceptedAt": _now()}
            self._by_key[key] = {"fingerprint": digest, "response": response}
            record["messageId"] = message_id
            self._messages.append(record)
            return 200, response

    def listing(self, order_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            items = [
                {
                    "messageId": message["messageId"],
                    "orderId": message["body"].get("orderId"),
                    "template": message["body"].get("template"),
                    "to": message["body"].get("to"),
                    "receivedAt": message["receivedAt"],
                }
                for message in self._messages
                if order_id is None or message["body"].get("orderId") == order_id
            ]
        return {"items": items, "total": len(items)}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "shopflow-capture-provider/1.0"

    @property
    def capture(self) -> CaptureProvider:
        return self.server.capture  # type: ignore[attr-defined]

    def log_message(self, *_args: Any) -> None:  # keep the test output clean
        return None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.startswith("/ready") or self.path.startswith("/health"):
            self._respond(200, {"status": "ok"})
            return
        if self.path.startswith("/messages"):
            query = self.path.partition("?")[2]
            order_id = None
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "orderId":
                    order_id = value
            self._respond(200, self.capture.listing(order_id))
            return
        self._respond(404, {"error": {"code": "not_found", "message": "unknown route"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self.path.startswith("/messages"):
            self._respond(404, {"error": {"code": "not_found", "message": "unknown route"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._respond(400, {"error": {"code": "validation_error", "message": "body is not JSON"}})
            return
        status, payload = self.capture.record(body, {key: value for key, value in self.headers.items()})
        self._respond(status, payload)

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _fingerprint(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "MODE_OK",
    "MODE_REJECTED",
    "MODE_UNAVAILABLE",
    "CaptureProvider",
    "deterministic_message_id",
]
