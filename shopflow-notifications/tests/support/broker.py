"""Real broker helpers: publish contract envelopes and inspect the queues.

Everything here talks to the RabbitMQ broker of ``shopflow-infra`` with pika, the
same client the service uses, so the integration and failure suites exercise the
contract binding of contracts.md §8 rather than a simulation of it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from typing import Any

import pika

from app.config import (
    DEAD_LETTER_EXCHANGE_NAME,
    DEAD_LETTER_QUEUE_NAME,
    EXCHANGE_NAME,
    RETRY_EXCHANGE_NAME,
    RETRY_QUEUE_NAME,
    ROUTING_KEY_CANCELLED,
    ROUTING_KEY_CONFIRMED,
)
from app.consumer.consumer import ATTEMPT_HEADER

DEFAULT_QUEUE = "shopflow.notifications.order-events"

CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 1.0


class BrokerHarness:
    """Publish and inspect messages on the contract topology (contracts.md §8)."""

    def __init__(
        self,
        broker_url: str,
        *,
        exchange: str = EXCHANGE_NAME,
        queue: str = DEFAULT_QUEUE,
        retry_exchange: str = RETRY_EXCHANGE_NAME,
        retry_queue: str = RETRY_QUEUE_NAME,
        dead_letter_exchange: str = DEAD_LETTER_EXCHANGE_NAME,
        dead_letter_queue: str = DEAD_LETTER_QUEUE_NAME,
        routing_keys: tuple[str, ...] = (ROUTING_KEY_CONFIRMED, ROUTING_KEY_CANCELLED),
    ) -> None:
        self.broker_url = broker_url
        self.exchange = exchange
        self.queue = queue
        self.retry_exchange = retry_exchange
        self.retry_queue = retry_queue
        self.dead_letter_exchange = dead_letter_exchange
        self.dead_letter_queue = dead_letter_queue
        self.routing_keys = routing_keys

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #
    def parameters(self) -> pika.URLParameters:
        parameters = pika.URLParameters(self.broker_url)
        if not parameters.virtual_host:
            parameters.virtual_host = "/"
        parameters.connection_attempts = CONNECT_ATTEMPTS
        parameters.retry_delay = CONNECT_RETRY_DELAY
        parameters.socket_timeout = 5
        parameters.heartbeat = 30
        return parameters

    @contextmanager
    def channel(self) -> Iterator[Any]:
        """Open a short-lived channel (a connection per operation is not thread safe)."""

        connection = pika.BlockingConnection(self.parameters())
        try:
            yield connection.channel()
        finally:
            with suppress(Exception):  # pragma: no cover - closing is best effort
                connection.close()

    # ------------------------------------------------------------------ #
    # topology
    # ------------------------------------------------------------------ #
    def declare(self, channel: Any) -> None:
        """Declare exactly what the consumer declares, so the two agree."""

        channel.exchange_declare(exchange=self.exchange, exchange_type="topic", durable=True, auto_delete=False)
        channel.queue_declare(queue=self.queue, durable=True, auto_delete=False)
        for routing_key in self.routing_keys:
            channel.queue_bind(queue=self.queue, exchange=self.exchange, routing_key=routing_key)
        channel.exchange_declare(exchange=self.retry_exchange, exchange_type="topic", durable=True, auto_delete=False)
        channel.queue_declare(
            queue=self.retry_queue,
            durable=True,
            auto_delete=False,
            arguments={"x-dead-letter-exchange": self.exchange},
        )
        channel.queue_bind(queue=self.retry_queue, exchange=self.retry_exchange, routing_key="#")
        channel.exchange_declare(
            exchange=self.dead_letter_exchange,
            exchange_type="fanout",
            durable=True,
            auto_delete=False,
        )
        channel.queue_declare(queue=self.dead_letter_queue, durable=True, auto_delete=False)
        channel.queue_bind(queue=self.dead_letter_queue, exchange=self.dead_letter_exchange, routing_key="#")

    def purge(self) -> None:
        """Empty every queue this service owns, before or after a test."""

        with self.channel() as channel:
            self.declare(channel)
            for queue in (self.queue, self.retry_queue, self.dead_letter_queue):
                channel.queue_purge(queue=queue)

    def depth(self, queue: str | None = None) -> int:
        with self.channel() as channel:
            self.declare(channel)
            return int(channel.queue_declare(queue=queue or self.queue, passive=True).method.message_count)

    def depths(self) -> dict[str, int]:
        """Depths of the contract queue, the retry queue and the dead letters."""

        with self.channel() as channel:
            self.declare(channel)
            return {
                name: int(channel.queue_declare(queue=name, passive=True).method.message_count)
                for name in (self.queue, self.retry_queue, self.dead_letter_queue)
            }

    # ------------------------------------------------------------------ #
    # publishing
    # ------------------------------------------------------------------ #
    def publish_event(self, envelope: Mapping[str, Any], *, routing_key: str | None = None) -> None:
        """Publish one contracts.md §8 envelope to the contract exchange."""

        self.publish_raw(
            json.dumps(envelope).encode("utf-8"),
            routing_key=routing_key or str(envelope.get("eventType", "")),
            correlation_id=envelope.get("correlationId"),
        )

    def publish_raw(
        self,
        body: bytes,
        *,
        routing_key: str,
        headers: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        exchange: str | None = None,
    ) -> None:
        with self.channel() as channel:
            channel.basic_publish(
                exchange=exchange or self.exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json; charset=utf-8",
                    headers=dict(headers or {}),
                    correlation_id=correlation_id,
                ),
            )

    def take_message(self, queue: str | None = None) -> dict[str, Any] | None:
        """Remove and return one message (used to inspect the dead letters)."""

        with self.channel() as channel:
            self.declare(channel)
            frame = channel.basic_get(queue=queue or self.dead_letter_queue, auto_ack=False)
            if frame is None:
                return None
            method, properties, body = frame
            if method is None:
                return None
            channel.basic_ack(method.delivery_tag)
            return {
                "body": bytes(body),
                "headers": dict(properties.headers or {}),
                "routingKey": method.routing_key,
                "correlationId": properties.correlation_id,
            }

    def replay(self, message: Mapping[str, Any], *, exchange: str | None = None) -> None:
        """Replay a message that was taken from the dead letters (operator replay)."""

        self.publish_raw(
            bytes(message["body"]),
            routing_key=str(message.get("routingKey") or ROUTING_KEY_CONFIRMED),
            headers=dict(message.get("headers") or {}),
            correlation_id=message.get("correlationId"),
            exchange=exchange,
        )

    def wait_for_depth(self, queue: str, minimum: int, *, timeout: float = 30.0) -> int:
        deadline = time.monotonic() + timeout
        observed = self.depth(queue)
        while observed < minimum:
            if time.monotonic() >= deadline:
                raise AssertionError(f"{queue} stayed at {observed}, expected at least {minimum}")
            time.sleep(0.05)
            observed = self.depth(queue)
        return observed


__all__ = ["BrokerHarness", "ATTEMPT_HEADER"]
