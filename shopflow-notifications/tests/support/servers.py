"""Run the real ASGI application on a real socket inside the test process.

The integration suite exercises the service over HTTP (not only through an
in-process client) so routing, the error envelope and the response headers are
tested on the same code path production uses.
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


def closed_port() -> int:
    """Return a loopback port that was just closed, i.e. one that refuses connections.

    Used to prove the provider-outage path with a genuine connection error instead
    of a simulated one.
    """

    return free_port()


class RunningServer:
    """A uvicorn server running in a background thread."""

    def __init__(self, host: str, port: int, server: uvicorn.Server, thread: threading.Thread) -> None:
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
        if not self._thread.is_alive():
            return
        self._server.should_exit = True
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._server.force_exit = True
            self._thread.join(timeout)
        if self._thread.is_alive():  # pragma: no cover - only on a stuck server
            raise ServerStartupError(f"test server on port {self.port} did not stop")

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=20.0)

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
    lifespan: str = "off",
) -> RunningServer:
    """Start ``app`` under uvicorn and return a handle once it is serving.

    ``lifespan="on"`` runs the application's startup/shutdown hooks, which is what
    starts the order-event consumer of this service.
    """

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False, lifespan=lifespan)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="shopflow-notifications-test-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + startup_timeout
    while not server.started:
        if not thread.is_alive():  # pragma: no cover - only on a broken app
            raise ServerStartupError("uvicorn server thread exited before startup completed")
        if time.monotonic() > deadline:  # pragma: no cover - only on a stuck server
            raise ServerStartupError(f"uvicorn server did not start within {startup_timeout}s")
        time.sleep(0.02)

    bound_port = int(server.servers[0].sockets[0].getsockname()[1])
    return RunningServer(host, bound_port, server, thread)


__all__ = [
    "RunningServer",
    "ServerStartupError",
    "closed_port",
    "free_port",
    "start_server",
]
