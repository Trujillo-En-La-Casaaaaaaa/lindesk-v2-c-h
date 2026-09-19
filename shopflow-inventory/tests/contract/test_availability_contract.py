"""Contract tests: availability checks (contracts.md §4)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_availability_reports_every_line(client: TestClient, store) -> None:
    response = client.post(
        "/inventory/availability",
        json={"lines": [{"productId": "p-1001", "quantity": 2}, {"productId": "p-1003", "quantity": 11}]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "items": [
            {"productId": "p-1001", "requested": 2, "available": 50, "sufficient": True},
            {"productId": "p-1003", "requested": 11, "available": 10, "sufficient": False},
        ],
    }
    assert store.snapshot() == {entry: quantity for entry, quantity in store.snapshot().items()}
    assert store.movement_keys() == set()


def test_availability_is_true_when_every_line_fits(client: TestClient) -> None:
    response = client.post("/inventory/availability", json={"lines": [{"productId": "p-1004", "quantity": 100}]})

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "items": [{"productId": "p-1004", "requested": 100, "available": 100, "sufficient": True}],
    }


def test_availability_of_a_zero_stock_product_is_insufficient(client: TestClient) -> None:
    body = client.post("/inventory/availability", json={"lines": [{"productId": "p-1005", "quantity": 1}]}).json()

    assert body == {
        "available": False,
        "items": [{"productId": "p-1005", "requested": 1, "available": 0, "sufficient": False}],
    }


def test_availability_of_an_unknown_product_is_not_found(client: TestClient) -> None:
    response = client.post(
        "/inventory/availability",
        json={"lines": [{"productId": "does-not-exist", "quantity": 1}]},
        headers={"X-Correlation-Id": "corr-avail"},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "unknown_product"
    assert body["error"]["correlationId"] == "corr-avail"
    assert body["error"]["details"] == {"productId": "does-not-exist"}


def test_availability_changes_no_stock(client: TestClient, store) -> None:
    before = store.snapshot()

    client.post("/inventory/availability", json={"lines": [{"productId": "p-1001", "quantity": 50}]})

    assert store.snapshot() == before
    assert store.movement_keys() == set()


def test_availability_rejects_malformed_bodies(client: TestClient) -> None:
    for payload in (
        {},
        {"lines": []},
        {"lines": [{"productId": "p-1001"}]},
        {"lines": [{"productId": "p-1001", "quantity": 0}]},
        {"lines": [{"productId": "p-1001", "quantity": -1}]},
        {"lines": [{"productId": "", "quantity": 1}]},
        {"lines": [{"productId": "p-1001", "quantity": "many"}]},
    ):
        response = client.post("/inventory/availability", json=payload)
        assert response.status_code == 400, payload
        assert response.json()["error"]["code"] == "validation_error", payload


def test_availability_rejects_a_non_json_body(client: TestClient) -> None:
    response = client.post(
        "/inventory/availability",
        content=b"not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
