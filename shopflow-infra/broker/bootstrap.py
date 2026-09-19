#!/usr/bin/env python
"""Declare and verify the durable ShopFlow broker topology (contracts.md §8).

RabbitMQ topology for the ecosystem:

* exchange ``shopflow.orders`` - topic, durable
* queue ``shopflow.notifications.order-events`` - durable
* bindings ``order.confirmed`` and ``order.cancelled``

``broker/definitions.json`` also declares this topology declaratively at broker
boot. This job exists because that file is only read if the broker's config file
is picked up in time, which is not guaranteed on every Docker host; running an
explicit, idempotent, exit-on-failure bootstrap after the broker reports healthy
makes the topology deterministic and gives the deployment a hard gate: the
services do not start until the queue and its bindings exist.

Publisher confirms are a *publisher-side* setting (shopflow-orders enables them
on its channel); RabbitMQ supports them natively, so there is nothing to
configure here.

The job performs no business work: it only declares broker topology.
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import pika

EXCHANGE_NAME = "shopflow.orders"
EXCHANGE_TYPE = "topic"
QUEUE_NAME = "shopflow.notifications.order-events"
ROUTING_KEYS = ("order.confirmed", "order.cancelled")

#: contracts.md §11 local default (credential-free, throwaway).
DEFAULT_BROKER_URL = "amqp://shopflow:shopflow@message-broker:5672/"
DEFAULT_TIMEOUT_SECONDS = 60.0
RETRY_INTERVAL_SECONDS = 2.0


def broker_url() -> str:
    return os.environ.get("BROKER_URL", "").strip() or DEFAULT_BROKER_URL


def timeout_seconds() -> float:
    raw = os.environ.get("BROKER_INIT_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        print(f"broker-bootstrap: ignoring invalid BROKER_INIT_TIMEOUT_SECONDS={raw!r}", file=sys.stderr)
        return DEFAULT_TIMEOUT_SECONDS


def redact(url: str) -> str:
    """Return the broker url without credentials, for log output."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def connect(url: str, timeout: float) -> pika.BlockingConnection:
    """Open a blocking AMQP connection, retrying until ``timeout`` elapses."""

    parameters = pika.URLParameters(url)
    # ``amqp://user:pass@host:5672/`` addresses vhost "/" (the default vhost that
    # the broker creates); be explicit so an empty path can never mean "".
    if not parameters.virtual_host:
        parameters.virtual_host = "/"
    parameters.connection_attempts = 1
    parameters.socket_timeout = 5
    parameters.blocked_connection_timeout = 10

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            return pika.BlockingConnection(parameters)
        except Exception as exc:  # pika.AMQPConnectionError and friends
            last_error = exc
            if time.monotonic() >= deadline:
                raise BrokerUnavailable(f"{redact(url)} unreachable after {timeout:g}s: {last_error}") from exc
            time.sleep(RETRY_INTERVAL_SECONDS)


class BrokerUnavailable(RuntimeError):
    """The broker could not be reached within the configured budget."""


def declare_topology(channel: "pika.adapters.blocking_connection.BlockingChannel") -> None:
    """Declare the exchange, the queue and both bindings (idempotent)."""

    channel.exchange_declare(
        exchange=EXCHANGE_NAME,
        exchange_type=EXCHANGE_TYPE,
        durable=True,
        auto_delete=False,
        internal=False,
    )
    channel.queue_declare(queue=QUEUE_NAME, durable=True, auto_delete=False, exclusive=False)
    for routing_key in ROUTING_KEYS:
        channel.queue_bind(queue=QUEUE_NAME, exchange=EXCHANGE_NAME, routing_key=routing_key)


def verify_topology(channel: "pika.adapters.blocking_connection.BlockingChannel") -> int:
    """Assert that the declared objects exist with the expected parameters."""

    channel.exchange_declare(
        exchange=EXCHANGE_NAME,
        exchange_type=EXCHANGE_TYPE,
        durable=True,
        passive=True,
    )
    declared = channel.queue_declare(queue=QUEUE_NAME, durable=True, passive=True)
    return int(declared.method.message_count)


def main() -> int:
    url = broker_url()
    timeout = timeout_seconds()
    print(f"broker-bootstrap: target {redact(url)} (timeout {timeout:g}s)", flush=True)

    try:
        connection = connect(url, timeout)
    except BrokerUnavailable as exc:
        print(f"broker-bootstrap: FAILED - {exc}", file=sys.stderr)
        return 1

    try:
        channel = connection.channel()
        declare_topology(channel)
        print(f"broker-bootstrap: declared exchange '{EXCHANGE_NAME}' (type={EXCHANGE_TYPE}, durable=true)")
        print(f"broker-bootstrap: declared queue    '{QUEUE_NAME}' (durable=true)")
        for routing_key in ROUTING_KEYS:
            print(f"broker-bootstrap: bound            {EXCHANGE_NAME} -> {QUEUE_NAME} with routing key '{routing_key}'")

        waiting = verify_topology(channel)
        print(f"broker-bootstrap: verified topology (queue reachable, {waiting} message(s) waiting)")
    except Exception as exc:  # pika.exceptions.ChannelClosedByBroker, AMQPError, ...
        print(f"broker-bootstrap: FAILED - could not ensure the topology: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover - closing best effort
            pass

    print("broker-bootstrap: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
