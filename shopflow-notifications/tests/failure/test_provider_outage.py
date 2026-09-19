"""Failure test of a provider outage (acceptance criterion 6).

A provider that answers ``5xx`` must lead to bounded retries, a terminal ``FAILED``
notification record and a message on the dead-letter queue - never to a silent drop
and never to an endless loop. Once the provider is healthy again, replaying the
dead-lettered message must not create a duplicate.
"""

from __future__ import annotations

import json
import time

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.provider_http_client import HttpNotificationProvider
from app.config import Settings
from app.consumer.consumer import (
    ATTEMPT_HEADER,
    EVENT_ID_HEADER,
    FAILURE_REASON_HEADER,
    FAILURE_RECORDED_HEADER,
)
from tests.failure.conftest import running_consumer
from tests.support.capture_provider import MODE_UNAVAILABLE
from tests.support.db import notification_rows, processed_event_ids
from tests.support.events import event_id, order_confirmed, order_id
from tests.support.runtimes import wait_until

SETTLE_SECONDS = 0.6


def rows_for(session_factory: sessionmaker[Session], order: str) -> list[dict[str, object]]:
    return notification_rows(session_factory, order_id=order)


def adapter_for(capture_provider, settings: Settings) -> HttpNotificationProvider:
    return HttpNotificationProvider(
        capture_provider.base_url,
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.provider_max_attempts,
        backoff_seconds=settings.provider_backoff_seconds,
        backoff_max_seconds=settings.provider_backoff_max_seconds,
    )


def test_an_outage_is_retried_then_recorded_as_failed_and_dead_lettered(
    case_id: str,
    broker,
    capture_provider,
    settings: Settings,
    session_factory: sessionmaker[Session],
    wiring_factory,
) -> None:
    capture_provider.reset(mode=MODE_UNAVAILABLE)
    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-c")

    with running_consumer(wiring_factory(provider=adapter_for(capture_provider, settings))):
        broker.publish_event(order_confirmed(order=order, event=event))
        wait_until(
            lambda: broker.depths()[broker.dead_letter_queue] == 1,
            description="the message on the dead-letter queue",
        )
        time.sleep(SETTLE_SECONDS)

    rows = rows_for(session_factory, order)
    assert len(rows) == 1, f"expected exactly one terminal record, got {rows}"
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["providerMessageId"] is None
    assert rows[0]["attempts"] == settings.consumer_max_delivery_attempts
    assert rows[0]["eventId"] == event
    assert event in processed_event_ids(session_factory)

    # the retries were bounded: attempts on the wire == consumer attempts x provider attempts
    assert capture_provider.request_count == (
        settings.consumer_max_delivery_attempts * settings.provider_max_attempts
    )
    assert capture_provider.count() == 0  # nothing was ever accepted
    # the contract queue is empty: the message was acknowledged after being dead-lettered
    assert broker.depths()[broker.queue] == 0
    assert broker.depths()[broker.retry_queue] == 0

    # the message is preserved on the dead-letter queue with its failure metadata
    dead_letter = broker.take_message(broker.dead_letter_queue)
    assert dead_letter is not None
    assert dead_letter["headers"][FAILURE_REASON_HEADER]
    assert "ProviderUnavailable" in str(dead_letter["headers"][FAILURE_REASON_HEADER])
    assert dead_letter["headers"][ATTEMPT_HEADER] == settings.consumer_max_delivery_attempts
    assert dead_letter["headers"][EVENT_ID_HEADER] == event
    assert dead_letter["headers"][FAILURE_RECORDED_HEADER] == "true"
    assert json.loads(dead_letter["body"])["orderId"] == order
    assert json.loads(dead_letter["body"])["eventId"] == event


def test_a_recovered_provider_and_a_replay_create_no_duplicate(
    case_id: str,
    broker,
    capture_provider,
    emulator,
    settings: Settings,
    session_factory: sessionmaker[Session],
    wiring_factory,
) -> None:
    """Criterion 6, second half: after the provider returns, nothing has to be replayed."""

    capture_provider.reset(mode=MODE_UNAVAILABLE)
    failed_order = order_id(f"{case_id}-failed")
    healthy_order = order_id(f"{case_id}-healthy")

    with running_consumer(wiring_factory(provider=adapter_for(capture_provider, settings))):
        broker.publish_event(order_confirmed(order=failed_order, event=event_id(f"{case_id}-failed")))
        wait_until(
            lambda: broker.depths()[broker.dead_letter_queue] == 1,
            description="the terminal failure",
        )

        # the provider comes back
        capture_provider.reset()
        dead_letter = broker.take_message(broker.dead_letter_queue)
        assert dead_letter is not None
        broker.replay(dead_letter)  # an operator replays the dead letter
        time.sleep(SETTLE_SECONDS * 2)

        assert capture_provider.count("order_confirmation", failed_order) == 0
        assert len(rows_for(session_factory, failed_order)) == 1
        assert rows_for(session_factory, failed_order)[0]["status"] == "FAILED"
        assert broker.depths()[broker.queue] == 0  # the replayed message was acknowledged
        assert broker.depths()[broker.dead_letter_queue] == 0
        assert capture_provider.request_count == 0  # the replay never reached the provider

        # and the service keeps working for the next order
        broker.publish_event(order_confirmed(order=healthy_order, event=event_id(f"{case_id}-healthy")))
        wait_until(
            lambda: capture_provider.count("order_confirmation", healthy_order) == 1,
            description="the notification after the outage",
        )

    rows = rows_for(session_factory, healthy_order)
    assert len(rows) == 1
    assert rows[0]["status"] == "SENT"
    # exactly one accepted call: the replay added none, the healthy order added one
    assert capture_provider.request_count == 1
    assert capture_provider.count("order_confirmation", healthy_order) == 1
    assert emulator.count(failed_order) == 0  # the failed order was never (and is not) delivered
