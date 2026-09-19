"""Broker consumer of order events (component: order-event-consumer, handoff #1).

contracts.md §8 fixes the binding: durable queue
``shopflow.notifications.order-events`` on the durable topic exchange
``shopflow.orders`` for routing keys ``order.confirmed`` and ``order.cancelled``.

Guarantees implemented here:

* **manual acknowledgement** - a delivery is acknowledged only after the
  notification record (or its terminal ``FAILED`` record) is committed;
* **prefetch** of 1-10 (``CONSUMER_PREFETCH``) so one consumer never buffers an
  unbounded number of unacknowledged deliveries;
* **bounded retry** - a retryable failure republishes the message to a retry queue
  carrying an incremented ``x-shopflow-attempt`` header and a per-message TTL.
  When the TTL expires the broker dead-letters the message back to
  ``shopflow.orders`` (the routing key is preserved, verified against the broker),
  which is a *delayed requeue*: the attempt counter lives in the durable message,
  so it survives a consumer restart, and the delay means a hot loop is impossible;
* **dead-lettering** - once the budget is exhausted (or the failure/event is
  permanent) the message is published with its failure metadata to
  ``shopflow.notifications.order-events.dead`` and only then acknowledged. A
  message is never silently dropped: if the dead-letter publish does not get
  confirmed, the original delivery is nacked with requeue instead of acked.

The topology this consumer owns is declared idempotently: the contract exchange,
queue and bindings (declared exactly as ``shopflow-infra`` declares them, so the
declarations agree) plus the retry and dead-letter objects this service owns.

.. rubric:: Fault injection

``SHOPFLOW_FAULT_CRASH_AFTER_PROVIDER_SEND`` / ``SHOPFLOW_FAULT_CRASH_AFTER_COMMIT``
make the process exit hard inside the two crash windows named by the handoff, so
the failure suite can prove recovery from a real crash instead of guessing at
timing. Both are inert unless explicitly enabled.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel

from app.config import Settings, load_settings
from app.consumer.router import EventRouter
from app.domain.notification import (
    SUPPORTED_EVENT_TYPES,
    DomainError,
    OrderEvent,
    format_timestamp,
    parse_order_event,
    utc_now,
)
from app.domain.retry import RetryPolicy
from app.ports.provider import ProviderRejected

LOGGER = logging.getLogger("shopflow.notifications.consumer")

# --- message headers (the durable retry/failure state of one delivery) ---------
ATTEMPT_HEADER = "x-shopflow-attempt"
EVENT_ID_HEADER = "x-shopflow-event-id"
EVENT_TYPE_HEADER = "x-shopflow-event-type"
NOTIFICATION_TYPE_HEADER = "x-shopflow-notification-type"
FAILURE_REASON_HEADER = "x-shopflow-failure-reason"
FAILED_AT_HEADER = "x-shopflow-failed-at"
ORIGINAL_ROUTING_KEY_HEADER = "x-shopflow-original-routing-key"
FAILURE_RECORDED_HEADER = "x-shopflow-failure-recorded"

JSON_CONTENT_TYPE = "application/json; charset=utf-8"

# --- what to do with one delivery ---------------------------------------------
ACTION_ACK = "ack"
ACTION_RETRY = "retry"
ACTION_DEAD_LETTER = "dead_letter"

# --- fault injection (test-only, inert by default) ----------------------------
CRASH_AFTER_PROVIDER_SEND_ENV = "SHOPFLOW_FAULT_CRASH_AFTER_PROVIDER_SEND"
CRASH_AFTER_COMMIT_ENV = "SHOPFLOW_FAULT_CRASH_AFTER_COMMIT"
CRASH_EXIT_CODE = 9
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class FaultHooks:
    """Crash-window seams used by the failure suite (disabled in production)."""

    after_send: Callable[[], None] | None = None
    after_commit: Callable[[], None] | None = None

    @property
    def enabled(self) -> bool:
        return self.after_send is not None or self.after_commit is not None


def _enabled(source: Mapping[str, str], name: str) -> bool:
    return str(source.get(name, "")).strip().lower() in _ENABLED_VALUES


def _hard_exit() -> None:  # pragma: no cover - only reached under fault injection
    os._exit(CRASH_EXIT_CODE)


def fault_hooks_from_env(env: Mapping[str, str] | None = None) -> FaultHooks:
    """Build the fault hooks described by the environment (empty by default)."""

    source: Mapping[str, str] = os.environ if env is None else env
    after_send = _hard_exit if _enabled(source, CRASH_AFTER_PROVIDER_SEND_ENV) else None
    after_commit = _hard_exit if _enabled(source, CRASH_AFTER_COMMIT_ENV) else None
    return FaultHooks(after_send=after_send, after_commit=after_commit)


@dataclass(frozen=True)
class Disposition:
    """What the consumer should do with one delivery (production of ``decide``)."""

    action: str
    attempt: int
    reason: str = ""
    delay_seconds: float = 0.0
    event_id: str | None = None
    notification_type: str | None = None
    failure_recorded: bool = False

    @property
    def delay_milliseconds(self) -> int:
        return int(round(max(0.0, self.delay_seconds) * 1000))


def broker_is_reachable(
    broker_url: str,
    *,
    timeout_seconds: float = 5.0,
    connection_factory: Callable[[pika.URLParameters], Any] | None = None,
) -> bool:
    """Return ``True`` when the broker accepts an AMQP connection (used by ``/ready``)."""

    factory = connection_factory or pika.BlockingConnection
    parameters = _connection_parameters(broker_url, timeout_seconds=timeout_seconds, attempts=1)
    try:
        connection = factory(parameters)
    except Exception:  # pika.AMQPConnectionError and friends
        return False
    with suppress(Exception):  # pragma: no cover - closing is best effort
        connection.close()
    return True


def _connection_parameters(
    broker_url: str,
    *,
    timeout_seconds: float,
    attempts: int = 3,
) -> pika.URLParameters:
    parameters = pika.URLParameters(broker_url)
    # ``amqp://user:pass@host:5672/`` addresses the default vhost "/"; be explicit
    # so an empty path can never be read as "".
    if not parameters.virtual_host:
        parameters.virtual_host = "/"
    parameters.connection_attempts = max(1, attempts)
    parameters.retry_delay = 1
    parameters.socket_timeout = max(1.0, float(timeout_seconds))
    parameters.blocked_connection_timeout = 30
    parameters.heartbeat = 30
    return parameters


class OrderEventConsumer:
    """Consumes order events, delivers notifications, retries and dead-letters."""

    def __init__(
        self,
        *,
        settings: Settings,
        router: EventRouter,
        hooks: FaultHooks | None = None,
        stop_event: threading.Event | None = None,
        connection_factory: Callable[[pika.URLParameters], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        unsupported = [key for key in settings.routing_keys if key not in SUPPORTED_EVENT_TYPES]
        if unsupported:
            raise ValueError(
                f"BROKER_ROUTING_KEYS may only contain the consumed event types "
                f"({', '.join(SUPPORTED_EVENT_TYPES)}); got {', '.join(unsupported)}"
            )
        self._settings = settings
        self._router = router
        self._hooks = hooks or FaultHooks()
        self._stop = stop_event or threading.Event()
        self._connection_factory = connection_factory or pika.BlockingConnection
        self._sleep = sleep
        self._timer: Any | None = None
        self._last_error: str | None = None
        self._retry_policy = RetryPolicy(
            max_attempts=settings.consumer_max_delivery_attempts,
            base_delay_seconds=settings.consumer_retry_backoff_seconds,
            max_delay_seconds=settings.consumer_retry_backoff_max_seconds,
        )

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def hooks(self) -> FaultHooks:
        return self._hooks

    # ------------------------------------------------------------------ #
    # topology (handoff #1)
    # ------------------------------------------------------------------ #
    def declare_topology(self, channel: BlockingChannel) -> None:
        """Declare the consumed topology and the retry / dead-letter objects."""

        settings = self._settings
        channel.exchange_declare(
            exchange=settings.broker_exchange,
            exchange_type="topic",
            durable=True,
            auto_delete=False,
            internal=False,
        )
        # Declared exactly like shopflow-infra (durable, no extra arguments), so
        # the two declarations are equivalent and neither side fails with 406.
        channel.queue_declare(queue=settings.broker_queue, durable=True, auto_delete=False, exclusive=False)
        for routing_key in settings.routing_keys:
            channel.queue_bind(
                queue=settings.broker_queue,
                exchange=settings.broker_exchange,
                routing_key=routing_key,
            )

        # Retry: its own topic exchange, and a queue that dead-letters back to the
        # contract exchange after the per-message TTL. The dead-lettered message
        # keeps its routing key, so it lands on the contract queue again.
        channel.exchange_declare(
            exchange=settings.broker_retry_exchange,
            exchange_type="topic",
            durable=True,
            auto_delete=False,
        )
        channel.queue_declare(
            queue=settings.broker_retry_queue,
            durable=True,
            auto_delete=False,
            exclusive=False,
            arguments={"x-dead-letter-exchange": settings.broker_exchange},
        )
        channel.queue_bind(
            queue=settings.broker_retry_queue,
            exchange=settings.broker_retry_exchange,
            routing_key="#",
        )

        # Dead-letter: a service-owned fanout holding the messages that exhausted
        # their budget, keeping their payload and failure metadata for an operator.
        channel.exchange_declare(
            exchange=settings.broker_dead_letter_exchange,
            exchange_type="fanout",
            durable=True,
            auto_delete=False,
        )
        channel.queue_declare(
            queue=settings.broker_dead_letter_queue,
            durable=True,
            auto_delete=False,
            exclusive=False,
        )
        channel.queue_bind(
            queue=settings.broker_dead_letter_queue,
            exchange=settings.broker_dead_letter_exchange,
            routing_key="#",
        )

    # ------------------------------------------------------------------ #
    # one delivery -> one decision (no broker interaction, fully unit testable)
    # ------------------------------------------------------------------ #
    def decide(self, body: bytes, routing_key: str, attempt: int = 0) -> Disposition:
        """Interpret and handle one delivery, and return what to do with it.

        Performs the notification work (dedup, provider delivery, commit, terminal
        ``FAILED`` record) and nothing else: the caller performs the acknowledgement
        or the republish this decision asks for.
        """

        attempts = self._attempts_of(attempt)
        event, rejection = self._interpret(body, routing_key, attempts)
        if rejection is not None:
            return rejection
        assert event is not None  # _interpret returns either an event or a rejection

        try:
            outcome = self._router.handle(event, attempts=attempts)
        except ProviderRejected as exc:
            # A 4xx the provider would repeat: permanent, dead-letter immediately.
            return self._terminate(event, attempts, f"provider_rejected: {exc}")
        except Exception as exc:  # noqa: BLE001 - everything else may be transient
            reason = f"{type(exc).__name__}: {exc}"
            if self._retry_policy.should_retry(attempts):
                delay = self._retry_policy.delay_for(attempts)
                LOGGER.warning(
                    "delivery failed, retrying in %.3fs (orderId=%s eventId=%s attempt=%s/%s reason=%s)",
                    delay,
                    event.order_id,
                    event.event_id,
                    attempts,
                    self._retry_policy.max_attempts,
                    reason,
                )
                return Disposition(
                    action=ACTION_RETRY,
                    attempt=attempts,
                    reason=reason,
                    delay_seconds=delay,
                    event_id=event.event_id,
                    notification_type=event.notification_type.value,
                )
            return self._terminate(event, attempts, reason)

        if outcome.skipped:
            LOGGER.info("delivery skipped (orderId=%s eventId=%s reason=%s)", event.order_id, event.event_id,
                        outcome.reason)
        return Disposition(
            action=ACTION_ACK,
            attempt=attempts,
            reason=outcome.reason,
            event_id=event.event_id,
            notification_type=event.notification_type.value,
        )

    def _interpret(self, body: bytes, routing_key: str, attempts: int) -> tuple[OrderEvent | None, Disposition | None]:
        """Validate one delivery; return the event, or the disposition that rejects it."""

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return None, self._rejected(attempts, f"malformed_event_body: {exc}")
        if not isinstance(payload, Mapping):
            return None, self._rejected(attempts, "malformed_event_body: not a JSON object")

        try:
            event = parse_order_event(payload)
        except DomainError as exc:
            # Unsupported event type/version and invalid envelopes are permanent:
            # retrying cannot make them valid, so they go straight to the dead
            # letters with their body intact.
            return None, self._rejected(attempts, f"{exc.code}: {exc}")
        except Exception as exc:  # noqa: BLE001 - never loop on an unexpected shape
            return None, self._rejected(attempts, f"unparseable_event: {type(exc).__name__}: {exc}")

        if event.event_type != routing_key:
            return None, self._rejected(
                attempts,
                f"routing_key_mismatch: routing key '{routing_key}' carries '{event.event_type}'",
                event_id=event.event_id,
            )
        return event, None

    def _rejected(self, attempts: int, reason: str, *, event_id: str | None = None) -> Disposition:
        LOGGER.error("event rejected and dead-lettered (reason=%s)", reason)
        return Disposition(action=ACTION_DEAD_LETTER, attempt=attempts, reason=reason, event_id=event_id)

    def _terminate(self, event: OrderEvent, attempts: int, reason: str) -> Disposition:
        """Record the terminal FAILED notification and ask for the dead letters."""

        recorded = False
        try:
            self._router.record_failure(event, reason=reason, attempts=attempts)
            recorded = True
        except Exception as exc:  # noqa: BLE001 - the message must still be dead-lettered
            LOGGER.critical(
                "could not record the FAILED notification for order %s (eventId=%s): %s; "
                "the message is dead-lettered regardless",
                event.order_id,
                event.event_id,
                exc,
            )
        return Disposition(
            action=ACTION_DEAD_LETTER,
            attempt=attempts,
            reason=reason,
            event_id=event.event_id,
            notification_type=event.notification_type.value,
            failure_recorded=recorded,
        )

    def _attempts_of(self, attempt: int) -> int:
        try:
            value = int(attempt)
        except (TypeError, ValueError):
            value = 0
        return max(0, value) + 1

    # ------------------------------------------------------------------ #
    # broker interaction
    # ------------------------------------------------------------------ #
    def on_message(self, channel: BlockingChannel, method: Any, properties: Any, body: bytes) -> None:
        """AMQP callback of one delivery: decide, then acknowledge or republish."""

        routing_key = str(getattr(method, "routing_key", "") or "")
        attempt = _attempt_header(properties)
        try:
            disposition = self.decide(bytes(body), routing_key, attempt)
        except Exception as exc:  # noqa: BLE001 - decide() is not allowed to kill the loop
            LOGGER.exception("unexpected failure while handling a delivery: %s", exc)
            disposition = Disposition(
                action=ACTION_RETRY,
                attempt=self._attempts_of(attempt),
                reason=f"unexpected_consumer_error: {type(exc).__name__}: {exc}",
            )

        if disposition.action == ACTION_RETRY:
            if self.publish_retry(channel, body, routing_key, properties, disposition):
                channel.basic_ack(method.delivery_tag)
            else:
                # The retry could not be confirmed: keep the original delivery
                # instead of acknowledging a message that is nowhere else.
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

        if disposition.action == ACTION_DEAD_LETTER:
            if self.publish_dead_letter(channel, body, routing_key, properties, disposition):
                channel.basic_ack(method.delivery_tag)
            else:
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

        # ACTION_ACK: the record is committed; only now is the delivery acknowledged.
        if self._hooks.after_commit is not None:
            LOGGER.warning("fault injection: exiting after the commit, before the acknowledgement")
            self._hooks.after_commit()
        channel.basic_ack(method.delivery_tag)

    def publish_retry(
        self,
        channel: BlockingChannel,
        body: bytes,
        routing_key: str,
        properties: Any,
        disposition: Disposition,
    ) -> bool:
        """Republish a failed delivery for a delayed retry; return whether it is confirmed."""

        headers = _headers_of(properties)
        headers[ATTEMPT_HEADER] = disposition.attempt
        headers[ORIGINAL_ROUTING_KEY_HEADER] = routing_key
        if disposition.event_id:
            headers[EVENT_ID_HEADER] = disposition.event_id
        headers[EVENT_TYPE_HEADER] = routing_key
        if disposition.notification_type:
            headers[NOTIFICATION_TYPE_HEADER] = disposition.notification_type
        try:
            channel.basic_publish(
                exchange=self._settings.broker_retry_exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type=JSON_CONTENT_TYPE,
                    correlation_id=getattr(properties, "correlation_id", None),
                    headers=headers,
                    expiration=str(disposition.delay_milliseconds),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - AMQP errors and confirmation failures
            self._last_error = f"{type(exc).__name__}: {exc}"
            LOGGER.error("could not republish the message for a retry: %s", exc)
            return False
        return True

    def publish_dead_letter(
        self,
        channel: BlockingChannel,
        body: bytes,
        routing_key: str,
        properties: Any,
        disposition: Disposition,
    ) -> bool:
        """Publish a message that exhausted (or cannot use) its retry budget."""

        headers = _headers_of(properties)
        headers[FAILURE_REASON_HEADER] = disposition.reason
        headers[FAILED_AT_HEADER] = format_timestamp(utc_now())
        headers[ATTEMPT_HEADER] = disposition.attempt
        headers[ORIGINAL_ROUTING_KEY_HEADER] = routing_key
        headers[EVENT_TYPE_HEADER] = routing_key
        headers[FAILURE_RECORDED_HEADER] = "true" if disposition.failure_recorded else "false"
        if disposition.event_id:
            headers[EVENT_ID_HEADER] = disposition.event_id
        if disposition.notification_type:
            headers[NOTIFICATION_TYPE_HEADER] = disposition.notification_type
        try:
            channel.basic_publish(
                exchange=self._settings.broker_dead_letter_exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type=JSON_CONTENT_TYPE,
                    correlation_id=getattr(properties, "correlation_id", None),
                    headers=headers,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - AMQP errors and confirmation failures
            self._last_error = f"{type(exc).__name__}: {exc}"
            LOGGER.error("could not publish the message to the dead-letter queue: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------ #
    # run loop
    # ------------------------------------------------------------------ #
    def _open_connection(self) -> Any:
        return self._connection_factory(
            _connection_parameters(self._settings.broker_url, timeout_seconds=self._settings.request_timeout_seconds)
        )

    def _schedule_stop_check(self, connection: Any, channel: BlockingChannel) -> None:
        interval = 0.25

        def check() -> None:
            if self._stop.is_set():
                LOGGER.info("stop requested: leaving the consume loop")
                channel.stop_consuming()
                return
            self._timer = connection.call_later(interval, check)

        self._timer = connection.call_later(interval, check)

    def serve_once(self) -> None:
        """Connect, declare the topology and consume until stopped or disconnected."""

        connection = self._open_connection()
        try:
            channel = connection.channel()
            self.declare_topology(channel)
            channel.basic_qos(prefetch_count=self._settings.consumer_prefetch)
            channel.confirm_delivery()
            channel.basic_consume(
                queue=self._settings.broker_queue,
                on_message_callback=self.on_message,
                auto_ack=False,
            )
            self._schedule_stop_check(connection, channel)
            LOGGER.info(
                "consuming queue=%s exchange=%s routing_keys=%s prefetch=%s max_attempts=%s",
                self._settings.broker_queue,
                self._settings.broker_exchange,
                ",".join(self._settings.routing_keys),
                self._settings.consumer_prefetch,
                self._retry_policy.max_attempts,
            )
            channel.start_consuming()
        finally:
            with suppress(Exception):  # pragma: no cover - closing is best effort
                connection.close()

    def run_forever(self) -> None:
        """Consume until :meth:`stop` is called, reconnecting after broker outages."""

        while not self._stop.is_set():
            try:
                self.serve_once()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001 - a broker outage must not kill the service
                self._last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("broker connection lost: %s", self._last_error)
            if self._stop.is_set():
                break
            self._stop.wait(self._settings.consumer_reconnect_interval_seconds)
        LOGGER.info("consumer stopped")

    def run_in_thread(self, *, name: str = "shopflow-notifications-consumer") -> threading.Thread:
        """Start the consumer in a background thread and return it."""

        thread = threading.Thread(target=self.run_forever, name=name, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Ask the consumer to stop after the delivery in flight."""

        self._stop.set()

    def reset(self) -> None:
        """Clear a previous stop request so the same instance can be restarted."""

        self._stop.clear()


