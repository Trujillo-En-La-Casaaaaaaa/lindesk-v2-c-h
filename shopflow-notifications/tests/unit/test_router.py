"""Unit tests of the event router (component: event-router, handoff #1)."""

from __future__ import annotations

import pytest

from app.consumer.router import EventRouter, describe_handlers
from app.domain.notification import UnsupportedEventError, parse_order_event
from app.handlers.cancellation import CancellationHandler
from app.handlers.confirmation import ConfirmationHandler
from app.handlers.orchestrator import NotificationOrchestrator
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id
from tests.support.fakes import FakeNotificationRepository, RecordingProvider


def build_router() -> tuple[EventRouter, RecordingProvider, FakeNotificationRepository]:
    repository = FakeNotificationRepository()
    provider = RecordingProvider()
    orchestrator = NotificationOrchestrator(repository=repository, provider=provider)
    return EventRouter.from_orchestrator(orchestrator), provider, repository


def test_the_router_owns_exactly_the_two_consumed_event_types() -> None:
    router, _, _ = build_router()

    assert set(router.event_types) == {"order.confirmed", "order.cancelled"}
    assert isinstance(router.handler_for("order.confirmed"), ConfirmationHandler)
    assert isinstance(router.handler_for("order.cancelled"), CancellationHandler)
    assert describe_handlers(router) == {
        "order.confirmed": "ConfirmationHandler",
        "order.cancelled": "CancellationHandler",
    }


def test_an_unconsumed_event_type_is_reported_as_unsupported() -> None:
    router, _, _ = build_router()

    with pytest.raises(UnsupportedEventError):
        router.handler_for("order.shipped")


def test_registration_requires_every_consumed_event_type() -> None:
    with pytest.raises(ValueError):
        EventRouter({})


def test_registration_rejects_an_unconsumed_event_type() -> None:
    orchestrator = NotificationOrchestrator(
        repository=FakeNotificationRepository(),
        provider=RecordingProvider(),
    )
    confirmation = ConfirmationHandler(orchestrator)

    with pytest.raises(ValueError):
        EventRouter({confirmation.event_type: confirmation, "order.shipped": confirmation})


def test_handle_routes_the_event_to_its_handler() -> None:
    router, provider, repository = build_router()
    order = order_id("router")

    confirmed = router.handle(parse_order_event(order_confirmed(order=order, event=event_id("router-c"))))
    cancelled = router.handle(
        parse_order_event(order_cancelled(order=order, event=event_id("router-x"), reason="too late"))
    )

    assert confirmed.delivered is True
    assert cancelled.delivered is True
    assert provider.templates(order_id=order) == ["order_confirmation", "order_cancellation"]
    assert len(repository.records) == 2


def test_record_failure_routes_to_the_same_handler() -> None:
    router, _, repository = build_router()
    order = order_id("router-failure")

    outcome = router.record_failure(
        parse_order_event(order_confirmed(order=order, event=event_id("router-failure-event"))),
        reason="ProviderUnavailable: boom",
        attempts=2,
    )

    assert outcome.failed is True
    record = repository.record_for(order, "confirmation")
    assert record is not None
    assert record.status.value == "FAILED"
