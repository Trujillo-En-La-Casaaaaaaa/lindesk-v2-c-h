"""Evidence helpers: what the gateway called, and what the upstreams received.

The gateway runs inside the test process, so its own structured upstream-call
log is available even when the suite runs against the real services
(``SHOPFLOW_GATEWAY_LIVE_UPSTREAMS=1``). The harness upstreams additionally
record every request they received, which corroborates the gateway log.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.upstream.client import UPSTREAM_REQUEST_LOGGER

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tests.conftest import UpstreamStack


@dataclass(frozen=True, slots=True)
class UpstreamCallEvent:
    """One structured record of the gateway upstream log."""

    event: str
    service: str
    method: str
    path: str
    status: int | None = None
    attempts: int | None = None
    correlation_id: str | None = None


class UpstreamCallLog(logging.Handler):
    """Collects the gateway's structured upstream-call records."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.events: list[UpstreamCallEvent] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - logging.Handler API
        upstream = getattr(record, "upstream", None)
        if not isinstance(upstream, dict):
            return
        self.events.append(
            UpstreamCallEvent(
                event=str(getattr(record, "event", "")),
                service=str(upstream.get("service", "")),
                method=str(upstream.get("method", "")),
                path=str(upstream.get("path", "")),
                status=upstream.get("status"),
                attempts=upstream.get("attempts"),
                correlation_id=getattr(record, "correlationId", None),
            )
        )

    def clear(self) -> None:
        self.events.clear()

    def matching(
        self,
        service: str | None = None,
        method: str | None = None,
        path: str | None = None,
        *,
        event: str | None = None,
    ) -> list[UpstreamCallEvent]:
        wanted_method = method.upper() if method else None
        return [
            item
            for item in self.events
            if (service is None or item.service == service)
            and (wanted_method is None or item.method.upper() == wanted_method)
            and (path is None or item.path == path)
            and (event is None or item.event == event)
        ]


class UpstreamEvidence:
    """Combined view of the gateway log and the harness upstream request logs."""

    def __init__(self, gateway_log: UpstreamCallLog, stack: UpstreamStack) -> None:
        self.gateway_log = gateway_log
        self.stack = stack

    def gateway_calls(self, service: str | None = None, method: str | None = None, path: str | None = None) -> int:
        return len(self.gateway_log.matching(service, method, path, event="upstream_call"))

    def upstream_calls(self, service: str, method: str | None = None, path: str | None = None) -> int:
        log = self.stack.log_for(service)
        if log is None:
            raise AssertionError(f"no request log available for {service}: the suite runs against live upstreams")
        return log.count(method, path)

    def assert_upstream_requests(self, service: str, method: str, path: str, expected: int) -> None:
        """Assert how many requests the upstream received, when its log is available.

        The request log is a harness affordance: against the real services this
        check is a no-op and the caller's state-based assertions carry the proof.
        """

        log = self.stack.log_for(service)
        if log is None:
            return
        received = log.count(method, path)
        assert received == expected, f"expected {expected} requests at {service} {method} {path}, got {received}"

    def request_log(self, service: str) -> list[object]:
        log = self.stack.log_for(service)
        if log is None:
            raise AssertionError(f"no request log available for {service}: the suite runs against live upstreams")
        return list(log.records())

    def assert_no_upstream_call(
        self,
        service: str,
        *,
        method: str,
        path: str,
        state_check: Callable[[], None] | None = None,
        hint: str = "",
    ) -> None:
        """Assert the gateway never called ``service`` for this method and path.

        The gateway log is authoritative in every mode. When the suite runs with
        the harness upstreams the upstream request log must agree, and an
        optional state check proves the backend state did not change either.
        """

        assert self.gateway_calls(service, method, path) == 0, (
            f"gateway called {service} {method} {path} although it must not ({hint})"
        )
        if self.stack.live:
            assert state_check is not None, (
                f"live-upstream mode needs a state check to prove no {service} write happened ({hint})"
            )
        else:
            assert self.upstream_calls(service, method, path) == 0, (
                f"{service} received {method} {path} although the gateway must not call it ({hint})"
            )
        if state_check is not None:
            state_check()

    def assert_attempts(self, service: str, method: str, path: str, expected: int) -> None:
        """Assert how many upstream attempts the gateway made for one route."""

        calls = self.gateway_log.matching(service, method, path, event="upstream_call")
        assert calls, f"gateway recorded no upstream call for {service} {method} {path}"
        assert all(call.attempts == expected for call in calls), (
            f"expected {expected} attempts for {service} {method} {path}, got {[call.attempts for call in calls]}"
        )
        if not self.stack.live:
            assert self.upstream_calls(service, method, path) == expected, (
                f"expected {expected} requests at {service} {method} {path},"
                f" got {self.upstream_calls(service, method, path)}"
            )


def attach_upstream_log() -> UpstreamCallLog:
    """Attach a fresh upstream-call log to the gateway logger."""

    handler = UpstreamCallLog()
    logging.getLogger(UPSTREAM_REQUEST_LOGGER).addHandler(handler)
    return handler


def detach_upstream_log(handler: UpstreamCallLog) -> None:
    """Detach a previously attached upstream-call log."""

    logging.getLogger(UPSTREAM_REQUEST_LOGGER).removeHandler(handler)
