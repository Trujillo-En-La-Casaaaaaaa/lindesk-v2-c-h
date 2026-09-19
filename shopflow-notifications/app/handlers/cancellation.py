"""The ``order.cancelled`` handler (component: cancellation-handler, handoff #4).

Thin by design: it fixes the event type and the notification type of this
handler, then delegates to the shared orchestration. The cancellation body is
rendered by ``app.domain.templates`` and carries the customer's ``reason``; the
handler only guarantees that a cancellation event is the one it handles.
"""

from __future__ import annotations

from app.domain.notification import (
    CANCELLED_EVENT_TYPE,
    TEMPLATE_ORDER_CANCELLATION,
    InvalidEventError,
    NotificationType,
    OrderEvent,
)
from app.handlers.orchestrator import HandleOutcome, NotificationOrchestrator


class CancellationHandler:
    """Deliver the order-cancellation notification of an ``order.cancelled`` event."""

    event_type = CANCELLED_EVENT_TYPE
    notification_type = NotificationType.CANCELLATION
    template = TEMPLATE_ORDER_CANCELLATION

    def __init__(self, orchestrator: NotificationOrchestrator) -> None:
        self._orchestrator = orchestrator

    def handle(self, event: OrderEvent, *, attempts: int = 1) -> HandleOutcome:
        """Deliver the cancellation, exactly once per order."""

        self._require_event_type(event)
        return self._orchestrator.handle(event, attempts=attempts)

    def record_failure(self, event: OrderEvent, *, reason: str, attempts: int) -> HandleOutcome:
        """Record the terminal ``FAILED`` cancellation after the retry budget expired."""

        self._require_event_type(event)
        return self._orchestrator.record_failure(event, reason=reason, attempts=attempts)

    @staticmethod
    def _require_event_type(event: OrderEvent) -> None:
        if event.event_type != CancellationHandler.event_type:
            raise InvalidEventError(
                f"the cancellation handler only handles '{CancellationHandler.event_type}'",
                details={"eventType": event.event_type},
            )


__all__ = ["CancellationHandler"]
