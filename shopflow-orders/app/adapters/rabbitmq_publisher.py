"""RabbitMQ adapter for the broker port (contracts.md §8).

Publishes the outbox rows to the durable topic exchange ``shopflow.orders`` with
routing key ``order.confirmed`` / ``order.cancelled``, as **persistent** messages,
with **publisher confirms** enabled: ``basic_publish`` only returns once the broker
confirmed the message, and a nack, a connection failure or a closed channel is
reported as :class:`~app.ports.outbox.BrokerUnavailable`. The publisher relay then
leaves the outbox row unpublished, so the event is retried - never lost.

``message_id`` is the stable ``eventId`` of the envelope and the same value is sent
in the ``x-shopflow-event-id`` header, so a redelivered event is recognisable as
the same event by the consumer (contracts.md §8: consumers deduplicate by
``eventId``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any

import pika

from app.config import BROKER_EXCHANGE
from app.ports.outbox import BrokerUnavailable, OutboxMessage

LOGGER = logging.getLogger("shopflow.orders.broker")

JSON_CONTENT_TYPE = "application/json; charset=utf-8"

EVENT_ID_HEADER = "x-shopflow-event-id"
EVENT_TYPE_HEADER = "x-shopflow-event-type"
EVENT_VERSION_HEADER = "x-shopflow-event-version"
CORRELATION_ID_HEADER = "x-correlation-id"


class RabbitMqPublisher:
    """Implements :class:`app.ports.outbox.MessageBroker` against RabbitMQ."""

    def __init__(
        self,
        broker_url: str,
        *,
        exchange: str = BROKER_EXCHANGE,
        connect_timeout_seconds: float = 5.0,
        connection_factory: Callable[[pika.URLParameters], Any] | None = None,
    ) -> None:
        self._broker_url = broker_url
        self._exchange = exchange
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._connection_factory = connection_factory or pika.BlockingConnection
        self._connection: Any | None = None
        self._channel: Any | None = None

    # -- port surface -------------------------------------------------------
    def publish(self, message: OutboxMessage) -> None:
        """Publish one message and return only once the broker confirmed it."""

        channel = self._confirmed_channel()
        try:
            channel.basic_publish(
                exchange=self._exchange,
                routing_key=message.routing_key,
                body=json.dumps(message.payload).encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type=JSON_CONTENT_TYPE,
                    delivery_mode=2,  # persistent
                    message_id=message.event_id,
                    timestamp=int(message.occurred_at.astimezone(UTC).timestamp()),
                    type=message.event_type,
                    headers={
                        EVENT_ID_HEADER: message.event_id,
                        EVENT_TYPE_HEADER: message.event_type,
                        EVENT_VERSION_HEADER: _event_version(message),
                        **(
                            {CORRELATION_ID_HEADER: str(message.payload.get("correlationId"))}
                            if message.payload.get("correlationId")
                            else {}
                        ),
                    },
                ),
                mandatory=False,
            )
        except (pika.exceptions.AMQPError, OSError) as exc:
            self.close()
            raise BrokerUnavailable(
                "The broker did not confirm the publish.",
                details={"eventId": message.event_id, "eventType": message.event_type, "error": str(exc)},
            ) from exc

    def is_reachable(self) -> bool:
        """``True`` when the broker accepts an AMQP connection (used by ``/ready``)."""

        try:
            self._confirmed_channel()
        except BrokerUnavailable:
            return False
        return True

    def close(self) -> None:
        """Close the channel and the connection (best effort)."""

        for resource in (self._channel, self._connection):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                LOGGER.debug("closing the broker connection failed", exc_info=True)
        self._channel = None
        self._connection = None

    # -- connection ---------------------------------------------------------
    def _confirmed_channel(self) -> Any:
        if self._channel is not None and not getattr(self._channel, "is_closed", False):
            return self._channel
        self.close()
        parameters = pika.URLParameters(self._broker_url)
        if not parameters.virtual_host:
            # ``amqp://host:5672`` addresses the default vhost "/"; be explicit so
            # an empty path can never be read as "".
            parameters.virtual_host = "/"
        parameters.connection_attempts = 1
        parameters.retry_delay = 1
        parameters.socket_timeout = max(1.0, self._connect_timeout_seconds)
        parameters.blocked_connection_timeout = 30
        parameters.heartbeat = 30
        try:
            connection = self._connection_factory(parameters)
            channel = connection.channel()
            channel.confirm_delivery()  # publisher confirms (contracts.md §8)
            channel.exchange_declare(
                exchange=self._exchange,
                exchange_type="topic",
                durable=True,
                auto_delete=False,
                internal=False,
            )
        except Exception as exc:  # noqa: BLE001 - pika raises several AMQP error types
            self.close()
            raise BrokerUnavailable(
                "The broker is not reachable.",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        self._connection = connection
        self._channel = channel
        return channel


def _event_version(message: OutboxMessage) -> int:
    try:
        return int(message.payload.get("eventVersion", 1))
    except (TypeError, ValueError):  # pragma: no cover - the envelope always carries an int
        return 1


__all__ = ["EVENT_ID_HEADER", "EVENT_TYPE_HEADER", "EVENT_VERSION_HEADER", "RabbitMqPublisher"]
