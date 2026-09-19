"""The notification-repository-port.

This repository is the **only** writable owner of the notification data
(contracts.md §9 notifications store); no other service writes here and this
service writes nowhere else. The port expresses the two guarantees the
deduplication rules need, without leaking SQLAlchemy into the handlers:

* :meth:`NotificationRepository.commit_notification` records the notification row
  **and** the processed-event row in one transaction, so a crash can never leave
  the two out of step;
* the store itself enforces ``unique (order_id, notification_type)`` and the
  ``processed_events`` primary key, so two concurrent deliveries of the same
  logical notification can never both win.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.notification import NotificationRecord, NotificationType


class RepositoryError(RuntimeError):
    """The store could not satisfy a request."""


class DuplicateNotification(RepositoryError):
    """``(order_id, notification_type)`` is already recorded.

    Raised by :meth:`NotificationRepository.commit_notification` when the unique
    constraint of contracts.md §9 rejects the insert; the whole write is rolled
    back, so a competing delivery can never create a second logical notification.
    """

    def __init__(self, order_id: str, notification_type: NotificationType) -> None:
        super().__init__(f"a '{_value(notification_type)}' notification already exists for order '{order_id}'")
        self.order_id = order_id
        self.notification_type = notification_type


class DuplicateEvent(RepositoryError):
    """``event_id`` is already recorded in ``processed_events``."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"event '{event_id}' was already processed")
        self.event_id = event_id


def _value(notification_type: NotificationType) -> str:
    return notification_type.value if isinstance(notification_type, NotificationType) else str(notification_type)


@runtime_checkable
class NotificationRepository(Protocol):
    """Storage port for notification records and event-deduplication state."""

    def event_processed(self, event_id: str) -> bool:
        """Return ``True`` when this event id was already handled."""

    def find_by_order_and_type(
        self,
        order_id: str,
        notification_type: NotificationType,
    ) -> NotificationRecord | None:
        """Return the recorded notification for ``(order_id, notification_type)``, if any."""

    def list_by_order(self, order_id: str) -> list[NotificationRecord]:
        """Return every notification recorded for an order, oldest first."""

    def commit_notification(
        self,
        record: NotificationRecord,
        *,
        event_id: str,
        processed_at: datetime,
    ) -> NotificationRecord:
        """Record ``record`` and mark ``event_id`` processed, atomically.

        Raises :class:`DuplicateNotification` when ``(order_id, notification_type)``
        already exists; nothing is written in that case.
        """

    def mark_event_processed(self, event_id: str, processed_at: datetime) -> None:
        """Mark an event handled without recording a new notification (idempotent)."""


__all__ = [
    "DuplicateEvent",
    "DuplicateNotification",
    "NotificationRepository",
    "RepositoryError",
]
