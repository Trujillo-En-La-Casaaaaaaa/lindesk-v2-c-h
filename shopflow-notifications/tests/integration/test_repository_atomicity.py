"""Integration tests of the PostgreSQL repository adapter against the real store.

These tests prove the properties the handlers rely on but cannot provide themselves:
the atomicity of "record the notification and mark the event processed", the
rollback of a failed write, and the race safety of the unique constraint when two
deliveries arrive at the same moment.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.postgres_repository import PostgresNotificationRepository
from app.domain.notification import (
    NotificationRecord,
    NotificationStatus,
    NotificationType,
    utc_now,
)
from app.ports.repository import DuplicateNotification, RepositoryError
from tests.support.db import notification_rows, processed_event_ids
from tests.support.events import event_id


@pytest.fixture()
def repository(session_factory: sessionmaker[Session]) -> PostgresNotificationRepository:
    return PostgresNotificationRepository(session_factory)


def make_record(
    *,
    order: str,
    notification_type: NotificationType = NotificationType.CONFIRMATION,
    status: NotificationStatus = NotificationStatus.SENT,
    provider_message_id: str | None = "msg-0123456789abcdef",
    attempts: int = 1,
    created_at: datetime | None = None,
) -> NotificationRecord:
    now = created_at or utc_now()
    return NotificationRecord(
        notification_id=str(uuid.uuid4()),
        order_id=order,
        notification_type=notification_type,
        event_id=event_id("unused"),
        status=status,
        provider_message_id=provider_message_id,
        attempts=attempts,
        created_at=now,
        updated_at=now,
    )


def test_a_commit_records_both_the_notification_and_the_processed_event(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = f"order-{case_id}"
    event = event_id(f"{case_id}-c")
    record = make_record(order=order)

    committed = repository.commit_notification(record, event_id=event, processed_at=utc_now())

    assert committed.notification_id == record.notification_id
    rows = notification_rows(session_factory, order_id=order)
    assert len(rows) == 1
    assert rows[0]["status"] == "SENT"
    assert rows[0]["providerMessageId"] == "msg-0123456789abcdef"
    assert rows[0]["attempts"] == 1
    assert event in processed_event_ids(session_factory)
    assert repository.event_processed(event) is True


def test_a_second_logical_notification_is_refused_by_the_store(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = f"order-{case_id}"
    first_event = event_id(f"{case_id}-first")
    second_event = event_id(f"{case_id}-second")

    repository.commit_notification(make_record(order=order), event_id=first_event, processed_at=utc_now())

    with pytest.raises(DuplicateNotification) as failure:
        repository.commit_notification(
            make_record(order=order, provider_message_id="msg-ffffffffffffffff"),
            event_id=second_event,
            processed_at=utc_now(),
        )

    assert failure.value.order_id == order
    rows = notification_rows(session_factory, order_id=order)
    assert len(rows) == 1
    assert rows[0]["providerMessageId"] == "msg-0123456789abcdef"  # the first one survived
    # the skipped event is remembered, so its own redelivery is a cheap skip
    assert second_event in processed_event_ids(session_factory)


def test_a_cancellation_and_a_confirmation_coexist_for_one_order(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = f"order-{case_id}"

    repository.commit_notification(
        make_record(order=order, notification_type=NotificationType.CONFIRMATION),
        event_id=event_id(f"{case_id}-c"),
        processed_at=utc_now(),
    )
    repository.commit_notification(
        make_record(order=order, notification_type=NotificationType.CANCELLATION),
        event_id=event_id(f"{case_id}-x"),
        processed_at=utc_now(),
    )

    rows = notification_rows(session_factory, order_id=order)
    assert [row["notificationType"] for row in rows] == ["confirmation", "cancellation"]


def test_a_failed_write_rolls_the_whole_transaction_back(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    """If the notification insert is rejected, the processed event must not be recorded."""

    order = f"order-{case_id}"
    event = event_id(f"{case_id}-rolled-back")
    # ``notification_type`` is constrained by contracts.md §9 to the two types; a bug
    # that stored the template name instead must not leave a half-written transaction.
    invalid = replace(make_record(order=order), notification_type="order_confirmation")

    with pytest.raises(RepositoryError) as failure:
        repository.commit_notification(invalid, event_id=event, processed_at=utc_now())

    assert isinstance(failure.value.__cause__, IntegrityError)
    assert notification_rows(session_factory, order_id=order) == []
    assert event not in processed_event_ids(session_factory)
    assert repository.event_processed(event) is False


def test_an_invalid_status_is_refused_by_the_store(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = f"order-{case_id}"
    invalid = replace(make_record(order=order), status="DELIVERED")

    with pytest.raises(RepositoryError) as failure:
        repository.commit_notification(invalid, event_id=event_id(f"{case_id}-bad"), processed_at=utc_now())

    assert isinstance(failure.value.__cause__, IntegrityError)
    assert notification_rows(session_factory, order_id=order) == []


def test_competing_commits_leave_exactly_one_row(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    """Two deliveries of the same logical notification at the same moment."""

    order = f"order-{case_id}"
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        try:
            repository.commit_notification(
                make_record(order=order, provider_message_id=f"msg-{index:016d}"),
                event_id=event_id(f"{case_id}-compete-{index}"),
                processed_at=utc_now(),
            )
            result = "committed"
        except DuplicateNotification:
            result = "duplicate"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == workers
    assert outcomes.count("committed") == 1
    assert outcomes.count("duplicate") == workers - 1
    assert len(notification_rows(session_factory, order_id=order)) == 1


def test_marking_an_event_processed_is_idempotent(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    event = event_id(f"{case_id}-mark")

    repository.mark_event_processed(event, utc_now())
    repository.mark_event_processed(event, utc_now())

    assert processed_event_ids(session_factory) == {event}


def test_the_diagnostic_read_is_ordered_and_filtered(
    repository: PostgresNotificationRepository,
    session_factory: sessionmaker[Session],
    case_id: str,
) -> None:
    order = f"order-{case_id}"
    other = f"order-{case_id}-other"
    earlier = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    later = datetime(2026, 9, 18, 12, 5, 0, tzinfo=UTC)

    repository.commit_notification(
        make_record(order=order, notification_type=NotificationType.CANCELLATION, created_at=later),
        event_id=event_id(f"{case_id}-late"),
        processed_at=later,
    )
    repository.commit_notification(
        make_record(order=order, notification_type=NotificationType.CONFIRMATION, created_at=earlier),
        event_id=event_id(f"{case_id}-early"),
        processed_at=earlier,
    )
    repository.commit_notification(
        make_record(order=other),
        event_id=event_id(f"{case_id}-other"),
        processed_at=earlier,
    )

    records = repository.list_by_order(order)
    assert [record.notification_type for record in records] == [
        NotificationType.CONFIRMATION,
        NotificationType.CANCELLATION,
    ]
    assert repository.list_by_order(f"order-{case_id}-unknown") == []
    assert [record.order_id for record in records] == [order, order]


def test_a_duplicate_event_is_visible_before_the_commit(
    repository: PostgresNotificationRepository,
    case_id: str,
) -> None:
    event = event_id(f"{case_id}-visible")
    assert repository.event_processed(event) is False
    assert repository.find_by_order_and_type(f"order-{case_id}", NotificationType.CONFIRMATION) is None
