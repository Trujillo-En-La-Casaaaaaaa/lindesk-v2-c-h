"""Unit tests of the handlers, the consumer-dedup-guard and the terminal failure path."""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.notification import (
    InvalidEventError,
    NotificationStatus,
    NotificationType,
    parse_order_event,
)
from app.handlers.cancellation import CancellationHandler
from app.handlers.confirmation import ConfirmationHandler
from app.handlers.orchestrator import (
    ACTION_DELIVERED,
    ACTION_FAILED,
    ACTION_SKIPPED,
    SKIP_EVENT_ALREADY_PROCESSED,
    SKIP_NOTIFICATION_ALREADY_RECORDED,
    SKIP_NOTIFICATION_COMMIT_RACE,
    NotificationOrchestrator,
)
from app.ports.provider import ProviderRejected, ProviderUnavailable
from app.ports.repository import DuplicateNotification
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id
from tests.support.fakes import FakeNotificationRepository, RecordingProvider

ORDER = order_id("handlers")
REASON = "customer changed their mind"


def confirmed(seed: str = "handlers-confirmed", order: str = ORDER):
    return parse_order_event(order_confirmed(order=order, event=event_id(seed)))


def cancelled(seed: str = "handlers-cancelled", order: str = ORDER):
    return parse_order_event(order_cancelled(order=order, event=event_id(seed), reason=REASON))


class RacingRepository(FakeNotificationRepository):
    """A store that reports the unique constraint as lost by a competing delivery."""

    def commit_notification(self, record: Any, *, event_id: str, processed_at: Any) -> Any:
        raise DuplicateNotification(record.order_id, record.notification_type)


def test_confirmation_is_delivered_once_and_recorded_as_sent() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    outcome = orchestrator.handle(confirmed(), attempts=2)

    assert outcome.action == ACTION_DELIVERED
    assert provider.templates(order_id=ORDER) == ["order_confirmation"]
    assert provider.messages[0].idempotency_key == f"order:{ORDER}:order_confirmation"
    record = repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.status is NotificationStatus.SENT
    assert record.attempts == 2
    assert record.provider_message_id is not None
    assert record.event_id == event_id("handlers-confirmed")
    assert event_id("handlers-confirmed") in repository.processed_event_ids


def test_the_recorded_provider_message_id_is_the_provider_answer() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    orchestrator.handle(confirmed())

    record = repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.provider_message_id is not None
    assert record.provider_message_id.startswith("msg-")


def test_a_redelivered_event_is_skipped_without_a_second_delivery() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)
    event = confirmed()

    first = orchestrator.handle(event)
    second = orchestrator.handle(event)

    assert first.delivered is True
    assert second.action == ACTION_SKIPPED
    assert second.reason == SKIP_EVENT_ALREADY_PROCESSED
    assert provider.count() == 1
    assert len(repository.records) == 1


def test_a_second_event_for_the_same_order_is_skipped_even_though_the_event_id_differs() -> None:
    """Handoff #2 check two: ``(order_id, notification_type)`` is the logical guard."""

    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    first = orchestrator.handle(confirmed("handlers-first"))
    second = orchestrator.handle(confirmed("handlers-second"))

    assert first.delivered is True
    assert second.action == ACTION_SKIPPED
    assert second.reason == SKIP_NOTIFICATION_ALREADY_RECORDED
    assert provider.count() == 1
    assert len(repository.records) == 1
    # the skipped event is remembered, so its own redelivery is a cheap skip too
    assert event_id("handlers-second") in repository.processed_event_ids


def test_a_seeded_record_skips_the_delivery_without_calling_the_provider() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    repository.seed(
        order_id=ORDER,
        notification_type=NotificationType.CONFIRMATION,
        event_id=event_id("handlers-earlier"),
    )
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    outcome = orchestrator.handle(confirmed())

    assert outcome.action == ACTION_SKIPPED
    assert provider.count() == 0
    assert len(repository.records) == 1


def test_cancellation_is_delivered_with_its_reason() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    outcome = orchestrator.handle(cancelled())

    assert outcome.delivered is True
    assert provider.templates(order_id=ORDER) == ["order_cancellation"]
    message = provider.messages[0]
    assert message.payload["reason"] == REASON
    assert REASON in message.payload["text"]
    assert repository.record_for(ORDER, "cancellation") is not None


