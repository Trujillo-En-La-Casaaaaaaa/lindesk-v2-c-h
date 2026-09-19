"""Unit tests of the consumer's decisions, topology and acknowledgement order.

``OrderEventConsumer.decide`` performs the notification work and returns what to do
with the delivery, so the whole retry/dead-letter policy is testable without a
broker. ``on_message``/``serve_once`` are then driven with channel and connection
doubles to prove the acknowledgement disciplines of handoff #1:

* an acknowledgement happens only after the notification record is committed;
* a retryable failure republishes the message with an incremented attempt counter
  and only then acknowledges the original delivery;
* an exhausted (or permanent) failure dead-letters the message before acking, and
  falls back to ``nack(requeue=True)`` when the publish cannot be confirmed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.consumer.consumer import (
    ACTION_ACK,
    ACTION_DEAD_LETTER,
    ACTION_RETRY,
    ATTEMPT_HEADER,
    CRASH_AFTER_COMMIT_ENV,
    CRASH_AFTER_PROVIDER_SEND_ENV,
    EVENT_ID_HEADER,
    FAILURE_REASON_HEADER,
    FaultHooks,
    OrderEventConsumer,
    broker_is_reachable,
    fault_hooks_from_env,
)
from app.consumer.router import EventRouter
from app.handlers.orchestrator import NotificationOrchestrator
from app.ports.provider import ProviderRejected
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id
from tests.support.fakes import (
    FakeChannel,
    FakeConnection,
    FakeMethod,
    FakeNotificationRepository,
    FakeProperties,
    RecordingProvider,
)
from tests.support.runtimes import wait_until
from tests.support.settings import make_settings

ORDER = order_id("consumer")


class OrderedRepository(FakeNotificationRepository):
    """Records how many acknowledgements existed when the commit happened."""

    def __init__(self, channel: FakeChannel) -> None:
        super().__init__()
        self._channel = channel
        self.acks_at_commit: list[int] | None = None

    def commit_notification(self, record: Any, *, event_id: str, processed_at: Any) -> Any:
        self.acks_at_commit = list(self._channel.acks)
        return super().commit_notification(record, event_id=event_id, processed_at=processed_at)


class Builder:
    """Builds a consumer whose broker and ports are doubles."""

    def __init__(self, **settings_overrides: Any) -> None:
        self.channel = FakeChannel()
        self.connection = FakeConnection(self.channel)
        self.repository = FakeNotificationRepository()
        self.provider = RecordingProvider()
        self.settings = make_settings(**settings_overrides)

    def with_channel(self, channel: FakeChannel) -> Builder:
        self.channel = channel
        self.connection = FakeConnection(channel)
        return self

    def with_repository(self, repository: FakeNotificationRepository) -> Builder:
        self.repository = repository
        return self

    def with_provider(self, provider: RecordingProvider) -> Builder:
        self.provider = provider
        return self

    def build(self, *, hooks: FaultHooks | None = None) -> OrderEventConsumer:
        orchestrator = NotificationOrchestrator(
            repository=self.repository,
            provider=self.provider,
            after_send=(hooks.after_send if hooks else None),
        )
        router = EventRouter.from_orchestrator(orchestrator)
        return OrderEventConsumer(
            settings=self.settings,
            router=router,
            hooks=hooks,
            connection_factory=lambda _parameters: self.connection,
        )


def body_of(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope).encode("utf-8")


def confirmed(seed: str = "consumer-c", order: str = ORDER) -> dict[str, Any]:
    return order_confirmed(order=order, event=event_id(seed))


def delivery(routing_key: str, *, attempt: int | None = None, delivery_tag: int = 7) -> tuple[Any, Any]:
    headers = {} if attempt is None else {ATTEMPT_HEADER: attempt}
    return FakeMethod(routing_key=routing_key, delivery_tag=delivery_tag), FakeProperties(
        headers=headers,
        correlation_id="corr-smoke",
    )


# --------------------------------------------------------------------------- #
# decide(): the notification work and the retry policy
# --------------------------------------------------------------------------- #
def test_a_valid_event_is_handled_and_acknowledged() -> None:
    builder = Builder()
    consumer = builder.build()

    disposition = consumer.decide(body_of(confirmed()), "order.confirmed")

    assert disposition.action == ACTION_ACK
    assert disposition.attempt == 1
    assert builder.provider.count() == 1
    assert builder.repository.record_for(ORDER, "confirmation") is not None


def test_a_retryable_failure_asks_for_a_retry_with_the_configured_backoff() -> None:
    builder = Builder(consumer_max_delivery_attempts=3, consumer_retry_backoff_seconds=0.5)
    builder.with_provider(RecordingProvider(failures=99))
    consumer = builder.build()

    disposition = consumer.decide(body_of(confirmed()), "order.confirmed", attempt=0)

    assert disposition.action == ACTION_RETRY
    assert disposition.attempt == 1
    assert disposition.delay_seconds == 0.5
    assert "ProviderUnavailable" in disposition.reason
    assert disposition.notification_type == "confirmation"
    assert builder.repository.records == []


def test_the_retry_budget_is_bounded_and_the_last_attempt_records_failed() -> None:
    builder = Builder(consumer_max_delivery_attempts=3, consumer_retry_backoff_seconds=0.5)
    builder.with_provider(RecordingProvider(failures=99))
    consumer = builder.build()

    # the message already carries two attempts, so this delivery is the third
    disposition = consumer.decide(body_of(confirmed()), "order.confirmed", attempt=2)

    assert disposition.action == ACTION_DEAD_LETTER
    assert disposition.attempt == 3
    assert disposition.failure_recorded is True
    record = builder.repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.status.value == "FAILED"
    assert record.attempts == 3
    assert record.provider_message_id is None


def test_a_permanent_rejection_is_dead_lettered_without_burning_the_budget() -> None:
    builder = Builder(consumer_max_delivery_attempts=5)
    builder.with_provider(
        RecordingProvider(failures=99, error=ProviderRejected("provider rejected the message", status_code=400))
    )
    consumer = builder.build()

    disposition = consumer.decide(body_of(confirmed()), "order.confirmed", attempt=0)

    assert disposition.action == ACTION_DEAD_LETTER
    assert disposition.attempt == 1
    assert "provider_rejected" in disposition.reason
    record = builder.repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.attempts == 1


def test_a_redelivered_event_is_handled_as_an_acknowledged_skip() -> None:
    builder = Builder()
    consumer = builder.build()

    first = consumer.decide(body_of(confirmed()), "order.confirmed")
    second = consumer.decide(body_of(confirmed()), "order.confirmed")

    assert first.action == ACTION_ACK
    assert second.action == ACTION_ACK
    assert builder.provider.count() == 1


@pytest.mark.parametrize(
    ("payload", "routing_key", "expected_reason"),
    [
        (b"{not json", "order.confirmed", "malformed_event_body"),
        (b"[]", "order.confirmed", "malformed_event_body"),
        (body_of({"eventType": "order.shipped"}), "order.shipped", "validation_error"),
        (body_of(confirmed()), "order.cancelled", "routing_key_mismatch"),
    ],
)
def test_unusable_deliveries_go_straight_to_the_dead_letters(
    payload: bytes,
    routing_key: str,
    expected_reason: str,
) -> None:
    builder = Builder()
    consumer = builder.build()

    disposition = consumer.decide(payload, routing_key)

    assert disposition.action == ACTION_DEAD_LETTER
    assert expected_reason in disposition.reason
    assert builder.provider.count() == 0
    assert builder.repository.records == []


def test_an_invalid_cancellation_is_dead_lettered_permanently() -> None:
    builder = Builder()
    consumer = builder.build()
    payload = order_cancelled(order=ORDER, event=event_id("consumer-bad"))
    payload["reason"] = "   "

    disposition = consumer.decide(body_of(payload), "order.cancelled")

    assert disposition.action == ACTION_DEAD_LETTER
    assert "reason is required" in disposition.reason


def test_the_attempt_header_advances_the_recorded_attempt_count() -> None:
    builder = Builder()
    consumer = builder.build()

    consumer.decide(body_of(confirmed()), "order.confirmed", attempt=1)

    record = builder.repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.attempts == 2


# --------------------------------------------------------------------------- #
# on_message(): acknowledgement disciplines
# --------------------------------------------------------------------------- #
def test_the_delivery_is_acknowledged_only_after_the_record_is_committed() -> None:
    channel = FakeChannel()
    builder = Builder().with_channel(channel).with_repository(OrderedRepository(channel))
    consumer = builder.build()
    method, properties = delivery("order.confirmed")

    consumer.on_message(channel, method, properties, body_of(confirmed()))

    assert isinstance(builder.repository, OrderedRepository)
    assert builder.repository.acks_at_commit == []  # nothing acknowledged at commit time
    assert channel.acks == [7]
    assert channel.published == []
    assert channel.nacks == []


def test_a_retryable_failure_republishes_with_the_next_attempt_and_then_acknowledges() -> None:
    channel = FakeChannel()
    builder = Builder(consumer_max_delivery_attempts=3, consumer_retry_backoff_seconds=0.25).with_channel(channel)
    builder.with_provider(RecordingProvider(failures=99))
    consumer = builder.build()
    method, properties = delivery("order.confirmed", attempt=0)

    consumer.on_message(channel, method, properties, body_of(confirmed()))

    assert channel.acks == [7]
    assert len(channel.published) == 1
    publish = channel.published[0]
    assert publish["exchange"] == builder.settings.broker_retry_exchange
    assert publish["routing_key"] == "order.confirmed"
    assert publish["body"] == body_of(confirmed())
    assert publish["properties"].headers[ATTEMPT_HEADER] == 1
    assert publish["properties"].expiration == "250"
    assert publish["properties"].delivery_mode == 2


def test_the_retry_republish_keeps_the_delivery_when_it_cannot_be_confirmed() -> None:
    channel = FakeChannel(publish_error=RuntimeError("publisher confirm failed"))
    builder = Builder().with_channel(channel)
    builder.with_provider(RecordingProvider(failures=99))
    consumer = builder.build()
    method, properties = delivery("order.confirmed")

    consumer.on_message(channel, method, properties, body_of(confirmed()))

    assert channel.acks == []
    assert channel.nacks == [(7, True)]


def test_an_exhausted_delivery_is_dead_lettered_and_then_acknowledged() -> None:
    channel = FakeChannel()
    builder = Builder(consumer_max_delivery_attempts=2).with_channel(channel)
    builder.with_provider(RecordingProvider(failures=99))
    consumer = builder.build()
    method, properties = delivery("order.confirmed", attempt=1)

    consumer.on_message(channel, method, properties, body_of(confirmed()))

    assert channel.acks == [7]
    assert len(channel.published) == 1
    publish = channel.published[0]
    assert publish["exchange"] == builder.settings.broker_dead_letter_exchange
    assert publish["properties"].headers[FAILURE_REASON_HEADER]
    assert publish["properties"].headers[ATTEMPT_HEADER] == 2
    assert publish["properties"].headers[EVENT_ID_HEADER] == event_id("consumer-c")
    assert publish["properties"].headers["x-shopflow-failure-recorded"] == "true"


def test_the_dead_letter_republish_keeps_the_delivery_when_it_cannot_be_confirmed() -> None:
    channel = FakeChannel(publish_error=RuntimeError("publisher confirm failed"))
    builder = Builder(consumer_max_delivery_attempts=1).with_channel(channel)
    builder.with_provider(RecordingProvider(failures=99))
    consumer = builder.build()
    method, properties = delivery("order.confirmed")

    consumer.on_message(channel, method, properties, body_of(confirmed()))

    assert channel.acks == []
    assert channel.nacks == [(7, True)]


def test_a_permanent_rejection_is_dead_lettered_on_the_first_delivery() -> None:
    channel = FakeChannel()
    builder = Builder(consumer_max_delivery_attempts=5).with_channel(channel)
    builder.with_provider(RecordingProvider(failures=99, error=ProviderRejected("bad request", status_code=400)))
    consumer = builder.build()
    method, properties = delivery("order.confirmed")

    consumer.on_message(channel, method, properties, body_of(confirmed()))

    assert channel.acks == [7]
    assert len(channel.published) == 1
    assert channel.published[0]["exchange"] == builder.settings.broker_dead_letter_exchange


def test_the_after_commit_seam_runs_before_the_acknowledgement() -> None:
    channel = FakeChannel()
    observed: list[int] = []
    hooks = FaultHooks(after_commit=lambda: observed.append(len(channel.acks)))
    builder = Builder().with_channel(channel).with_repository(OrderedRepository(channel))
    consumer = builder.build(hooks=hooks)
    method, properties = delivery("order.confirmed")

    consumer.on_message(channel, method, properties, body_of(confirmed()))

    assert observed == [0]  # the seam ran while nothing was acknowledged yet
    assert channel.acks == [7]


# --------------------------------------------------------------------------- #
# topology and consumption wiring
# --------------------------------------------------------------------------- #
def test_the_declared_topology_is_the_contract_binding_plus_retry_and_dead_letters() -> None:
    builder = Builder()
    consumer = builder.build()

    consumer.declare_topology(builder.channel)

    exchanges = {declaration["exchange"]: declaration for declaration in builder.channel.exchanges}
    assert exchanges[builder.settings.broker_exchange]["exchange_type"] == "topic"
    assert exchanges[builder.settings.broker_exchange]["durable"] is True
    assert exchanges[builder.settings.broker_retry_exchange]["durable"] is True
    assert exchanges[builder.settings.broker_dead_letter_exchange]["exchange_type"] == "fanout"
    assert exchanges[builder.settings.broker_dead_letter_exchange]["durable"] is True

    queues = {declaration["queue"]: declaration for declaration in builder.channel.queues}
    contract_queue = queues[builder.settings.broker_queue]
    assert contract_queue["durable"] is True
    # The contract queue is declared exactly as shopflow-infra declares it.
    assert "arguments" not in contract_queue or not contract_queue["arguments"]
    retry_queue = queues[builder.settings.broker_retry_queue]
    assert retry_queue["arguments"] == {"x-dead-letter-exchange": builder.settings.broker_exchange}
    assert queues[builder.settings.broker_dead_letter_queue]["durable"] is True

    bindings = {(binding["queue"], binding["exchange"], binding["routing_key"]) for binding in builder.channel.bindings}
    assert (builder.settings.broker_queue, builder.settings.broker_exchange, "order.confirmed") in bindings
    assert (builder.settings.broker_queue, builder.settings.broker_exchange, "order.cancelled") in bindings
    assert (builder.settings.broker_retry_queue, builder.settings.broker_retry_exchange, "#") in bindings
    assert (
        builder.settings.broker_dead_letter_queue,
        builder.settings.broker_dead_letter_exchange,
        "#",
    ) in bindings


def test_serving_once_uses_manual_acknowledgement_and_the_configured_prefetch() -> None:
    builder = Builder(consumer_prefetch=3)
    consumer = builder.build()

    consumer.serve_once()

    assert builder.channel.qos == 3
    assert builder.channel.confirms is True
    assert builder.channel.started is True
    assert builder.channel.consume is not None
    assert builder.channel.consume["queue"] == builder.settings.broker_queue
    assert builder.channel.consume["auto_ack"] is False
    assert builder.channel.consume["on_message_callback"] == consumer.on_message
    assert builder.connection.closed is True
    # the stop check was scheduled on the connection
    assert builder.connection.timers and builder.connection.timers[0][0] > 0


def test_the_stop_check_leaves_the_consume_loop() -> None:
    builder = Builder()
    consumer = builder.build()

    consumer.serve_once()
    _, check = builder.connection.timers[0]
    consumer.stop()
    check()

    assert builder.channel.stopped is True


def _router() -> EventRouter:
    return EventRouter.from_orchestrator(
        NotificationOrchestrator(repository=FakeNotificationRepository(), provider=RecordingProvider())
    )


def test_only_the_contract_routing_keys_may_be_consumed() -> None:
    with pytest.raises(ValueError):
        OrderEventConsumer(
            settings=make_settings(broker_routing_keys="order.shipped"),
            router=_router(),
        )


def test_a_broker_outage_is_survived_and_reported() -> None:
    """A broker that is down must not kill the consumer process."""

    attempts: list[Any] = []

    def factory(parameters: Any) -> Any:
        attempts.append(parameters)
        raise RuntimeError("broker is down")

    consumer = OrderEventConsumer(
        settings=make_settings(consumer_reconnect_interval_seconds=0.01),
        router=_router(),
        connection_factory=factory,
    )
    thread = consumer.run_in_thread()
    try:
        wait_until(lambda: consumer.last_error is not None, timeout=10, description="the outage is reported")
        assert attempts
        assert thread.is_alive() is True
    finally:
        consumer.stop()
        thread.join(10)
    assert thread.is_alive() is False


def test_the_consumer_recovers_when_the_broker_comes_back() -> None:
    channel = FakeChannel()
    outcomes: list[Any] = [RuntimeError("broker is down"), FakeConnection(channel)]

    def factory(_parameters: Any) -> Any:
        outcome = outcomes.pop(0) if outcomes else FakeConnection(channel)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    consumer = OrderEventConsumer(
        settings=make_settings(consumer_reconnect_interval_seconds=0.01),
        router=_router(),
        connection_factory=factory,
    )
    thread = consumer.run_in_thread()
    try:
        wait_until(lambda: channel.started, timeout=10, description="the consumer reconnects and consumes")
        assert channel.consume is not None
    finally:
        consumer.stop()
        thread.join(10)


# --------------------------------------------------------------------------- #
# fault injection and readiness probes
# --------------------------------------------------------------------------- #
def test_fault_injection_is_disabled_by_default() -> None:
    hooks = fault_hooks_from_env({})

    assert hooks.enabled is False
    assert hooks.after_send is None
    assert hooks.after_commit is None


def test_fault_injection_switches_are_read_from_the_environment() -> None:
    hooks = fault_hooks_from_env({CRASH_AFTER_PROVIDER_SEND_ENV: "1", CRASH_AFTER_COMMIT_ENV: "true"})

    assert hooks.enabled is True
    assert hooks.after_send is not None
    assert hooks.after_commit is not None


def test_broker_readiness_reports_an_unreachable_broker() -> None:
    def factory(_parameters: Any) -> Any:
        raise RuntimeError("connection refused")

    assert broker_is_reachable("amqp://shopflow:shopflow@localhost:5672/", connection_factory=factory) is False


def test_broker_readiness_reports_a_reachable_broker() -> None:
    connection = FakeConnection()

    assert (
        broker_is_reachable(
            "amqp://shopflow:shopflow@localhost:5672/",
            connection_factory=lambda _parameters: connection,
        )
        is True
    )
    assert connection.closed is True


def test_the_settings_produce_the_bounded_policy() -> None:
    consumer = OrderEventConsumer(
        settings=make_settings(
            consumer_max_delivery_attempts=4,
            consumer_retry_backoff_seconds=1,
            consumer_retry_backoff_max_seconds=8,
        ),
        router=_router(),
    )

    assert consumer.retry_policy.max_attempts == 4
    assert consumer.retry_policy.delay_for(3) == 4
    assert consumer.retry_policy.should_retry(4) is False
