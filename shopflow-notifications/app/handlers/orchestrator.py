"""Notification orchestration and the consumer-dedup-guard (handoff #2, #3/#4).

One order of business for both event types, expressed only in terms of the two
ports (``notification-repository-port`` and ``provider-port``) - the handlers and
this module never import FastAPI, SQLAlchemy or the broker client (criterion 8).

.. rubric:: The delivery algorithm

1. **Deduplication, check one** (``processed_events`` by ``event_id``): a
   redelivered event whose record was already committed is skipped. This is the
   check that catches a crash *after* the commit but *before* the acknowledgement.
2. **Deduplication, check two** (``notifications`` by ``(order_id,
   notification_type)``): a second event carrying a **different** ``eventId`` for
   the same order is skipped, so an order can never receive two confirmations or
   two cancellations. The new event id is then remembered, so its own redeliveries
   short-circuit at check one.
3. **Render and deliver** through the provider port, with the ``Idempotency-Key``
   of contracts.md §7 (``order:{orderId}:{template}``). If a previous attempt
   already reached the provider and only the commit was lost, the provider returns
   the original ``messageId`` and no second message is recorded by the provider.
4. **Commit atomically**: the notification row and the processed-event row in one
   transaction (``commit_notification``). A competing delivery loses the unique
   constraint and is reported as a skip instead of creating a second record.

:meth:`NotificationOrchestrator.record_failure` writes the terminal ``FAILED``
record (handoff #3/#6) and applies exactly the same two guards, so a failure can
never downgrade a record that already reported ``SENT``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from app.domain.notification import (
    NotificationRecord,
    NotificationStatus,
    NotificationType,
    OrderEvent,
    ProviderReceipt,
    new_notification_id,
    utc_now,
)
from app.domain.templates import DEFAULT_RECIPIENT_DOMAIN, build_outbound_message
from app.ports.provider import NotificationProvider
from app.ports.repository import (
    DuplicateEvent,
    DuplicateNotification,
    NotificationRepository,
)

LOGGER = logging.getLogger("shopflow.notifications.orchestrator")

#: What the orchestrator did with one event.
ACTION_DELIVERED = "delivered"
ACTION_SKIPPED = "skipped"
ACTION_FAILED = "failed"

#: Why an event was skipped (never silently: the reason is logged and returned).
SKIP_EVENT_ALREADY_PROCESSED = "event_already_processed"
SKIP_NOTIFICATION_ALREADY_RECORDED = "notification_already_recorded"
SKIP_NOTIFICATION_COMMIT_RACE = "notification_commit_race"


@dataclass(frozen=True)
class HandleOutcome:
    """The result of handling (or failing to handle) one event."""

    action: str
    order_id: str
    event_id: str
    notification_type: NotificationType
    reason: str
    record: NotificationRecord | None = None

    @property
    def delivered(self) -> bool:
        return self.action == ACTION_DELIVERED

    @property
    def skipped(self) -> bool:
        return self.action == ACTION_SKIPPED

    @property
    def failed(self) -> bool:
        return self.action == ACTION_FAILED


def _outcome(
    action: str,
    event: OrderEvent,
    reason: str,
    *,
    record: NotificationRecord | None = None,
) -> HandleOutcome:
    return HandleOutcome(
        action=action,
        order_id=event.order_id,
        event_id=event.event_id,
        notification_type=event.notification_type,
        reason=reason,
        record=record,
    )


class NotificationOrchestrator:
    """Deliver exactly one notification per order and notification type."""

    def __init__(
        self,
        *,
        repository: NotificationRepository,
        provider: NotificationProvider,
        recipient_domain: str = DEFAULT_RECIPIENT_DOMAIN,
        clock: Callable[[], datetime] = utc_now,
        after_send: Callable[[], None] | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._recipient_domain = recipient_domain
        self._clock = clock
        #: Fault-injection seam used by the failure suite to model a crash between
        #: "the provider accepted the message" and "the record is committed".
        #: ``None`` (the production default) means the seam is inert.
        self._after_send = after_send

    # ------------------------------------------------------------------ #
    # the consumer-dedup-guard (handoff #2)
    # ------------------------------------------------------------------ #
    def _dedup_guard(self, event: OrderEvent, now: datetime) -> HandleOutcome | None:
        """Return an outcome when the event must not be delivered again, else ``None``."""

        if self._repository.event_processed(event.event_id):
            return _outcome(ACTION_SKIPPED, event, SKIP_EVENT_ALREADY_PROCESSED)

        existing = self._repository.find_by_order_and_type(event.order_id, event.notification_type)
        if existing is not None:
            self._remember_event(event, now)
            return _outcome(ACTION_SKIPPED, event, SKIP_NOTIFICATION_ALREADY_RECORDED, record=existing)
        return None

    def _remember_event(self, event: OrderEvent, now: datetime) -> None:
        """Remember a skipped duplicate event id, so its own redelivery is a cheap skip."""

        with suppress(DuplicateEvent):  # pragma: no cover - racing duplicate of the same event id
            self._repository.mark_event_processed(event.event_id, now)

    # ------------------------------------------------------------------ #
    # delivery
    # ------------------------------------------------------------------ #
    def handle(self, event: OrderEvent, *, attempts: int = 1) -> HandleOutcome:
        """Deliver the notification of ``event`` exactly once.

        Raises the provider-port error unchanged when the message could not be
        delivered; the caller owns the retry / dead-letter decision.
        """

        notification_type = event.notification_type
        now = self._clock()

        skipped = self._dedup_guard(event, now)
        if skipped is not None:
            LOGGER.info(
                "notification skipped (reason=%s orderId=%s eventId=%s notificationType=%s)",
                skipped.reason,
                event.order_id,
                event.event_id,
                notification_type.value,
            )
            return skipped

        message = build_outbound_message(event, recipient_domain=self._recipient_domain)
        receipt: ProviderReceipt = self._provider.send(message)

        if self._after_send is not None:
            self._after_send()

        record = NotificationRecord(
            notification_id=new_notification_id(),
            order_id=event.order_id,
            notification_type=notification_type,
            event_id=event.event_id,
            status=NotificationStatus.SENT,
            provider_message_id=receipt.message_id,
            attempts=max(1, attempts),
            created_at=now,
            updated_at=now,
        )
        try:
            committed = self._repository.commit_notification(record, event_id=event.event_id, processed_at=now)
        except DuplicateNotification:
            # A competing delivery won the unique constraint of contracts.md §9.
            # Its record is the single logical notification for this order.
            LOGGER.info(
                "notification commit lost the (order_id, notification_type) race "
                "(orderId=%s eventId=%s notificationType=%s)",
                event.order_id,
                event.event_id,
                notification_type.value,
            )
            return _outcome(ACTION_SKIPPED, event, SKIP_NOTIFICATION_COMMIT_RACE)

        LOGGER.info(
            "notification delivered (orderId=%s eventId=%s notificationType=%s template=%s providerMessageId=%s)",
            event.order_id,
            event.event_id,
            notification_type.value,
            message.template,
            committed.provider_message_id,
        )
        return _outcome(ACTION_DELIVERED, event, "delivered", record=committed)

    # ------------------------------------------------------------------ #
    # terminal failure (handoff #3/#6)
    # ------------------------------------------------------------------ #
    def record_failure(self, event: OrderEvent, *, reason: str, attempts: int) -> HandleOutcome:
        """Record the terminal ``FAILED`` notification of an exhausted delivery.

        Applies the same dedup guard as :meth:`handle`, so a record that already
        reported ``SENT`` is never downgraded and a second ``FAILED`` row is never
        written. The caller dead-letters the message afterwards.
        """

        notification_type = event.notification_type
        now = self._clock()

        skipped = self._dedup_guard(event, now)
        if skipped is not None:
            return skipped

        record = NotificationRecord(
            notification_id=new_notification_id(),
            order_id=event.order_id,
            notification_type=notification_type,
            event_id=event.event_id,
            status=NotificationStatus.FAILED,
            provider_message_id=None,
            attempts=max(1, attempts),
            created_at=now,
            updated_at=now,
        )
        try:
            committed = self._repository.commit_notification(record, event_id=event.event_id, processed_at=now)
        except DuplicateNotification:
            return _outcome(ACTION_SKIPPED, event, SKIP_NOTIFICATION_COMMIT_RACE)

        LOGGER.error(
            "notification failed after bounded retries and was dead-lettered "
            "(orderId=%s eventId=%s notificationType=%s attempts=%s reason=%s)",
            event.order_id,
            event.event_id,
            notification_type.value,
            attempts,
            reason,
        )
        return _outcome(ACTION_FAILED, event, reason, record=committed)


__all__ = [
    "ACTION_DELIVERED",
    "ACTION_FAILED",
    "ACTION_SKIPPED",
    "HandleOutcome",
    "NotificationOrchestrator",
    "SKIP_EVENT_ALREADY_PROCESSED",
    "SKIP_NOTIFICATION_ALREADY_RECORDED",
    "SKIP_NOTIFICATION_COMMIT_RACE",
]
