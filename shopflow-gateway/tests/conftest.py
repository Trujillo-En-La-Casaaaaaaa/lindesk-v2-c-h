"""Shared fixtures: a real HTTP gateway talking to real HTTP upstream services.

Default mode: two contract-faithful harness upstreams (``shopflow-inventory``
and ``shopflow-orders``) are started in-process on loopback ports, so the
gateway is exercised end-to-end over HTTP without the sibling repositories.

Live mode: ``SHOPFLOW_GATEWAY_LIVE_UPSTREAMS=1`` plus ``ORDERS_BASE_URL`` and
``INVENTORY_BASE_URL`` makes the same suites run against the real services
(for example the docker compose stack of ``shopflow-infra``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.support.evidence import UpstreamCallLog, UpstreamEvidence, attach_upstream_log, detach_upstream_log
from tests.support.servers import RunningServer, start_server
from tests.support.upstreams.harness import FaultController, RequestLog
from tests.support.upstreams.inventory import create_inventory_app
from tests.support.upstreams.orders import create_orders_app

LIVE_MODE_ENV = "SHOPFLOW_GATEWAY_LIVE_UPSTREAMS"


@dataclass
class UpstreamStack:
    """Where the gateway should route, and how to observe the upstreams."""

    inventory_url: str
    orders_url: str
    live: bool
    inventory_log: RequestLog | None = None
    orders_log: RequestLog | None = None
    inventory_faults: FaultController | None = None
    orders_faults: FaultController | None = None
    servers: list[RunningServer] = field(default_factory=list)
    stores: dict[str, Any] = field(default_factory=dict)

    def log_for(self, service: str) -> RequestLog | None:
        return self.inventory_log if service == "inventory" else self.orders_log

    def faults_for(self, service: str) -> FaultController:
        faults = self.inventory_faults if service == "inventory" else self.orders_faults
        if faults is None:
            raise AssertionError(f"fault injection is unavailable for {service} in live-upstream mode")
        return faults

    def reset(self) -> None:
        """Reset harness state between tests (no-op against live upstreams)."""

        for store in self.stores.values():
            store.reset()
        for log in (self.inventory_log, self.orders_log):
            if log is not None:
                log.clear()
        for faults in (self.inventory_faults, self.orders_faults):
            if faults is not None:
                faults.clear()


def _require_reachable(url: str, service: str) -> None:
    try:
        response = httpx.get(f"{url.rstrip('/')}/health", timeout=5.0)
    except httpx.HTTPError as exc:
        raise AssertionError(
            f"{service} is not reachable at {url}: {exc!r}. Start the stack (for example"
            " `docker compose -f ../shopflow-infra/docker-compose.yml up -d ...`) or unset"
            f" {LIVE_MODE_ENV} to use the harness upstreams."
        ) from exc
    assert response.status_code == 200, f"{service} /health at {url} returned {response.status_code}"


@pytest.fixture
def upstream_stack(tmp_path: Path) -> Iterator[UpstreamStack]:
    """Start (or locate) the two owning services the gateway routes to."""

    if os.environ.get(LIVE_MODE_ENV) == "1":
        orders_url = os.environ.get("ORDERS_BASE_URL", "")
        inventory_url = os.environ.get("INVENTORY_BASE_URL", "")
        assert orders_url and inventory_url, (
            f"{LIVE_MODE_ENV}=1 requires ORDERS_BASE_URL and INVENTORY_BASE_URL to be set"
        )
        _require_reachable(orders_url, "shopflow-orders")
        _require_reachable(inventory_url, "shopflow-inventory")
        yield UpstreamStack(inventory_url=inventory_url, orders_url=orders_url, live=True)
        return

    inventory_log = RequestLog()
    inventory_faults = FaultController()
    orders_log = RequestLog()
    orders_faults = FaultController()

    inventory_db = str(tmp_path / "inventory.db")
    orders_db = str(tmp_path / "orders.db")
    inventory_app = create_inventory_app(inventory_db, log=inventory_log, faults=inventory_faults)
    inventory_server = start_server(inventory_app)
    orders_app = create_orders_app(
        orders_db,
        inventory_base_url=inventory_server.base_url,
        log=orders_log,
        faults=orders_faults,
    )
    orders_server = start_server(orders_app)

    stack = UpstreamStack(
        inventory_url=inventory_server.base_url,
        orders_url=orders_server.base_url,
        live=False,
        inventory_log=inventory_log,
        orders_log=orders_log,
        inventory_faults=inventory_faults,
        orders_faults=orders_faults,
        servers=[inventory_server, orders_server],
        stores={"inventory": inventory_app.state.store, "orders": orders_app.state.store},
    )
    try:
        yield stack
    finally:
        for server in reversed(stack.servers):
            server.stop()
        for store in stack.stores.values():
            store.close()


@pytest.fixture
def gateway_log() -> Iterator[UpstreamCallLog]:
    """Collect the gateway's structured upstream-call records for one test."""

    handler = attach_upstream_log()
    try:
        yield handler
    finally:
        detach_upstream_log(handler)


@pytest.fixture
def evidence(gateway_log: UpstreamCallLog, upstream_stack: UpstreamStack) -> UpstreamEvidence:
    return UpstreamEvidence(gateway_log, upstream_stack)


@pytest.fixture
def gateway_factory(upstream_stack: UpstreamStack) -> Iterator[Any]:
    """Start additional gateway instances (for example with a short timeout)."""

    started: list[RunningServer] = []

    def _start(**overrides: Any) -> RunningServer:
        values: dict[str, Any] = {
            "orders_base_url": upstream_stack.orders_url,
            "inventory_base_url": upstream_stack.inventory_url,
            "request_timeout_seconds": 2.0,
            "get_retry_attempts": 1,
        }
        values.update(overrides)
        settings = Settings(**values)
        server = start_server(create_app(settings=settings))
        started.append(server)
        return server

    try:
        yield _start
    finally:
        for server in reversed(started):
            server.stop()


@pytest.fixture
def gateway(gateway_factory: Any) -> RunningServer:
    """The gateway under test, configured against the upstream stack."""

    return gateway_factory()


@pytest.fixture
def client(gateway: RunningServer) -> Iterator[httpx.Client]:
    """HTTP client speaking to the public gateway surface."""

    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as http:
        yield http


@pytest.fixture
def direct_inventory(upstream_stack: UpstreamStack) -> Iterator[httpx.Client]:
    """HTTP client speaking directly to ``shopflow-inventory``."""

    with httpx.Client(base_url=upstream_stack.inventory_url, timeout=15.0) as http:
        yield http


@pytest.fixture
def direct_orders(upstream_stack: UpstreamStack) -> Iterator[httpx.Client]:
    """HTTP client speaking directly to ``shopflow-orders``."""

    with httpx.Client(base_url=upstream_stack.orders_url, timeout=15.0) as http:
        yield http


@pytest.fixture
def stopped_upstream_port() -> int:
    """A loopback port with nothing listening: a stopped backend."""

    from tests.support.servers import free_port

    return free_port()


def pytest_report_header(config: pytest.Config) -> str:
    mode = "live upstream services" if os.environ.get(LIVE_MODE_ENV) == "1" else "in-process harness upstreams"
    return f"shopflow-gateway tests: {mode}"
