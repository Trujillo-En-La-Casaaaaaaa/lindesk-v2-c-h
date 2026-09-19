"""Integration tests of the end-to-end delivery path.

Acceptance criteria 1, 4 and 7, exercised through the real broker, the real store
and the real provider emulator: one event on the wire, one recorded notification,
one emulator message - and a second event for the same order must not add another.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.provider_http_client import deterministic_message_id
from app.config import Settings
from tests.support.db import notification_rows, processed_event_ids
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id
from tests.support.runtimes import wait_until

#: Window in which a wrongly duplicated delivery would become visible.
SETTLE_SECONDS = 0.6


def rows_for(session_factory: sessionmaker[Session], order: str) -> list[dict[str, object]]:
    return notification_rows(session_factory, order_id=order)


def test_one_order_confirmed_event_delivers_exactly_one_confirmation(
    client_factory,
    broker,
    emulator,
    settings: Settings,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-confirmed")

    with client_factory():
        broker.publish_event(order_confirmed(order=order, event=event))
        wait_until(lambda: rows_for(session_factory, order), description="the confirmation record")
        time.sleep(SETTLE_SECONDS)

    rows = rows_for(session_factory, order)
    assert len(rows) == 1, f"expected exactly one notification row, got {rows}"
    row = rows[0]
    assert row["notificationType"] == "confirmation"
    assert row["status"] == "SENT"
    assert row["eventId"] == event
    assert row["attempts"] == 1

    assert emulator.count(order) == 1
    assert emulator.templates(order) == ["order_confirmation"]
    expected_message_id = deterministic_message_id("order_confirmation", order)
    assert emulator.message_ids(order) == [expected_message_id]
    assert row["providerMessageId"] == expected_message_id
    assert emulator.messages(order)[0]["to"] == "cust-smoke@notifications.shopflow.local"

    assert processed_event_ids(session_factory) == {event}
    assert broker.depths()[settings.broker_queue] == 0  # delivered and acknowledged


def test_one_order_cancelled_event_delivers_exactly_one_cancellation(
    client_factory,
    broker,
    emulator,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = order_id(f"{case_id}-order")
    event = event_id(f"{case_id}-cancelled")
    reason = "customer changed their mind"

    with client_factory():
        broker.publish_event(order_cancelled(order=order, event=event, reason=reason))
        wait_until(lambda: rows_for(session_factory, order), description="the cancellation record")
        time.sleep(SETTLE_SECONDS)

    rows = rows_for(session_factory, order)
    assert len(rows) == 1
    assert rows[0]["notificationType"] == "cancellation"
    assert rows[0]["status"] == "SENT"
    assert emulator.count(order) == 1
    assert emulator.templates(order) == ["order_cancellation"]
    assert emulator.message_ids(order) == [deterministic_message_id("order_cancellation", order)]


def test_an_order_gets_one_confirmation_and_one_cancellation(
    client_factory,
    broker,
    emulator,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = order_id(f"{case_id}-order")

    with client_factory():
        broker.publish_event(order_confirmed(order=order, event=event_id(f"{case_id}-c")))
        wait_until(lambda: rows_for(session_factory, order), description="the confirmation")
        broker.publish_event(order_cancelled(order=order, event=event_id(f"{case_id}-x"), reason="late"))
        wait_until(lambda: len(rows_for(session_factory, order)) == 2, description="the cancellation")
        time.sleep(SETTLE_SECONDS)

    rows = sorted(rows_for(session_factory, order), key=lambda row: str(row["notificationType"]))
    assert [row["notificationType"] for row in rows] == ["cancellation", "confirmation"]
    assert {row["status"] for row in rows} == {"SENT"}
    assert emulator.count(order) == 2
    assert sorted(emulator.templates(order)) == ["order_cancellation", "order_confirmation"]


def test_the_delivered_body_carries_the_order_content(
    client_factory,
    capture_provider,
    broker,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    """The provider payload is asserted against a real HTTP provider double.

    The emulator only exposes ``messageId``/``orderId``/``template``/``to``/
    ``receivedAt``, so the *content* of the message is proven here.
    """

    order = order_id(f"{case_id}-order")
    with client_factory(provider_base_url=capture_provider.base_url):
        broker.publish_event(order_confirmed(order=order, event=event_id(f"{case_id}-content")))
        wait_until(
            lambda: capture_provider.count("order_confirmation", order) == 1,
            description="the captured confirmation",
        )

    message = capture_provider.messages[0]
    body = message["body"]
    assert set(body) == {"channel", "to", "template", "orderId", "payload"}
    assert body["channel"] == "email"
    assert body["template"] == "order_confirmation"
    assert body["orderId"] == order
    assert message["idempotencyKey"] == f"order:{order}:order_confirmation"
    assert message["correlationId"] == "corr-smoke"
    payload = body["payload"]
    assert payload["orderId"] == order
    assert payload["total"] == 72.47
    assert [line["productId"] for line in payload["lines"]] == ["p-1001", "p-1002"]
    assert payload["lines"][0]["lineTotal"] == 59.97
    assert "72.47" in payload["text"]

    row = rows_for(session_factory, order)[0]
    assert row["providerMessageId"] == message["messageId"]


def test_the_cancellation_body_carries_the_event_reason(
    client_factory,
    capture_provider,
    broker,
    case_id: str,
) -> None:
    order = order_id(f"{case_id}-order")
    reason = "payment failed and the customer asked to stop"

    with client_factory(provider_base_url=capture_provider.base_url):
        broker.publish_event(order_cancelled(order=order, event=event_id(f"{case_id}-reason"), reason=reason))
        wait_until(
            lambda: capture_provider.count("order_cancellation", order) == 1,
            description="the captured cancellation",
        )

    message = capture_provider.messages[0]
    payload = message["body"]["payload"]
    assert message["body"]["template"] == "order_cancellation"
    assert message["idempotencyKey"] == f"order:{order}:order_cancellation"
    assert payload["reason"] == reason
    assert reason in payload["text"]
    assert capture_provider.count("order_cancellation") == 1
    assert capture_provider.request_count == 1  # exactly one call per notification
