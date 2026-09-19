"""Event router: event type -> handler (component: event-router, handoff #1).

The routing key of the delivery and the ``eventType`` of the envelope name the
same thing (contracts.md §8), so this is the single place that decides which
handler owns an event. An event type this service does not consume is reported as
:class:`UnsupportedEventError`, which is permanent by nature: the consumer
dead-letters it instead of retrying, so the message is kept for an operator
rather than looping forever.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.domain.notification import (
    SUPPORTED_EVENT_TYPES,
    OrderEvent,
    UnsupportedEventError,
)
from app.handlers.cancellation import CancellationHandler
from app.handlers.confirmation import ConfirmationHandler
from app.handlers.orchestrator import HandleOutcome, NotificationOrchestrator


@runtime_checkable
class EventHandler(Protocol):
    """One event type's handler."""

    event_type: str

    def handle(self, event: OrderEvent, *, attempts: int = 1) -> HandleOutcome:
        """Deliver the notification of ``event``."""

    def record_failure(self, event: OrderEvent, *, reason: str, attempts: int) -> HandleOutcome:
        """Record the terminal ``FAILED`` notification of ``event``."""


class EventRouter:
    """Maps the two consumed event types onto their handlers."""

    def __init__(self, handlers: Mapping[str, EventHandler]) -> None:
        unknown = sorted(set(handlers) - set(SUPPORTED_EVENT_TYPES))
        if unknown:
            raise ValueError(f"no handler may be registered for unconsumed event type(s): {', '.join(unknown)}")
        missing = sorted(set(SUPPORTED_EVENT_TYPES) - set(handlers))
        if missing:
            raise ValueError(f"every consumed event type needs a handler: {', '.join(missing)}")
        self._handlers: dict[str, EventHandler] = dict(handlers)

    @classmethod
    def from_orchestrator(cls, orchestrator: NotificationOrchestrator) -> EventRouter:
        """Build the router of the two handlers this service owns."""

        confirmation = ConfirmationHandler(orchestrator)
        cancellation = CancellationHandler(orchestrator)
        return cls({confirmation.event_type: confirmation, cancellation.event_type: cancellation})

    @property
    def event_types(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def handler_for(self, event_type: str) -> EventHandler:
        try:
            return self._handlers[event_type]
        except KeyError as exc:
            raise UnsupportedEventError(
                f"event type '{event_type}' is not consumed by this service",
                details={"eventType": event_type, "supported": list(SUPPORTED_EVENT_TYPES)},
            ) from exc

    def handle(self, event: OrderEvent, *, attempts: int = 1) -> HandleOutcome:
        return self.handler_for(event.event_type).handle(event, attempts=attempts)

    def record_failure(self, event: OrderEvent, *, reason: str, attempts: int) -> HandleOutcome:
        return self.handler_for(event.event_type).record_failure(event, reason=reason, attempts=attempts)


def describe_handlers(router: EventRouter) -> dict[str, Any]:
    """Small diagnostic helper used by the service description and the tests."""

    return {
        event_type: type(router.handler_for(event_type)).__name__ for event_type in router.event_types
    }


__all__ = ["EventHandler", "EventRouter", "describe_handlers"]