def test_an_order_receives_one_confirmation_and_one_cancellation() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    orchestrator.handle(confirmed())
    orchestrator.handle(cancelled())

    assert sorted(provider.templates(order_id=ORDER)) == ["order_cancellation", "order_confirmation"]
    assert len(repository.records) == 2
    assert {record.notification_type for record in repository.records} == {
        NotificationType.CONFIRMATION,
        NotificationType.CANCELLATION,
    }


def test_a_provider_outage_leaves_no_record() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider(failures=99)
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    with pytest.raises(ProviderUnavailable):
        orchestrator.handle(confirmed())

    assert repository.records == []
    assert repository.processed_event_ids == set()


def test_a_permanent_rejection_is_reported_unchanged() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider(
        failures=99,
        error=ProviderRejected("provider rejected the message", status_code=400),
    )
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    with pytest.raises(ProviderRejected):
        orchestrator.handle(confirmed())

    assert repository.records == []


def test_record_failure_writes_the_terminal_failed_record() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    outcome = orchestrator.record_failure(confirmed(), reason="ProviderUnavailable: boom", attempts=3)

    assert outcome.action == ACTION_FAILED
    record = repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.status is NotificationStatus.FAILED
    assert record.provider_message_id is None
    assert record.attempts == 3
    assert record.event_id == event_id("handlers-confirmed")
    assert event_id("handlers-confirmed") in repository.processed_event_ids
    assert provider.count() == 0


def test_record_failure_never_downgrades_a_delivered_notification() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    orchestrator.handle(confirmed())
    outcome = orchestrator.record_failure(confirmed(), reason="boom", attempts=3)

    assert outcome.action == ACTION_SKIPPED
    record = repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.status is NotificationStatus.SENT
    assert len(repository.records) == 1


def test_a_lost_commit_race_is_reported_as_a_skip() -> None:
    repository = RacingRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    outcome = orchestrator.handle(confirmed())

    assert outcome.action == ACTION_SKIPPED
    assert outcome.reason == SKIP_NOTIFICATION_COMMIT_RACE
    assert repository.records == []


def test_the_after_send_seam_runs_after_the_delivery_and_before_the_commit() -> None:
    """The crash window of handoff #5: delivered by the provider, not yet committed."""

    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    observations: dict[str, Any] = {}

    def after_send() -> None:
        observations["provider_calls"] = provider.count()
        observations["committed_rows"] = len(repository.records)

    orchestrator = NotificationOrchestrator(
        repository=repository,
        provider=provider,
        after_send=after_send,
    )

    orchestrator.handle(confirmed())

    assert observations == {"provider_calls": 1, "committed_rows": 0}
    assert len(repository.records) == 1  # committed afterwards


def test_a_crash_in_the_after_send_seam_leaves_the_store_untouched() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()

    def crash() -> None:
        raise RuntimeError("simulated crash after the provider accepted the message")

    orchestrator = NotificationOrchestrator(repository=repository, provider=provider, after_send=crash)

    with pytest.raises(RuntimeError):
        orchestrator.handle(confirmed())

    assert provider.count() == 1
    assert repository.records == []
    assert repository.processed_event_ids == set()


def test_attempts_are_recorded_from_the_delivery_budget() -> None:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)

    orchestrator.handle(confirmed(), attempts=4)

    record = repository.record_for(ORDER, "confirmation")
    assert record is not None
    assert record.attempts == 4


def test_handlers_only_accept_their_own_event_type() -> None:
    orchestrator = NotificationOrchestrator(
        repository=FakeNotificationRepository(),
        provider=RecordingProvider(),
    )
    confirmation = ConfirmationHandler(orchestrator)
    cancellation = CancellationHandler(orchestrator)

    assert confirmation.event_type == "order.confirmed"
    assert cancellation.event_type == "order.cancelled"
    assert confirmation.handle(confirmed()).delivered is True
    with pytest.raises(InvalidEventError):
        cancellation.handle(confirmed())
    with pytest.raises(InvalidEventError):
        confirmation.record_failure(cancelled(), reason="boom", attempts=1)


def test_handler_metadata_matches_the_contract() -> None:
    orchestrator = NotificationOrchestrator(
        repository=FakeNotificationRepository(),
        provider=RecordingProvider(),
    )

    assert ConfirmationHandler(orchestrator).notification_type is NotificationType.CONFIRMATION
    assert ConfirmationHandler(orchestrator).template == "order_confirmation"
    assert CancellationHandler(orchestrator).notification_type is NotificationType.CANCELLATION
    assert CancellationHandler(orchestrator).template == "order_cancellation"
