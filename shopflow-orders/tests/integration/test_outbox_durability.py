"""Integration tests of the transactional outbox and the real broker (criterion 10).

The broker here is the real RabbitMQ of shopflow-infra: the assertions are made on
messages that the broker actually delivered to a queue bound to the durable topic
exchange ``shopflow.orders``, not on an in-memory stand-in. The store is the real
``outbox`` table.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.postgres_outbox import PostgresOutbox
from app.adapters.rabbitmq_publisher import RabbitMqPublisher
from app.config import BROKER_EXCHANGE
from app.ports.outbox import BrokerUnavailable, OutboxMessage
from tests.integration.conftest import make_publisher
from tests.support.broker import BrokerObservation, broker_reachable
from tests.support.db import outbox_rows, unpublished_outbox_rows
from tests.support.servers import wait_until

UNREACHABLE_BROKER = "amqp://shopflow:shopflow@127.0.0.1:1/"


@pytest.fixture()
def real_broker(broker_url: str):
    if not broker_reachable(broker_url, timeout_seconds=5.0):
        pytest.fail(
            "the broker is not reachable; start it with\n"
            "  docker compose -f ../shopflow-infra/docker-compose.yml up -d message-broker broker-init"
        )
    publisher = RabbitMqPublisher(broker_url, exchange=BROKER_EXCHANGE, connect_timeout_seconds=5.0)
    try:
        yield publisher
    finally:
        publisher.close()


def place_order(client: TestClient, *, customer_id: str = "c-outbox", product_id: str = "p-1004") -> dict:
    response = client.post(
        "/orders",
        json={"customerId": customer_id, "lines": [{"productId": product_id, "quantity": 1}]},
        headers={"X-Correlation-Id": "corr-outbox"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_confirmed_event_reaches_the_real_broker_exactly_once(
    client: TestClient,
    session_factory: sessionmaker[Session],
    broker_url: str,
    real_broker: RabbitMqPublisher,
) -> None:
    order = place_order(client)
    stored = outbox_rows(session_factory, order_id=order["orderId"])
    assert len(stored) == 1
    assert stored[0]["published_at"] is None

    with BrokerObservation(broker_url) as observation:
        relay = make_publisher(PostgresOutbox(session_factory), real_broker)
        assert relay.drain() == 1
        delivered = observation.wait_for(1, timeout=20.0)

    assert len(delivered) == 1
    message = delivered[0]
    assert message["routingKey"] == "order.confirmed"
    assert message["exchange"] == BROKER_EXCHANGE
    assert message["deliveryMode"] == 2  # persistent
    assert message["messageId"] == stored[0]["event_id"]
    assert message["body"] == stored[0]["payload"]

    after = outbox_rows(session_factory, order_id=order["orderId"])[0]
    assert after["published_at"] is not None
    assert after["attempts"] == 0
    assert unpublished_outbox_rows(session_factory) == []


def test_a_cancelled_event_is_published_with_the_reason(
    client: TestClient,
    session_factory: sessionmaker[Session],
    broker_url: str,
    real_broker: RabbitMqPublisher,
) -> None:
    order = place_order(client)
    client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "customer changed their mind"})

    with BrokerObservation(broker_url) as observation:
        relay = make_publisher(PostgresOutbox(session_factory), real_broker)
        relay.drain()
        delivered = observation.wait_for(2, timeout=20.0)

    assert {message["messageId"] for message in delivered} == {
        row["event_id"] for row in outbox_rows(session_factory, order_id=order["orderId"])
    }
    cancelled = [message for message in delivered if message["routingKey"] == "order.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["body"]["reason"] == "customer changed their mind"
    assert cancelled[0]["body"]["eventType"] == "order.cancelled"
    assert cancelled[0]["headers"]["x-shopflow-event-type"] == "order.cancelled"


def test_exactly_one_event_per_type_for_the_whole_lifecycle(
    client: TestClient,
    session_factory: sessionmaker[Session],
    broker_url: str,
    real_broker: RabbitMqPublisher,
) -> None:
    order = place_order(client, customer_id="c-outbox-lifecycle")
    client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})
    client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind again"})
    client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})

    rows = outbox_rows(session_factory, order_id=order["orderId"])
    assert [row["event_type"] for row in rows] == ["order.confirmed", "order.cancelled"]

    with BrokerObservation(broker_url) as observation:
        relay = make_publisher(PostgresOutbox(session_factory), real_broker)
        relay.drain()
        observation.wait_for(2, timeout=20.0)
        observation.drain(timeout=1.0)  # give a wrongly duplicated publish the chance to show up
        delivered = list(observation.messages)
        assert make_publisher(PostgresOutbox(session_factory), real_broker).drain() == 0

    assert len(delivered) == 2
    assert sorted(message["routingKey"] for message in delivered) == ["order.cancelled", "order.confirmed"]
    assert len({message["messageId"] for message in delivered}) == 2


def test_the_relay_survives_a_restart_between_publish_and_mark_published(
    client: TestClient,
    session_factory: sessionmaker[Session],
    broker_url: str,
    real_broker: RabbitMqPublisher,
) -> None:
    """The crash window of acceptance criterion 10, against the real broker.

    The broker confirmed the publish, then the process died before ``published_at``
    was written. A restarted relay republishes **the same ``eventId``**: delivery is
    at-least-once (contracts.md §8) and the consumer deduplicates by ``eventId``, so
    the order still produces exactly one logical event.
    """

    order = place_order(client, customer_id="c-outbox-crash")
    event_id = outbox_rows(session_factory, order_id=order["orderId"])[0]["event_id"]

    class CrashingOutbox(PostgresOutbox):
        """The store of a process that dies right after the broker confirmed the publish."""

        def __init__(self, session_factory) -> None:
            super().__init__(session_factory)
            self.crashes_left = 1

        def mark_published(self, record_id: str, *, published_at) -> None:
            if self.crashes_left:
                self.crashes_left -= 1
                raise RuntimeError("the process died before the mark was written")
            super().mark_published(record_id, published_at=published_at)

    with BrokerObservation(broker_url) as observation:
        with pytest.raises(RuntimeError):
            make_publisher(CrashingOutbox(session_factory), real_broker).run_once()
        first_delivery = observation.wait_for(1, timeout=20.0)
        assert len(first_delivery) == 1
        assert first_delivery[0]["messageId"] == event_id
        assert unpublished_outbox_rows(session_factory) != []  # the row thinks it is unpublished

        # restart: a fresh relay over the same store, as a restarted process would be
        restarted_broker = RabbitMqPublisher(broker_url, exchange=BROKER_EXCHANGE, connect_timeout_seconds=5.0)
        try:
            restarted = make_publisher(PostgresOutbox(session_factory), restarted_broker)
            assert restarted.drain() == 1
        finally:
            restarted_broker.close()

        observation.wait_for(2, timeout=20.0)
        observation.drain(timeout=1.0)
        delivered = list(observation.messages)

    assert len(delivered) == 2
    assert {message["messageId"] for message in delivered} == {event_id}  # one logical event
    assert len({json.dumps(message["body"], sort_keys=True) for message in delivered}) == 1
    assert unpublished_outbox_rows(session_factory) == []
    assert outbox_rows(session_factory, order_id=order["orderId"])[0]["published_at"] is not None


def test_a_broker_outage_never_loses_the_event(
    client: TestClient,
    session_factory: sessionmaker[Session],
    broker_url: str,
    real_broker: RabbitMqPublisher,
) -> None:
    order = place_order(client, customer_id="c-outbox-outage")
    store = PostgresOutbox(session_factory)

    down = RabbitMqPublisher(UNREACHABLE_BROKER, exchange=BROKER_EXCHANGE, connect_timeout_seconds=2.0)
    try:
        report = make_publisher(store, down).run_once()
    finally:
        down.close()

    assert report.published == 0
    assert report.pending == 1
    pending = unpublished_outbox_rows(session_factory)
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["published_at"] is None

    with BrokerObservation(broker_url) as observation:
        assert make_publisher(PostgresOutbox(session_factory), real_broker).drain() == 1
        delivered = observation.wait_for(1, timeout=20.0)

    assert [message["body"]["orderId"] for message in delivered] == [order["orderId"]]
    assert unpublished_outbox_rows(session_factory) == []


def test_a_broker_publish_is_idempotent_for_the_same_event_id(
    client: TestClient, session_factory: sessionmaker[Session], broker_url: str
) -> None:
    """The same outbox row is never published twice once it is marked published."""

    order = place_order(client, customer_id="c-outbox-idempotent")
    store = PostgresOutbox(session_factory)
    publisher = RabbitMqPublisher(broker_url, exchange=BROKER_EXCHANGE, connect_timeout_seconds=5.0)
    try:
        with BrokerObservation(broker_url) as observation:
            relay = make_publisher(store, publisher)
            relay.drain()
            observation.wait_for(1, timeout=20.0)
            assert relay.drain() == 0  # nothing left to publish
            observation.drain(timeout=1.0)
            delivered = list(observation.messages)
    finally:
        publisher.close()

    assert len(delivered) == 1
    event_id = outbox_rows(session_factory, order_id=order["orderId"])[0]["event_id"]
    assert delivered[0]["messageId"] == event_id


def test_the_application_starts_the_relays_in_its_lifespan(
    app_factory,
    session_factory: sessionmaker[Session],
    broker_url: str,
    real_broker: RabbitMqPublisher,
) -> None:
    """The composed process publishes without any external worker being started."""

    app = app_factory(
        broker_override=real_broker,
        start_workers=True,
        outbox_poll_interval_seconds=0.05,
        release_retry_interval_seconds=0.1,
    )
    with BrokerObservation(broker_url) as observation, TestClient(app) as running_client:
        created = running_client.post(
            "/orders",
            json={"customerId": "c-outbox-lifespan", "lines": [{"productId": "p-1004", "quantity": 1}]},
        )
        assert created.status_code == 201
        order_id = created.json()["orderId"]
        assert wait_until(lambda: observation.poll() > 0 or bool(observation.messages), timeout=30.0)

    delivered = observation.payloads_of(order_id)
    assert len(delivered) == 1
    assert delivered[0]["eventType"] == "order.confirmed"
    rows = outbox_rows(session_factory, order_id=order_id)
    assert [row["event_type"] for row in rows] == ["order.confirmed"]
    assert rows[0]["published_at"] is not None


def test_an_unconfirmed_publish_is_kept_unpublished(
    client: TestClient, session_factory: sessionmaker[Session], broker_url: str
) -> None:
    """A publish the broker does not confirm keeps the row for a later retry."""

    class RejectingBroker(RabbitMqPublisher):
        def publish(self, message: OutboxMessage) -> None:
            del message
            raise BrokerUnavailable("the broker did not confirm the publish")

    order = place_order(client, customer_id="c-outbox-unconfirmed")
    report = make_publisher(PostgresOutbox(session_factory), RejectingBroker(broker_url)).run_once()

    assert report.published == 0
    pending = unpublished_outbox_rows(session_factory)
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["payload"]["orderId"] == order["orderId"]
