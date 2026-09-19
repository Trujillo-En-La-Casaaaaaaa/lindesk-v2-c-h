"""The ``order.confirmed`` handler (component: confirmation-handler, handoff #3).

Thin by design: it fixes the event type and the notification type of this
handler, then delegates to the shared orchestration. It depends on the ports
only, so ``handlers/`` imports neither FastAPI, SQLAlchemy nor the broker client.
"""

from __future__ import annotations

from app.domain.notification import (
    CONFIRMED_EVENT_TYPE,
    TEMPLATE_ORDER_CONFIRMATION,
    InvalidEventError,
    NotificationType,
    OrderEvent,
)
from app.handlers.orchestrator import HandleOutcome, NotificationOrchestrator


class ConfirmationHandler:
    """Deliver the order-confirmation notification of an ``order.confirmed`` event."""

    event_type = CONFIRMED_EVENT_TYPE
    notification_type = NotificationType.CONFIRMATION
    template = TEMPLATE_ORDER_CONFIRMATION

    def __init__(self, orchestrator: NotificationOrchestrator) -> None:
        self._orchestrator = orchestrator

    def handle(self, event: OrderEvent, *, attempts: int = 1) -> HandleOutcome:
        """Deliver the confirmation, exactly once per order."""

        self._require_event_type(event)
        return self._orchestrator.handle(event, attempts=attempts)

    def record_failure(self, event: OrderEvent, *, reason: str, attempts: int) -> HandleOutcome:
        """Record the terminal ``FAILED`` confirmation after the retry budget expired."""

        self._require_event_type(event)
        return self._orchestrator.record_failure(event, reason=reason, attempts=attempts)

    @staticmethod
    def _require_event_type(event: OrderEvent) -> None:
        if event.event_type != ConfirmationHandler.event_type:
            raise InvalidEventError(
                f"the confirmation handler only handles '{ConfirmationHandler.event_type}'",
                details={"eventType": event.event_type},
            )


__all__ = ["ConfirmationHandler"]
