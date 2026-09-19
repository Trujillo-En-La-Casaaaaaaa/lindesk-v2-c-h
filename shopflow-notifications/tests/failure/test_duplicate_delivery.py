"""Failure tests of duplicate delivery (acceptance criteria 2 and 3).

Both deduplication checks are exercised end to end over the real broker: the same
``eventId`` twice, and a second event carrying a **different** ``eventId`` for the
same order. Neither may reach the provider twice.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.provider_http_client import HttpNotificationProvider
from app.config import Settings
from tests.failure.conftest import running_consumer
from tests.support.db import notification_rows, processed_event_ids
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id
from tests.support.runtimes import wait_until

SETTLE_SECONDS = 0.6


def rows_for(session_factory: sessionmaker[Session], order: str) -> list[dict[str, object]]:
    return notification_rows(session_factory, order_id=order)


def test_the_same_event_id_delivered_twice_produces_one_message(
    case_id: str,
    broker,
    emulator,
    session_factory: sessionmaker[Session],
    wiring_factory,
) -> None:
    """Criterion 2: re-publishing the same event produces no additional emulator message."""

    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-c")

    with running_consumer(wiring_factory()):
        broker.publish_event(order_confirmed(order=order, event=event))
        wait_until(lambda: rows_for(session_factory, order), description="the first delivery")
        broker.publish_event(order_confirmed(order=order, event=event))
        time.sleep(SETTLE_SECONDS)

    assert len(rows_for(session_factory, order)) == 1
    assert emulator.count(order, "order_confirmation") == 1
    assert processed_event_ids(session_factory) == {event}
    assert broker.depths()[broker.queue] == 0  # both deliveries acknowledged


def test_a_second_event_id_for_the_same_order_produces_no_second_message(
    case_id: str,
    broker,
    emulator,
    session_factory: sessionmaker[Session],
    wiring_factory,
) -> None:
    """Criterion 3: a different eventId for the same order must not deliver twice."""

    order = order_id(f"{case_id}-order")
    first = event_id(f"{case_id}-first")
    second = event_id(f"{case_id}-second")

    with running_consumer(wiring_factory()):
        broker.publish_event(order_confirmed(order=order, event=first))
        wait_until(lambda: rows_for(session_factory, order), description="the first delivery")
        broker.publish_event(order_confirmed(order=order, event=second, occurred_at="2026-09-18T13:00:00Z"))
        wait_until(
            lambda: second in processed_event_ids(session_factory),
            description="the duplicate to be recognised",
        )
        time.sleep(SETTLE_SECONDS)

    rows = rows_for(session_factory, order)
    assert len(rows) == 1
    assert rows[0]["eventId"] == first
    assert emulator.count(order, "order_confirmation") == 1
    assert processed_event_ids(session_factory) == {first, second}
    assert broker.depths()[broker.queue] == 0


def test_the_duplicate_never_reaches_the_provider(
    case_id: str,
    broker,
    capture_provider,
    settings: Settings,
    session_factory: sessionmaker[Session],
    wiring_factory,
) -> None:
    """The second event is stopped by the logical guard, so no HTTP call is made for it."""

    provider = HttpNotificationProvider(
        capture_provider.base_url,
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.provider_max_attempts,
        backoff_seconds=settings.provider_backoff_seconds,
        backoff_max_seconds=settings.provider_backoff_max_seconds,
    )
    order = order_id(f"{case_id}-order")

    with running_consumer(wiring_factory(provider=provider)):
        broker.publish_event(order_confirmed(order=order, event=event_id(f"{case_id}-first")))
        wait_until(
            lambda: capture_provider.count("order_confirmation", order) == 1,
            description="the first delivery",
        )
        broker.publish_event(order_confirmed(order=order, event=event_id(f"{case_id}-second")))
        wait_until(
            lambda: event_id(f"{case_id}-second") in processed_event_ids(session_factory),
            description="the duplicate to be recognised",
        )
        time.sleep(SETTLE_SECONDS)

    assert capture_provider.request_count == 1
    assert capture_provider.count("order_confirmation", order) == 1
    assert len(rows_for(session_factory, order)) == 1


def test_a_duplicated_cancellation_produces_one_message(
    case_id: str,
    broker,
    emulator,
    session_factory: sessionmaker[Session],
    wiring_factory,
) -> None:
    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-x")
    payload = order_cancelled(order=order, event=event, reason="changed my mind")

    with running_consumer(wiring_factory()):
        broker.publish_event(payload)
        wait_until(lambda: rows_for(session_factory, order), description="the cancellation")
        broker.publish_event(payload)
        broker.publish_event(order_cancelled(order=order, event=event_id(f"{case_id}-x2"), reason="changed my mind"))
        wait_until(
            lambda: event_id(f"{case_id}-x2") in processed_event_ids(session_factory),
            description="the duplicate cancellation",
        )
        time.sleep(SETTLE_SECONDS)

    rows = rows_for(session_factory, order)
    assert len(rows) == 1
    assert rows[0]["notificationType"] == "cancellation"
    assert emulator.count(order, "order_cancellation") == 1
    assert broker.depths()[broker.queue] == 0
