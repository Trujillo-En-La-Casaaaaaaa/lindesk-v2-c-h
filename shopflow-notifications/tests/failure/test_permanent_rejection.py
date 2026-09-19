"""Failure test of a permanent provider rejection (handoff #5).

A ``4xx`` is not worth retrying: the consumer must dead-letter the message at once,
record a ``FAILED`` notification with the attempts it really made, and keep
consuming. Retrying a permanent rejection would only grow the dead letters later.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.provider_http_client import HttpNotificationProvider
from app.config import Settings
from app.consumer.consumer import FAILURE_REASON_HEADER, FAILURE_RECORDED_HEADER
from tests.failure.conftest import running_consumer
from tests.support.capture_provider import MODE_REJECTED
from tests.support.db import notification_rows, processed_event_ids
from tests.support.events import event_id, order_confirmed, order_id
from tests.support.runtimes import wait_until


def test_a_permanent_rejection_is_dead_lettered_on_the_first_attempt(
    case_id: str,
    broker,
    capture_provider,
    settings: Settings,
    session_factory: sessionmaker[Session],
    wiring_factory,
) -> None:
    capture_provider.reset(mode=MODE_REJECTED)
    provider = HttpNotificationProvider(
        capture_provider.base_url,
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.provider_max_attempts,
        backoff_seconds=settings.provider_backoff_seconds,
        backoff_max_seconds=settings.provider_backoff_max_seconds,
    )
    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-c")

    with running_consumer(wiring_factory(provider=provider)):
        broker.publish_event(order_confirmed(order=order, event=event))
        wait_until(
            lambda: broker.depths()[broker.dead_letter_queue] == 1,
            description="the dead letter",
        )
        wait_until(lambda: broker.depths()[broker.queue] == 0, description="the queue to drain")

    rows = notification_rows(session_factory, order_id=order)
    assert len(rows) == 1
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["attempts"] == 1  # the permanent rejection did not burn the retry budget
    assert rows[0]["providerMessageId"] is None
    assert capture_provider.request_count == 1  # exactly one attempt on the wire
    assert processed_event_ids(session_factory) == {event}
    assert broker.depths()[broker.retry_queue] == 0

    dead_letter = broker.take_message(broker.dead_letter_queue)
    assert dead_letter is not None
    assert "provider_rejected" in str(dead_letter["headers"][FAILURE_REASON_HEADER])
    assert dead_letter["headers"][FAILURE_RECORDED_HEADER] == "true"
