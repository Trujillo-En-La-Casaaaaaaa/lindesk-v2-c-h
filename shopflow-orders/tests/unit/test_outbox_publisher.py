"""Unit tests of the outbox publisher relay (HANDOFF #6, acceptance criterion 10)."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.order import Order, OrderLine, OrderStatus, order_total
from app.events.contracts import EVENT_ORDER_CANCELLED, EVENT_ORDER_CONFIRMED, order_confirmed_event
from app.ports.outbox import BrokerUnavailable, OutboxMessage
from app.workers.outbox_publisher import OutboxPublisher
from tests.support.fakes import FakeBroker, InMemoryOutboxStore
from tests.support.servers import wait_until

MOMENT = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def message(event_type: str, *, order_id: str, occurred_at: datetime, event_id: str | None = None) -> OutboxMessage:
    return OutboxMessage.new(
        event_type,
        {
            "eventId": event_id or str(uuid.uuid4()),
            "eventType": event_type,
            "eventVersion": 1,
            "occurredAt": occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "correlationId": "corr",
            "orderId": order_id,
            "customerId": "c-1",
            "lines": [{"productId": "p-1001", "quantity": 2, "unitPrice": 19.99}],
            "reason": "changed mind" if event_type == EVENT_ORDER_CANCELLED else None,
        },
        occurred_at=occurred_at,
        event_id=event_id,
    )


def store_with(*messages: OutboxMessage) -> InMemoryOutboxStore:
    store = InMemoryOutboxStore()
    for item in messages:
        store.insert_message(item)
    return store


def publisher(store: InMemoryOutboxStore, broker: FakeBroker, **kwargs) -> OutboxPublisher:
    return OutboxPublisher(
        store=store,
        broker=broker,
        batch_size=kwargs.pop("batch_size", 10),
        poll_interval_seconds=kwargs.pop("poll_interval_seconds", 0.01),
        max_backoff_seconds=kwargs.pop("max_backoff_seconds", 0.05),
        now=kwargs.pop("now", lambda: MOMENT),
    )


def test_publishes_pending_rows_oldest_first_and_marks_them() -> None:
    first = message(EVENT_ORDER_CONFIRMED, order_id="o-1", occurred_at=MOMENT)
    second = message(EVENT_ORDER_CANCELLED, order_id="o-1", occurred_at=MOMENT + timedelta(seconds=1))
    store = store_with(first, second)
    broker = FakeBroker()

    report = publisher(store, broker).run_once()

    assert report.published == 2
    assert report.pending == 0
    assert report.failure is None
    assert [item.event_id for item in broker.published] == [first.event_id, second.event_id]
    assert [item.event_type for item in broker.published] == [EVENT_ORDER_CONFIRMED, EVENT_ORDER_CANCELLED]
    assert all(row["published_at"] == MOMENT for row in store.rows)


def test_published_rows_are_not_published_again() -> None:
    store = store_with(message(EVENT_ORDER_CONFIRMED, order_id="o-1", occurred_at=MOMENT))
    broker = FakeBroker()
    relay = publisher(store, broker)

    relay.run_once()
    report = relay.run_once()

    assert report.published == 0
    assert len(broker.published) == 1


def test_a_broker_outage_keeps_the_rows_unpublished_and_counts_the_attempt() -> None:
    store = store_with(message(EVENT_ORDER_CONFIRMED, order_id="o-1", occurred_at=MOMENT))
    broker = FakeBroker()
    broker.failures_remaining = 1

    report = publisher(store, broker).run_once()

    assert report.published == 0
    assert report.failed == 1
    assert report.pending == 1
    assert "broker" in str(report.failure).lower()
    assert broker.published == []
    assert store.rows[0]["attempts"] == 1
    assert store.rows[0]["published_at"] is None


def test_a_recovered_broker_receives_the_event_without_any_loss() -> None:
    first = message(EVENT_ORDER_CONFIRMED, order_id="o-1", occurred_at=MOMENT)
    second = message(EVENT_ORDER_CANCELLED, order_id="o-1", occurred_at=MOMENT + timedelta(seconds=1))
    store = store_with(first, second)
    broker = FakeBroker()
    broker.failures_remaining = 1

    relay = publisher(store, broker)
    assert relay.run_once().pending == 2  # outage: both rows still unpublished
    assert relay.run_once().published == 2  # recovery: nothing was lost

    assert [item.event_id for item in broker.published] == [first.event_id, second.event_id]
    assert store.count_unpublished() == 0


def test_the_relay_survives_a_restart_between_publish_and_mark_published() -> None:
    """The crash window of acceptance criterion 10.

    The broker confirmed the message but the process died before ``published_at``
    was written. The row is still unpublished, so a *new* relay instance republishes
    *the same* ``eventId`` - at-least-once delivery, exactly one logical event for a
    consumer that deduplicates by ``eventId`` (contracts.md §8).
    """

    event_id = "9c8b7a65-4321-4321-9876-0fedcba98765"
    original = message(EVENT_ORDER_CONFIRMED, order_id="o-1", occurred_at=MOMENT, event_id=event_id)

    class CrashingStore(InMemoryOutboxStore):
        """The store of a process that dies *after* the broker confirmed the publish."""

        def __init__(self) -> None:
            super().__init__()
            self.crashes_left = 1

        def mark_published(self, record_id: str, *, published_at: datetime) -> None:
            if self.crashes_left:
                self.crashes_left -= 1
                raise RuntimeError("the process died before the mark was written")
            super().mark_published(record_id, published_at=published_at)

    crashing = CrashingStore()
    crashing.insert_message(original)
    broker = FakeBroker()

    with pytest.raises(RuntimeError):
        publisher(crashing, broker).run_once()

    assert len(broker.published) == 1  # the broker did receive it
    assert crashing.rows[0]["published_at"] is None  # ... but the row thinks otherwise

    # restart: a fresh relay, the same store, the same broker
    restarted = publisher(crashing, broker)
    assert restarted.run_once().published == 1

    assert [item.event_id for item in broker.published] == [event_id, event_id]
    assert len({item.event_id for item in broker.published}) == 1  # one logical event
    assert {item.payload["orderId"] for item in broker.published} == {"o-1"}
    assert crashing.rows[0]["published_at"] is not None


def test_the_batch_bound_is_respected_and_never_skips_a_row() -> None:
    store = store_with(
        *[message(EVENT_ORDER_CONFIRMED, order_id=f"o-{index}", occurred_at=MOMENT) for index in range(5)]
    )
    broker = FakeBroker()
    relay = publisher(store, broker, batch_size=2)

    assert relay.run_once().published == 2
    assert relay.run_once().published == 2
    assert relay.run_once().published == 1
    assert len({item.payload["orderId"] for item in broker.published}) == 5


def test_drain_flushes_the_outbox() -> None:
    store = store_with(
        *[message(EVENT_ORDER_CONFIRMED, order_id=f"o-{index}", occurred_at=MOMENT) for index in range(3)]
    )
    broker = FakeBroker()
    assert publisher(store, broker, batch_size=1).drain() == 3
    assert store.count_unpublished() == 0


def test_run_forever_publishes_and_stops_on_the_stop_event() -> None:
    store = store_with(message(EVENT_ORDER_CONFIRMED, order_id="o-1", occurred_at=MOMENT))
    broker = FakeBroker()
    relay = publisher(store, broker)
    stop_event = threading.Event()
    result: list[int] = []

    thread = threading.Thread(target=lambda: result.append(relay.run_forever(stop_event)), daemon=True)
    thread.start()
    assert wait_until(lambda: len(broker.published) == 1, timeout=5.0)
    stop_event.set()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result == [1]  # run_forever reports what it published
    assert [item.payload["orderId"] for item in broker.published] == ["o-1"]
    assert store.count_unpublished() == 0


def test_an_unexpected_broker_error_is_also_retried_not_lost() -> None:
    store = store_with(message(EVENT_ORDER_CONFIRMED, order_id="o-1", occurred_at=MOMENT))
    broker = FakeBroker()

    def explode(_message: OutboxMessage) -> None:
        raise ValueError("unexpected client failure")

    broker.before_publish = explode
    relay = publisher(store, broker)
    assert relay.run_once().failed == 1
    assert store.rows[0]["attempts"] == 1

    broker.before_publish = None
    assert relay.run_once().published == 1
    assert len(broker.published) == 1


def test_the_relay_uses_the_stable_event_id_of_the_envelope() -> None:
    lines = (OrderLine("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", 2, Decimal("19.99")),)
    order = Order(
        order_id="0f14de0e-6f2e-4b7a-9f5b-1f3f1d9c1111",
        customer_id="c-1",
        status=OrderStatus.CONFIRMED,
        lines=lines,
        total=order_total(lines),
        created_at=MOMENT,
        updated_at=MOMENT,
    )
    envelope = order_confirmed_event(order, correlation_id="corr", occurred_at=MOMENT)
    store = store_with(envelope)
    broker = FakeBroker()

    publisher(store, broker).run_once()

    assert broker.published[0].event_id == envelope.event_id == envelope.payload["eventId"]


def test_close_closes_the_broker() -> None:
    broker = FakeBroker()
    relay = publisher(InMemoryOutboxStore(), broker)
    relay.close()
    assert broker.closed is True


def test_broker_unavailable_is_the_documented_error_type() -> None:
    assert issubclass(BrokerUnavailable, Exception)
    assert BrokerUnavailable("x").code == "internal_error"
