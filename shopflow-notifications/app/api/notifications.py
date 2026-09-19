"""Diagnostic read of the notification records (component: notification-api, handoff #6).

``GET /notifications?orderId=`` answers
``{ items: [{ orderId, notificationType, status, providerMessageId, createdAt }], total }``
(contracts.md §6) read from the owned store, so the ecosystem smoke test and an
operator can verify exactly-once delivery without touching the database.

The route handler holds no business rule: it validates the parameter, asks the
repository port for the records and maps them onto the documented shape.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from app.domain.notification import InvalidRequestError, NotificationRecord, format_timestamp

router = APIRouter()

ORDER_ID_PARAMETER = "orderId"


def record_to_item(record: NotificationRecord) -> dict[str, Any]:
    """Map one stored notification onto the documented item shape."""

    return {
        "orderId": record.order_id,
        "notificationType": record.notification_type.value,
        "status": record.status.value,
        "providerMessageId": record.provider_message_id,
        "createdAt": format_timestamp(record.created_at),
    }


@router.get("/notifications", tags=["notifications"])
async def list_notifications(
    request: Request,
    orderId: str | None = Query(default=None, description="Order whose notifications are read back."),
) -> dict[str, Any]:
    """Return every notification recorded for one order (oldest first)."""

    if orderId is None or not str(orderId).strip():
        raise InvalidRequestError(
            f"the '{ORDER_ID_PARAMETER}' query parameter is required",
            details={"parameter": ORDER_ID_PARAMETER},
        )
    repository = request.app.state.notification_repository
    records = repository.list_by_order(str(orderId).strip())
    items = [record_to_item(record) for record in records]
    return {"items": items, "total": len(items)}


__all__ = ["ORDER_ID_PARAMETER", "list_notifications", "record_to_item", "router"]
