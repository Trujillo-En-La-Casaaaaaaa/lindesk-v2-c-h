"""Integration tests of the broker topology (contracts.md §8, handoff #1).

Everything here talks to the real RabbitMQ broker with pika. The declarations the
consumer performs are exercised against the queue and exchange that
``shopflow-infra`` (``broker/definitions.json`` and ``broker-init``) already created,
which is what proves the two declarations are equivalent - a mismatch would raise
``ChannelClosedByBroker`` (406 precondition failed).

The delayed requeue is proven by behaviour, not by reading queue arguments: a
message published to the retry exchange with a per-message TTL must come back on the
contract queue with its attempt counter and its routing key intact.
"""

from __future__ import annotations

import json
import time

import pika
import pytest

from app.config import Settings
from app.consumer.consumer import ATTEMPT_HEADER, ORIGINAL_ROUTING_KEY_HEADER, OrderEventConsumer
from app.consumer.router import EventRouter
from app.domain.notification import NotificationType
from app.handlers.orchestrator import NotificationOrchestrator
from tests.support.broker import ATTEMPT_HEADER as BROKER_ATTEMPT_HEADER
from tests.support.events import event_id, order_confirmed, order_id
from tests.support.fakes import FakeNotificationRepository, RecordingProvider
from tests.support.runtimes import wait_until


def build_consumer(settings: Settings) -> OrderEventConsumer:
    orchestrator = NotificationOrchestrator(
        repository=FakeNotificationRepository(),
        provider=RecordingProvider(),
    )
    return OrderEventConsumer(settings=settings, router=EventRouter.from_orchestrator(orchestrator))


def test_the_contract_queue_exists_and_our_declaration_is_equivalent(broker, settings: Settings) -> None:
    consumer = build_consumer(settings)

    with broker.channel() as channel:
        # passive: the durable queue of contracts.md §8 exists (created by shopflow-infra)
        declared = channel.queue_declare(queue=settings.broker_queue, passive=True)
        assert declared.method.queue == settings.broker_queue
        # active with our own arguments: equivalent declarations never raise 406
        consumer.declare_topology(channel)
        channel.queue_declare(queue=settings.broker_queue, durable=True, auto_delete=False)


def test_the_consumer_topology_is_durable_and_complete(broker, settings: Settings) -> None:
    consumer = build_consumer(settings)

    with broker.channel() as channel:
        consumer.declare_topology(channel)

        channel.exchange_declare(exchange=settings.broker_exchange, exchange_type="topic", durable=True, passive=True)
        channel.exchange_declare(
            exchange=settings.broker_retry_exchange,
            exchange_type="topic",
            durable=True,
            passive=True,
        )
        channel.exchange_declare(
            exchange=settings.broker_dead_letter_exchange,
            exchange_type="fanout",
            durable=True,
            passive=True,
        )
        channel.queue_declare(queue=settings.broker_queue, durable=True, passive=True)
        channel.queue_declare(queue=settings.broker_retry_queue, durable=True, passive=True)
        channel.queue_declare(queue=settings.broker_dead_letter_queue, durable=True, passive=True)


@pytest.mark.parametrize("routing_key", ["order.confirmed", "order.cancelled"])
def test_both_contract_routing_keys_are_bound_to_the_queue(broker, routing_key: str) -> None:
    broker.purge()

    broker.publish_raw(b"{}", routing_key=routing_key)

    assert broker.wait_for_depth(broker.queue, 1) == 1
    message = broker.take_message(broker.queue)
    assert message is not None
    assert message["routingKey"] == routing_key


def test_an_unbound_routing_key_is_not_delivered_to_the_contract_queue(broker) -> None:
    broker.purge()

    broker.publish_raw(b"{}", routing_key="order.shipped")
    time.sleep(0.3)

    assert broker.depths()[broker.queue] == 0


