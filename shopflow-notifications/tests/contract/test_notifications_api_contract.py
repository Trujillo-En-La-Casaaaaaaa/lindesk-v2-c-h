"""Contract tests of ``GET /notifications?orderId=`` (contracts.md §6, handoff #6).

The response shape is asserted field by field, including the order of the items and
the error envelope of contracts.md §1 for a missing parameter.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.domain.notification import NotificationType
from tests.support.events import event_id, order_id
from tests.support.fakes import FakeNotificationRepository

ORDER = order_id("contract-api")
OTHER_ORDER = order_id("contract-api-other")

ITEM_FIELDS = {"orderId", "notificationType", "status", "providerMessageId", "createdAt"}


def test_notifications_of_an_unknown_order_are_an_empty_list(ready_client: TestClient) -> None:
    response = ready_client.get("/notifications", params={"orderId": ORDER})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_the_item_shape_is_the_documented_one(
    ready_client: TestClient,
    repository: FakeNotificationRepository,
) -> None:
    repository.seed(
        order_id=ORDER,
        notification_type=NotificationType.CONFIRMATION,
        event_id=event_id("contract-api-c"),
        provider_message_id="msg-aaaaaaaaaaaaaaaa",
    )
    repository.seed(
        order_id=ORDER,
        notification_type=NotificationType.CANCELLATION,
        event_id=event_id("contract-api-x"),
        provider_message_id="msg-bbbbbbbbbbbbbbbb",
    )
    repository.seed(
        order_id=OTHER_ORDER,
        notification_type=NotificationType.CONFIRMATION,
        event_id=event_id("contract-api-other"),
    )

    response = ready_client.get("/notifications", params={"orderId": ORDER})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert set(item) == ITEM_FIELDS
    by_type = {item["notificationType"]: item for item in body["items"]}
    assert by_type["confirmation"] == {
        "orderId": ORDER,
        "notificationType": "confirmation",
        "status": "SENT",
        "providerMessageId": "msg-aaaaaaaaaaaaaaaa",
        "createdAt": by_type["confirmation"]["createdAt"],
    }
    assert by_type["cancellation"]["providerMessageId"] == "msg-bbbbbbbbbbbbbbbb"
    assert by_type["confirmation"]["createdAt"].endswith("Z")


def test_a_missing_order_id_uses_the_shared_error_envelope(ready_client: TestClient) -> None:
    response = ready_client.get("/notifications")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["correlationId"] == ""
    assert "orderId" in body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)


def test_a_blank_order_id_is_rejected(ready_client: TestClient) -> None:
    response = ready_client.get("/notifications", params={"orderId": "   "})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_the_correlation_id_is_echoed_in_the_error_envelope(ready_client: TestClient) -> None:
    response = ready_client.get("/notifications", headers={"X-Correlation-Id": "corr-42"})

    assert response.status_code == 400
    assert response.json()["error"]["correlationId"] == "corr-42"
    assert response.headers["X-Correlation-Id"] == "corr-42"


def test_a_failed_notification_reports_a_null_provider_message_id(
    ready_client: TestClient,
    repository: FakeNotificationRepository,
) -> None:
    repository.seed(
        order_id=ORDER,
        notification_type=NotificationType.CONFIRMATION,
        event_id=event_id("contract-api-failed"),
        status="FAILED",
        provider_message_id=None,
        attempts=3,
    )

    item = ready_client.get("/notifications", params={"orderId": ORDER}).json()["items"][0]

    assert item["status"] == "FAILED"
    assert item["providerMessageId"] is None


def test_an_unknown_route_uses_the_shared_error_envelope(ready_client: TestClient) -> None:
    response = ready_client.get("/notification")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
