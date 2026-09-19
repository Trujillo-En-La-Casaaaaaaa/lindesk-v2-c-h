"""Failure test of a crash between provider delivery and the commit (criterion 5).

The consumer is started as a real OS process that exits hard right after the
provider accepted the message (``SHOPFLOW_FAULT_CRASH_AFTER_PROVIDER_SEND=1``), so
the delivery window of the handoff is reproduced rather than guessed at:

* the provider already holds the message,
* the notification record was never committed,
* the delivery was never acknowledged, so the broker requeues it.

A *new* consumer process must then finish the job without producing a second
message - the provider's ``Idempotency-Key`` returns the original ``messageId``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.provider_http_client import deterministic_message_id
from app.consumer.consumer import CRASH_AFTER_PROVIDER_SEND_ENV
from tests.failure.conftest import CRASH_EXIT_CODE, running_consumer
from tests.support.db import notification_rows, processed_event_ids
from tests.support.events import event_id, order_confirmed, order_id
from tests.support.runtimes import wait_until


def test_a_crash_after_delivery_is_recovered_without_a_second_message(
    case_id: str,
    broker,
    emulator,
    session_factory: sessionmaker[Session],
    wiring_factory,
    consumer_process,
) -> None:
    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-c")

    crashed = consumer_process(**{CRASH_AFTER_PROVIDER_SEND_ENV: "1"})
    broker.publish_event(order_confirmed(order=order, event=event))

    # the provider accepted the message before the crash
    wait_until(
        lambda: emulator.count(order, "order_confirmation") == 1,
        description="the provider delivery before the crash",
    )
    exit_code = crashed.wait(timeout=90)
    assert exit_code == CRASH_EXIT_CODE, f"the consumer did not crash as injected:\n{crashed.log}"

    # the crash window: delivered but not committed, and not acknowledged
    assert notification_rows(session_factory, order_id=order) == []
    assert processed_event_ids(session_factory) == set()
    assert emulator.count(order, "order_confirmation") == 1

    # a fresh consumer finishes the job after the restart
    with running_consumer(wiring_factory()):
        wait_until(
            lambda: notification_rows(session_factory, order_id=order),
            description="the record after the restart",
        )
        wait_until(lambda: broker.depths()[broker.queue] == 0, description="the queue to drain")

    rows = notification_rows(session_factory, order_id=order)
    assert len(rows) == 1, f"expected exactly one record after the restart, got {rows}"
    assert rows[0]["status"] == "SENT"
    assert rows[0]["eventId"] == event
    assert rows[0]["providerMessageId"] == deterministic_message_id("order_confirmation", order)
    # the provider idempotency key prevented a second message
    assert emulator.count(order, "order_confirmation") == 1
    assert emulator.count(order) == 1
    assert processed_event_ids(session_factory) == {event}