def test_the_retry_queue_requeues_the_message_after_the_backoff(broker, settings: Settings) -> None:
    """The bounded retry relies on this: TTL expiry dead-letters back to the contract queue."""

    broker.purge()
    consumer = build_consumer(settings)
    with broker.channel() as channel:
        consumer.declare_topology(channel)
        channel.basic_publish(
            exchange=settings.broker_retry_exchange,
            routing_key="order.confirmed",
            body=b'{"probe": "retry"}',
            properties=pika.BasicProperties(
                delivery_mode=2,
                headers={ATTEMPT_HEADER: 1},
                expiration="250",
            ),
        )

    message = None
    deadline = time.monotonic() + 20
    while message is None and time.monotonic() < deadline:
        assert broker.depths()[broker.queue] <= 1
        message = broker.take_message(broker.queue)
        if message is None:
            time.sleep(0.1)

    assert message is not None, "the retried message never came back to the contract queue"
    assert message["routingKey"] == "order.confirmed"
    assert message["headers"][ATTEMPT_HEADER] == 1
    assert json.loads(message["body"]) == {"probe": "retry"}
    assert broker.depths()[broker.retry_queue] == 0


def test_the_consumer_marks_the_original_routing_key_when_it_retries(broker, settings: Settings) -> None:
    """The header an operator finds on a retried message is written by the consumer."""

    from app.consumer.consumer import Disposition
    from tests.support.fakes import FakeChannel

    channel = FakeChannel()
    consumer = build_consumer(settings)
    consumer.publish_retry(
        channel,
        b'{"probe": "retry"}',
        "order.confirmed",
        type("P", (), {"headers": {}, "correlation_id": None})(),
        Disposition(action="retry", attempt=1, reason="probe", delay_seconds=0.25),
    )

    published = channel.published[0]
    assert published["exchange"] == settings.broker_retry_exchange
    assert published["properties"].headers[ORIGINAL_ROUTING_KEY_HEADER] == "order.confirmed"
    assert published["properties"].expiration == "250"


def test_a_finished_retry_leaves_no_leftover(broker) -> None:
    """After the requeue, nothing is stuck in the retry queue."""

    assert broker.depths()[broker.retry_queue] == 0
    assert len(broker.depths()) == 3


def test_the_dead_letter_queue_receives_failed_messages(broker, settings: Settings) -> None:
    broker.purge()
    consumer = build_consumer(settings)
    with broker.channel() as channel:
        consumer.declare_topology(channel)
        channel.basic_publish(
            exchange=settings.broker_dead_letter_exchange,
            routing_key="order.confirmed",
            body=b'{"probe": "dead"}',
            properties=pika.BasicProperties(delivery_mode=2, headers={"x-shopflow-failure-reason": "probe"}),
        )

    assert broker.wait_for_depth(broker.dead_letter_queue, 1) == 1
    message = broker.take_message(broker.dead_letter_queue)
    assert message is not None
    assert message["headers"]["x-shopflow-failure-reason"] == "probe"
    assert BROKER_ATTEMPT_HEADER == ATTEMPT_HEADER


def test_the_consumer_drains_a_published_contract_event(
    settings: Settings,
    engine,
    broker,
    emulator,
    case_id: str,
) -> None:
    """A message published exactly as shopflow-orders publishes it is consumed and acked.

    The consumer is assembled by hand here (real repository adapter, real provider
    adapter) instead of through the application factory, so the two wirings are both
    covered.
    """

    from app.adapters.postgres_repository import PostgresNotificationRepository
    from app.adapters.provider_http_client import HttpNotificationProvider
    from app.db.session import create_session_factory
    from tests.support.runtimes import ConsumerThread

    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-c")
    repository = PostgresNotificationRepository(create_session_factory(engine))
    provider = HttpNotificationProvider(
        settings.provider_base_url,
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.provider_max_attempts,
        backoff_seconds=settings.provider_backoff_seconds,
        backoff_max_seconds=settings.provider_backoff_max_seconds,
    )
    consumer = OrderEventConsumer(
        settings=settings,
        router=EventRouter.from_orchestrator(
            NotificationOrchestrator(repository=repository, provider=provider)
        ),
    )
    with ConsumerThread(consumer):
        broker.publish_event(order_confirmed(order=order, event=event))
        wait_until(lambda: repository.event_processed(event), description="the delivery")
        wait_until(lambda: broker.depths()[broker.queue] == 0, description="the queue to drain")

    assert emulator.count(order, "order_confirmation") == 1
    assert repository.find_by_order_and_type(order, NotificationType.CONFIRMATION) is not None
