"""Failure test of a crash between the commit and the acknowledgement.

The consumer exits hard right after the notification record was committed and before
the delivery was acknowledged (``SHOPFLOW_FAULT_CRASH_AFTER_COMMIT=1``). The broker
requeues the message; the restarted consumer must recognise it through
``processed_events`` and skip it, so the provider sees exactly one message and the
store holds exactly one row.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.provider_http_client import deterministic_message_id
from app.consumer.consumer import CRASH_AFTER_COMMIT_ENV
from tests.failure.conftest import CRASH_EXIT_CODE, running_consumer
from tests.support.db import notification_rows, processed_event_ids
from tests.support.events import event_id, order_confirmed, order_id
from tests.support.runtimes import wait_until


def test_a_crash_after_the_commit_skips_the_redelivery(
    case_id: str,
    broker,
    emulator,
    session_factory: sessionmaker[Session],
    wiring_factory,
    consumer_process,
) -> None:
    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-c")

    crashed = consumer_process(**{CRASH_AFTER_COMMIT_ENV: "1"})
    broker.publish_event(order_confirmed(order=order, event=event))

    wait_until(
        lambda: notification_rows(session_factory, order_id=order),
        description="the committed record before the crash",
    )
    exit_code = crashed.wait(timeout=90)
    assert exit_code == CRASH_EXIT_CODE, f"the consumer did not crash as injected:\n{crashed.log}"

    # committed, acknowledged nowhere: the broker will deliver the message again
    rows = notification_rows(session_factory, order_id=order)
    assert len(rows) == 1
    assert rows[0]["status"] == "SENT"
    assert emulator.count(order, "order_confirmation") == 1

    with running_consumer(wiring_factory()):
        wait_until(lambda: broker.depths()[broker.queue] == 0, description="the redelivery to be consumed")

    rows = notification_rows(session_factory, order_id=order)
    assert len(rows) == 1, f"the redelivery must not add a record, got {rows}"
    assert rows[0]["providerMessageId"] == deterministic_message_id("order_confirmation", order)
    assert emulator.count(order, "order_confirmation") == 1
    assert processed_event_ids(session_factory) == {event}
    assert broker.depths()[broker.retry_queue] == 0
    assert broker.depths()[broker.dead_letter_queue] == 0
