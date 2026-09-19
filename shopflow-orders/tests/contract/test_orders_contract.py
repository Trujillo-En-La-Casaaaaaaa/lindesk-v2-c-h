"""Contract tests of the order write/read routes (contracts.md §5, HANDOFF #2/#5)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.contract.conftest import contract_settings
from tests.support.fakes import FakeInventory, InMemoryOrderRepository

ORDER_VIEW_KEYS = [
    "orderId",
    "status",
    "customerId",
    "createdAt",
    "updatedAt",
    "confirmedAt",
    "shippedAt",
    "cancelledAt",
    "cancellationReason",
    "total",
    "lines",
]


def create_order(client: TestClient, payload: dict, headers: dict | None = None):
    return client.post("/orders", json=payload, headers=headers or {})


def test_create_order_returns_201_with_the_documented_view(client: TestClient) -> None:
    response = create_order(
        client,
        {"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 2}]},
        {"X-Correlation-Id": "corr-1"},
    )

    assert response.status_code == 201
    order = response.json()
    assert list(order) == ORDER_VIEW_KEYS
    uuid.UUID(order["orderId"])
    assert order["status"] == "CONFIRMED"
    assert order["customerId"] == "c-1"
    assert order["confirmedAt"] is not None
    assert order["confirmedAt"].endswith("Z")
    assert order["shippedAt"] is None
    assert order["cancelledAt"] is None
    assert order["cancellationReason"] is None
    assert order["total"] == 39.98
    assert order["lines"] == [
        {
            "productId": "p-1001",
            "sku": "SF-TEE-001",
            "name": "ShopFlow Classic Tee",
            "quantity": 2,
            "unitPrice": 19.99,
            "lineTotal": 39.98,
        }
    ]


def test_create_order_keeps_every_line_in_request_order(client: TestClient) -> None:
    response = create_order(
        client,
        {
            "customerId": "c-1",
            "lines": [
                {"productId": "p-1002", "quantity": 1},
                {"productId": "p-1001", "quantity": 2},
            ],
        },
    )

    assert response.status_code == 201
    order = response.json()
    assert [line["productId"] for line in order["lines"]] == ["p-1002", "p-1001"]
    assert order["total"] == 52.48


@pytest.mark.parametrize(
    "payload",
    [
        {"lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "c-1"},
        {"customerId": "c-1", "lines": []},
        {"customerId": "", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "   ", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 0}]},
        {"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": -1}]},
        {"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 1.5}]},
        {"customerId": "c-1", "lines": [{"productId": "p-1001"}]},
        {"customerId": "c-1", "lines": [{"productId": "", "quantity": 1}]},
    ],
)
def test_malformed_create_requests_are_validation_errors(
    client: TestClient, repository: InMemoryOrderRepository, payload: dict
) -> None:
    response = create_order(client, payload)

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"]
    assert body["error"]["correlationId"] == ""
    assert "details" in body["error"]
    assert repository.orders == {}


def test_blank_customer_id_is_rejected_by_the_domain(client: TestClient, repository: InMemoryOrderRepository) -> None:
    response = create_order(client, {"customerId": "   ", "lines": [{"productId": "p-1001", "quantity": 1}]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert repository.orders == {}


def test_the_correlation_id_is_echoed_in_the_error_envelope(client: TestClient) -> None:
    response = client.post(
        "/orders",
        json={"customerId": "c-1", "lines": [{"productId": "p-1005", "quantity": 1}]},
        headers={"X-Correlation-Id": "corr-42"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["correlationId"] == "corr-42"


def test_insufficient_stock_is_409_with_details_and_creates_no_order(
    client: TestClient, repository: InMemoryOrderRepository, inventory: FakeInventory
) -> None:
    response = create_order(client, {"customerId": "c-1", "lines": [{"productId": "p-1005", "quantity": 1}]})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "insufficient_stock"
    assert error["details"]["productId"] == "p-1005"
    assert error["details"]["requested"] == 1
    assert error["details"]["available"] == 0
    assert error["details"]["items"] == [{"productId": "p-1005", "requested": 1, "available": 0}]
    assert repository.orders == {}
    assert inventory.stock["p-1005"] == 0
    assert [call[0] for call in inventory.calls] == ["availability"]


def test_unreachable_inventory_is_503_and_creates_no_order(
    client: TestClient, repository: InMemoryOrderRepository, inventory: FakeInventory
) -> None:
    inventory.make_unavailable("availability")
    response = create_order(client, {"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 1}]})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "inventory_unavailable"
    assert repository.orders == {}


def test_unknown_product_is_404_unknown_product(client: TestClient, repository: InMemoryOrderRepository) -> None:
    response = create_order(client, {"customerId": "c-1", "lines": [{"productId": "p-9999", "quantity": 1}]})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_product"
    assert repository.orders == {}


def test_get_order_returns_the_view_and_404_for_an_unknown_id(client: TestClient) -> None:
    created = create_order(client, {"customerId": "c-1", "lines": [{"productId": "p-1003", "quantity": 1}]}).json()

    response = client.get(f"/orders/{created['orderId']}", headers={"X-Correlation-Id": "corr-9"})
    assert response.status_code == 200
    assert response.json() == created

    missing = "11111111-2222-3333-4444-555555555555"
    not_found = client.get(f"/orders/{missing}", headers={"X-Correlation-Id": "corr-9"})
    assert not_found.status_code == 404
    body = not_found.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["correlationId"] == "corr-9"
    assert body["error"]["details"]["orderId"] == missing


def test_order_list_returns_items_and_total(client: TestClient) -> None:
    first = create_order(client, {"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 1}]}).json()
    second = create_order(client, {"customerId": "c-1", "lines": [{"productId": "p-1002", "quantity": 1}]}).json()
    create_order(client, {"customerId": "c-2", "lines": [{"productId": "p-1003", "quantity": 1}]})

    response = client.get("/orders", params={"customerId": "c-1"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["orderId"] for item in payload["items"]] == [first["orderId"], second["orderId"]]

    window = client.get("/orders", params={"customerId": "c-1", "limit": 1, "offset": 1})
    assert window.json()["total"] == 2
    assert [item["orderId"] for item in window.json()["items"]] == [second["orderId"]]

    empty = client.get("/orders", params={"customerId": "nobody"})
    assert empty.json() == {"items": [], "total": 0}


@pytest.mark.parametrize("params", [{"limit": -1}, {"offset": -1}, {"limit": "abc"}])
def test_invalid_pagination_is_a_validation_error(client: TestClient, params: dict) -> None:
    response = client.get("/orders", params={"customerId": "c-1", **params})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_a_missing_customer_id_is_a_validation_error(client: TestClient) -> None:
    response = client.get("/orders")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_the_entry_correlation_id_of_the_gateway_is_accepted(client: TestClient) -> None:
    """The gateway sets ``X-Correlation-Id``; the service must not require anything else."""

    response = create_order(
        client,
        {"customerId": "c-1", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"X-Correlation-Id": "gateway-generated-id", "X-ShopFlow-Role": "admin"},
    )
    assert response.status_code == 201


def test_settings_of_the_contract_suite_are_hermetic() -> None:
    settings = contract_settings()
    assert settings.start_workers is False
    assert settings.inventory_base_url == "http://inventory.invalid:8000"
