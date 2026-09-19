#!/usr/bin/env python
"""Health gate for the ShopFlow stack (HANDOFF #7).

Polls ``/health`` on every ShopFlow service (and on the provider emulator) until
each one answers ``200``, then exits ``0``. Exits ``1`` when the deadline passes
and names the endpoints that never became healthy, so the gate can never report
a stack as ready that is not.

Used in two ways:

* on the operator's host, against the published ports::

      python scripts/wait_for_health.py --timeout 180

* as the ``healthcheck-gate`` one-shot compose service, against the service
  names on the compose network::

      python wait_for_health.py --urls http://shopflow-gateway:8000/health,...

``shopflow-web`` is deliberately not polled: contracts.md defines ``/health`` for
the five backend services and the emulator only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shopflow_http as http  # noqa: E402  (path bootstrap above)

DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_INTERVAL_SECONDS = 2.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3.0

#: Environment variable used by the ``healthcheck-gate`` compose service.
URLS_ENV_VAR = "SHOPFLOW_HEALTH_URLS"


@dataclass(frozen=True)
class Target:
    """One endpoint to watch."""

    name: str
    url: str


def default_targets() -> tuple[Target, ...]:
    """The documented host-side endpoints, honouring the published port overrides."""

    inventory_port = os.environ.get("INVENTORY_PORT", "8002")
    orders_port = os.environ.get("ORDERS_PORT", "8001")
    notifications_port = os.environ.get("NOTIFICATIONS_PORT", "8003")
    provider_port = os.environ.get("PROVIDER_PORT", "8081")
    gateway_port = os.environ.get("GATEWAY_PORT", "8080")

    # Dependency order: catalogue before orders, orders before the gateway.
    return (
        Target("shopflow-inventory", f"http://localhost:{inventory_port}/health"),
        Target("shopflow-orders", f"http://localhost:{orders_port}/health"),
        Target("shopflow-notifications", f"http://localhost:{notifications_port}/health"),
        Target("notification-provider-emulator", f"http://localhost:{provider_port}/health"),
        Target("shopflow-gateway", f"http://localhost:{gateway_port}/health"),
    )


def parse_target(spec: str) -> Target:
    """Parse ``name=url`` or a bare ``http://...`` URL into a :class:`Target`."""

    candidate = spec.strip()
    if not candidate:
        raise ValueError("empty endpoint specification")
    if candidate.startswith(("http://", "https://")):
        parsed = urlparse(candidate)
        return Target(name=f"{parsed.netloc}{parsed.path}", url=candidate)
    name, separator, url = candidate.partition("=")
    if not separator or not name.strip() or not url.strip():
        raise ValueError(f"expected 'name=url' or a full URL, got '{spec}'")
    return Target(name=name.strip(), url=url.strip())


def parse_spec_list(spec: str) -> list[Target]:
    return [parse_target(item) for item in spec.split(",") if item.strip()]


def build_targets(args: argparse.Namespace) -> list[Target]:
    targets: list[Target] = []
    explicit = list(args.url or [])
    if args.urls:
        explicit.append(args.urls)
    if explicit:
        for spec in explicit:
            targets.extend(parse_spec_list(spec))
    else:
        targets.extend(default_targets())
    deduplicated: dict[str, Target] = {}
    for target in targets:
        deduplicated[target.url] = target
    return list(deduplicated.values())


def probe(target: Target, request_timeout: float) -> tuple[bool, str]:
    """Return ``(healthy, detail)`` for one endpoint."""

    try:
        response = http.get(target.url, timeout=request_timeout)
    except http.TransportError as exc:
        return False, str(exc)
    return response.ok, f"HTTP {response.status_code} {target.url}"


def wait(
    targets: Sequence[Target],
    *,
    timeout: float,
    interval: float,
    request_timeout: float,
    quiet: bool = False,
    sleep=time.sleep,
    clock=time.monotonic,
) -> int:
    """Poll ``targets`` until all are healthy or ``timeout`` elapses."""

    if not targets:
        print("health gate: no endpoints to watch", file=sys.stderr)
        return 2

    pending: dict[str, Target] = {target.name: target for target in targets}
    last_failure: dict[str, str] = {}
    started = clock()

    print(
        f"health gate: {len(targets)} endpoint(s), timeout {timeout:g}s, "
        f"interval {interval:g}s, transport {http.backend()}",
        flush=True,
    )

    while True:
        for name in list(pending):
            healthy, detail = probe(pending[name], request_timeout)
            if healthy:
                if not quiet:
                    print(f"  [{clock() - started:6.1f}s] ok   {name}  {detail}", flush=True)
                del pending[name]
                last_failure.pop(name, None)
            else:
                last_failure[name] = detail
            # keep the advertised budget honest even when several endpoints are
            # unreachable and every probe burns its own request timeout
            if pending and clock() - started >= timeout:
                break

        if not pending:
            print(f"health gate: READY - {len(targets)}/{len(targets)} endpoints healthy after {clock() - started:.1f}s")
            return 0

        remaining = timeout - (clock() - started)
        if remaining <= 0:
            print(
                f"health gate: NOT READY after {timeout:g}s - {len(pending)} endpoint(s) unhealthy",
                file=sys.stderr,
            )
            for name in pending:
                print(f"  FAIL {name}  {last_failure.get(name, 'no response')}", file=sys.stderr)
            return 1

        if not quiet:
            print(f"  [{clock() - started:6.1f}s] waiting for: {', '.join(pending)}", flush=True)
        sleep(min(interval, remaining))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll /health on every ShopFlow service until the stack is ready.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="total budget in seconds before the gate fails")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS,
                        help="seconds between polling rounds")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                        help="per-request timeout in seconds")
    parser.add_argument("--urls", default=os.environ.get(URLS_ENV_VAR, ""),
                        help=f"comma-separated [name=]url list; replaces the defaults (env: {URLS_ENV_VAR})")
    parser.add_argument("--url", action="append", default=[],
                        help="additional [name=]url endpoint; may be repeated")
    parser.add_argument("--quiet", action="store_true", help="only print the final verdict")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        targets = build_targets(args)
    except ValueError as exc:
        print(f"health gate: invalid endpoint specification: {exc}", file=sys.stderr)
        return 2
    return wait(
        targets,
        timeout=args.timeout,
        interval=args.interval,
        request_timeout=args.request_timeout,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    raise SystemExit(main())
