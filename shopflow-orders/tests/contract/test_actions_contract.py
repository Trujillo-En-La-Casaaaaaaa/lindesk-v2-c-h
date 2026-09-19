"""Contract tests of the order actions (contracts.md §5, HANDOFF #3/#4)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.cancellation import MAX_REASON_LENGTH
from tests.support.fakes import FakeInventory, InMemoryOrderRepository

UNKNOWN_ORDER = "11111111-2222-3333-4444-555555555555"


def confirmed_order(client: TestClient) -> dict:
    response = client.post(
        "/orders",
        json={"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 2}]},
        headers={"X-Correlation-Id": "corr-create"},
    )
    assert response.status_code == 201
    return response.json()


def test_cancelling_a_confirmed_order_returns_the_cancelled_view(client: TestClient) -> None:
    order = confirmed_order(client)

    response = client.post(
        f"/orders/{order['orderId']}/cancel",
        json={"reason": "customer changed their mind"},
        headers={"X-Correlation-Id": "corr-cancel"},
    )

    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancelledAt"] is not None
    assert cancelled["cancelledAt"].endswith("Z")
    assert cancelled["cancellationReason"] == "customer changed their mind"
    assert cancelled["shippedAt"] is None
    assert cancelled["lines"] == order["lines"]
    assert cancelled["total"] == order["total"]


def test_a_ten_character_reason_is_stored_trimmed(client: TestClient) -> None:
    order = confirmed_order(client)
    response = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "  not wanted  "})
    assert response.status_code == 200
    assert response.json()["cancellationReason"] == "not wanted"


def test_a_two_hundred_character_reason_is_accepted(client: TestClient) -> None:
    order = confirmed_order(client)
    reason = "a" * MAX_REASON_LENGTH
    response = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": reason})
    assert response.status_code == 200
    assert response.json()["cancellationReason"] == reason


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reason": None},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "\t\n"},
        {"reason": "a" * (MAX_REASON_LENGTH + 1)},
    ],
)
def test_invalid_reasons_are_400_invalid_reason_and_change_nothing(
    client: TestClient,
    repository: InMemoryOrderRepository,
    payload: dict,
) -> None:
    order = confirmed_order(client)

    response = client.post(
        f"/orders/{order['orderId']}/cancel",
        json=payload,
        headers={"X-Correlation-Id": "corr-bad-reason"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_reason"
    assert body["error"]["correlationId"] == "corr-bad-reason"
    assert body["error"]["details"]["maxLength"] == MAX_REASON_LENGTH
    assert repository.orders[order["orderId"]].status.value == "CONFIRMED"
    assert repository.event_types == ["order.confirmed"]  # no cancellation event


def test_a_non_string_reason_is_a_validation_error(client: TestClient, repository: InMemoryOrderRepository) -> None:
    order = confirmed_order(client)
    response = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": 123})
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"validation_error", "invalid_reason"}
    assert repository.orders[order["orderId"]].status.value == "CONFIRMED"


def test_cancelling_an_unknown_order_is_404(client: TestClient) -> None:
    response = client.post(f"/orders/{UNKNOWN_ORDER}/cancel", json={"reason": "changed mind"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_bad_reason_wins_over_an_unknown_order(client: TestClient) -> None:
    response = client.post(f"/orders/{UNKNOWN_ORDER}/cancel", json={"reason": "  "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reason"


def test_a_shipped_order_cannot_be_cancelled(client: TestClient, repository: InMemoryOrderRepository) -> None:
    order = confirmed_order(client)
    shipped = client.post(f"/orders/{order['orderId']}/ship")
    assert shipped.status_code == 200

    response = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "too late"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "order_shipped"
    assert body["error"]["details"]["status"] == "SHIPPED"
    assert repository.orders[order["orderId"]].status.value == "SHIPPED"
    assert repository.event_types == ["order.confirmed"]  # no cancellation event


def test_a_failed_order_is_not_cancellable(
    client: TestClient, repository: InMemoryOrderRepository, inventory: FakeInventory
) -> None:
    inventory.insufficient_at_reserve = True
    failed = client.post(
        "/orders",
        json={"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 2}]},
    )
    assert failed.status_code == 409
    failed_id = next(
        order_id for order_id, order in repository.orders.items() if order.status.value == "FAILED"
    )

    response = client.post(f"/orders/{failed_id}/cancel", json={"reason": "changed mind"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "order_not_cancellable"
    assert body["error"]["details"]["status"] == "FAILED"
    assert repository.orders[failed_id].status.value == "FAILED"
    assert repository.event_types == []  # a FAILED order publishes nothing


def test_a_repeated_cancellation_returns_the_stored_facts(client: TestClient) -> None:
    order = confirmed_order(client)
    first = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "not wanted"}).json()

    second = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "a different reason"})

    assert second.status_code == 200
    repeated = second.json()
    assert repeated["status"] == "CANCELLED"
    assert repeated["cancelledAt"] == first["cancelledAt"]
    assert repeated["cancellationReason"] == "not wanted"


def test_cancelling_a_created_order_is_409_order_not_cancellable(
    client: TestClient, repository: InMemoryOrderRepository, inventory: FakeInventory
) -> None:
    inventory.make_unavailable("reserve")
    created = client.post(
        "/orders",
        json={"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 2}]},
    )
    assert created.status_code == 503
    order_id = next(iter(repository.orders))

    response = client.post(f"/orders/{order_id}/cancel", json={"reason": "changed mind"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "order_not_cancellable"
    assert body["error"]["details"]["status"] == "CREATED"
    assert repository.orders[order_id].status.value == "CREATED"


def test_cancelling_while_the_release_fails_still_answers_200(
    client: TestClient, repository: InMemoryOrderRepository, inventory: FakeInventory
) -> None:
    order = confirmed_order(client)
    inventory.make_unavailable("release")

    response = client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert repository.orders[order["orderId"]].inventory_release_state.value == "PENDING"


def test_shipping_a_confirmed_order_returns_the_shipped_view(client: TestClient) -> None:
    order = confirmed_order(client)

    response = client.post(f"/orders/{order['orderId']}/ship", headers={"X-Correlation-Id": "corr-ship"})

    assert response.status_code == 200
    shipped = response.json()
    assert shipped["status"] == "SHIPPED"
    assert shipped["shippedAt"] is not None
    assert shipped["shippedAt"].endswith("Z")
    assert shipped["cancelledAt"] is None


def test_shipping_an_unknown_order_is_404(client: TestClient) -> None:
    response = client.post(f"/orders/{UNKNOWN_ORDER}/ship")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_shipping_a_cancelled_order_is_409_invalid_status_transition(
    client: TestClient, repository: InMemoryOrderRepository
) -> None:
    order = confirmed_order(client)
    client.post(f"/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})

    response = client.post(f"/orders/{order['orderId']}/ship")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "invalid_status_transition"
    assert body["error"]["details"]["status"] == "CANCELLED"
    assert body["error"]["details"]["targetStatus"] == "SHIPPED"
    assert repository.orders[order["orderId"]].status.value == "CANCELLED"


def test_shipping_a_shipped_order_is_409(client: TestClient) -> None:
    order = confirmed_order(client)
    client.post(f"/orders/{order['orderId']}/ship")
    response = client.post(f"/orders/{order['orderId']}/ship")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_status_transition"