class ConsumerRunner:
    """Supervises the consumer thread inside the API process."""

    def __init__(self, consumer: OrderEventConsumer, *, name: str = "shopflow-notifications-consumer") -> None:
        self._consumer = consumer
        self._name = name
        self._thread: threading.Thread | None = None

    @property
    def consumer(self) -> OrderEventConsumer:
        return self._consumer

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> str | None:
        return self._consumer.last_error

    def start(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._consumer.reset()
        self._thread = self._consumer.run_in_thread(name=self._name)
        return self._thread

    def stop(self, timeout: float = 20.0) -> None:
        self._consumer.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def status(self) -> str:
        if self._thread is None:
            return "stopped"
        return "running" if self._thread.is_alive() else "failed"


def _attempt_header(properties: Any) -> int:
    """Read the durable attempt counter of a delivery (default: first attempt)."""

    headers = _headers_of(properties)
    try:
        return max(0, int(headers.get(ATTEMPT_HEADER, 0)))
    except (TypeError, ValueError):
        return 0


def _headers_of(properties: Any) -> dict[str, Any]:
    headers = getattr(properties, "headers", None)
    return dict(headers) if isinstance(headers, Mapping) else {}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the consumer as its own process (``python -m app.consumer.consumer``)."""

    del argv
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Imported here so that ``app.consumer.consumer`` stays importable without the
    # HTTP layer and no module-level import cycle exists.
    from app.main import create_app

    settings = load_settings()
    app = create_app(settings=settings, start_consumer=False)
    consumer: OrderEventConsumer | None = app.state.order_event_consumer
    if consumer is None:
        raise SystemExit("the order-event consumer is not configured (check CONSUMER_ENABLED)")

    def _handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s: shutting down", signum)
        consumer.stop()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            with suppress(ValueError, OSError):  # pragma: no cover - not on every platform
                signal.signal(signum, _handle_signal)

    consumer.run_forever()
    return 0


__all__ = [
    "ACTION_ACK",
    "ACTION_DEAD_LETTER",
    "ACTION_RETRY",
    "ATTEMPT_HEADER",
    "ConsumerRunner",
    "Disposition",
    "FaultHooks",
    "OrderEventConsumer",
    "broker_is_reachable",
    "fault_hooks_from_env",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
