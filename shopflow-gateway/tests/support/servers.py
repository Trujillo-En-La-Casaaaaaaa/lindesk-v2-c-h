"""Run real ASGI applications on real sockets inside the test process.

The gateway suites exercise the gateway over HTTP, not through an in-process
test client: the gateway, ``shopflow-orders`` and ``shopflow-inventory`` are
started as three independent uvicorn servers so routing, headers, timeouts and
failure modes are exercised over a real network path.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx
import uvicorn

DEFAULT_STARTUP_TIMEOUT = 30.0


class ServerStartupError(RuntimeError):
    """Raised when a test server does not become reachable."""


def free_port() -> int:
    """Reserve a free TCP port on the loopback interface."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunningServer:
    """A uvicorn server running in a background thread."""

    def __init__(self, app: Any, host: str, port: int, server: uvicorn.Server, thread: threading.Thread) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._server = server
        self._thread = thread

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and self._server.started

    def stop(self, timeout: float = 15.0) -> None:
        """Stop the server and wait for its thread to finish."""

        if not self._thread.is_alive():
            return
        self._server.should_exit = True
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._server.force_exit = True
            self._thread.join(timeout)
        if self._thread.is_alive():
            raise ServerStartupError(f"test server on port {self.port} did not stop")

    def __enter__(self) -> RunningServer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def start_server(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> RunningServer:
    """Start ``app`` under uvicorn and return a handle once it is serving."""

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="shopflow-test-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + startup_timeout
    while not server.started:
        if not thread.is_alive():
            raise ServerStartupError("uvicorn server thread exited before startup completed")
        if time.monotonic() > deadline:
            raise ServerStartupError(f"uvicorn server did not start within {startup_timeout}s")
        time.sleep(0.02)

    bound_port = int(server.servers[0].sockets[0].getsockname()[1])
    return RunningServer(app, host, bound_port, server, thread)


def wait_for_http(url: str, *, timeout: float = 20.0) -> httpx.Response:
    """Poll ``url`` until it answers, then return the response."""

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
        except httpx.HTTPError as exc:  # pragma: no cover - only on a stuck server
            last_error = exc
            time.sleep(0.05)
            continue
        return response
    raise ServerStartupError(f"{url} did not become reachable: {last_error!r}")
