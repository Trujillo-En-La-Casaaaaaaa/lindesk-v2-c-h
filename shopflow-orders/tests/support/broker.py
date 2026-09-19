"""Real broker plumbing for the integration suite (contracts.md §8).

The integration tests publish to - and read from - the real RabbitMQ that
shopflow-infra runs, so "the event was published" is an observation of the broker
rather than of an in-memory stand-in. The observation queue is declared with the
same exchange the service uses and bound to both routing keys of contracts.md §8;
it is exclusive and auto-deleted, so a test run leaves no topology behind.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

import pika

DEFAULT_EXCHANGE = "shopflow.orders"
DEFAULT_ROUTING_KEYS = ("order.confirmed", "order.cancelled")


def broker_reachable(broker_url: str, *, timeout_seconds: float = 5.0) -> bool:
    """Return ``True`` when the broker accepts an AMQP connection."""

    try:
        parameters = pika.URLParameters(broker_url)
        parameters.socket_timeout = max(1.0, timeout_seconds)
        parameters.connection_attempts = 1
        connection = pika.BlockingConnection(parameters)
    except Exception:  # noqa: BLE001 - pika raises several connection errors
        return False
    with contextlib.suppress(Exception):  # closing is best effort
        connection.close()
    return True


class BrokerObservation:
    """A temporary queue bound to ``shopflow.orders`` that records deliveries."""

    def __init__(
        self,
        broker_url: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        routing_keys: tuple[str, ...] = DEFAULT_ROUTING_KEYS,
        queue_name: str | None = None,
    ) -> None:
        self._broker_url = broker_url
        self._exchange = exchange
        self._routing_keys = tuple(routing_keys)
        self._queue_name = queue_name
        self.connection: Any | None = None
        self.channel: Any | None = None
        self.queue: str | None = None
        self.messages: list[dict[str, Any]] = []

    def __enter__(self) -> BrokerObservation:
        parameters = pika.URLParameters(self._broker_url)
        parameters.connection_attempts = 3
        parameters.retry_delay = 1
        parameters.socket_timeout = 10
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(
            exchange=self._exchange,
            exchange_type="topic",
            durable=True,
            auto_delete=False,
            internal=False,
        )
        result = self.channel.queue_declare(
            queue=self._queue_name or "", exclusive=True, auto_delete=True, durable=False
        )
        self.queue = str(result.method.queue)
        for routing_key in self._routing_keys:
            self.channel.queue_bind(queue=self.queue, exchange=self._exchange, routing_key=routing_key)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        for resource in (self.channel, self.connection):
            if resource is None:
                continue
            with contextlib.suppress(Exception):  # closing is best effort
                resource.close()
        self.channel = None
        self.connection = None

    # -- reading ------------------------------------------------------------
    def poll(self) -> int:
        """Collect every ready delivery; returns how many were collected."""

        assert self.channel is not None, "the observation queue is not open"
        collected = 0
        while True:
            method, properties, body = self.channel.basic_get(queue=str(self.queue), auto_ack=True)
            if method is None:
                break
            self.messages.append(_delivery(method, properties, body))
            collected += 1
        return collected

    def drain(self, *, timeout: float = 10.0, settle_seconds: float = 0.0) -> list[dict[str, Any]]:
        """Collect deliveries until none arrive for ``timeout`` seconds."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.poll():
                deadline = time.monotonic() + timeout
                continue
            _process_events(self.connection)
            time.sleep(0.05)
        if settle_seconds:
            time.sleep(settle_seconds)
            self.poll()
        return list(self.messages)

    def wait_for(self, count: int, *, timeout: float = 20.0) -> list[dict[str, Any]]:
        """Wait until at least ``count`` deliveries arrived (or fail after ``timeout``)."""

        deadline = time.monotonic() + timeout
        while len(self.messages) < count and time.monotonic() < deadline:
            self.poll()
            _process_events(self.connection)
            time.sleep(0.05)
        return list(self.messages)

    @property
    def event_ids(self) -> list[str]:
        return [str(message["headers"].get("x-shopflow-event-id") or message["messageId"]) for message in self.messages]

    def payloads_of(self, order_id: str) -> list[dict[str, Any]]:
        return [message["body"] for message in self.messages if message["body"].get("orderId") == order_id]


def _delivery(method: Any, properties: Any, body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):  # pragma: no cover - the publisher always sends JSON
        parsed = {"raw": body.decode("utf-8", "replace")}
    return {
        "routingKey": str(getattr(method, "routing_key", "")),
        "exchange": str(getattr(method, "exchange", "")),
        "messageId": str(getattr(properties, "message_id", "") or ""),
        "deliveryMode": int(getattr(properties, "delivery_mode", 0) or 0),
        "headers": dict(getattr(properties, "headers", {}) or {}),
        "body": parsed,
    }


def _process_events(connection: Any) -> None:
    if connection is None:
        return
    with contextlib.suppress(Exception):  # a closed connection is reported by the assertions instead
        connection.process_data_events(time_limit=0)


__all__ = ["BrokerObservation", "broker_reachable"]
