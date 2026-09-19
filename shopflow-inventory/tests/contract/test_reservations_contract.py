"""Contract tests: stock reservations (contracts.md §4, handoff acceptance 2 and 6)."""

from __future__ import annotations

from fastapi.testclient import TestClient

RESERVE = "/inventory/reservations"
_UNSET = object()


def reserve(
    client: TestClient,
    *,
    order_id: str = "o-1",
    lines: list[dict] | None = None,
    key: object = _UNSET,
):
    """POST a reservation; ``key=None`` omits the header, otherwise the derived key is used."""

    provided = f"order:{order_id}:reserve" if key is _UNSET else key
    headers = {} if provided is None else {"Idempotency-Key": provided}  # type: ignore[dict-item]
    body = {"orderId": order_id, "lines": lines or [{"productId": "p-1001", "quantity": 2}]}
    return client.post(RESERVE, json=body, headers=headers)


def test_reservation_reserves_and_decrements_once(client: TestClient, store) -> None:
    response = reserve(client, key="order:o-1:reserve")

    assert response.status_code == 200
    assert response.json() == {"orderId": "o-1", "reserved": True, "replayed": False}
    assert set(response.json()) == {"orderId", "reserved", "replayed"}
    assert store.quantity_of("p-1001") == 48


def test_a_replayed_reservation_changes_nothing(client: TestClient, store) -> None:
    reserve(client, key="order:o-1:reserve")

    replay = reserve(client, key="order:o-1:reserve")

    assert replay.status_code == 200
    assert replay.json() == {"orderId": "o-1", "reserved": True, "replayed": True}
    assert store.quantity_of("p-1001") == 48
    assert len(store.movements_for("o-1", "reserve")) == 1


def test_the_same_key_with_a_different_body_is_an_idempotency_conflict(client: TestClient, store) -> None:
    reserve(client, key="order:o-1:reserve")

    conflict = reserve(client, lines=[{"productId": "p-1001", "quantity": 3}], key="order:o-1:reserve")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert store.quantity_of("p-1001") == 48


def test_insufficient_stock_changes_nothing(client: TestClient, store) -> None:
    response = reserve(client, order_id="o-5", lines=[{"productId": "p-1005", "quantity": 1}])

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "insufficient_stock"
    assert body["error"]["details"]["productId"] == "p-1005"
    assert store.quantity_of("p-1005") == 0
    assert store.movement_keys() == set()


def test_a_partially_insufficient_reservation_leaves_no_trace(client: TestClient, store) -> None:
    response = reserve(
        client,
        order_id="o-6",
        lines=[{"productId": "p-1001", "quantity": 2}, {"productId": "p-1005", "quantity": 1}],
    )

    assert response.status_code == 409
    assert store.quantity_of("p-1001") == 50
    assert store.movement_keys() == set()


def test_reservation_requires_the_documented_idempotency_key(client: TestClient, store) -> None:
    missing = reserve(client, key=None)
    wrong = reserve(client, key="order:o-1:release")

    for response in (missing, wrong):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
    assert store.quantity_of("p-1001") == 50
    assert store.movement_keys() == set()


def test_reservation_rejects_malformed_bodies(client: TestClient) -> None:
    headers = {"Idempotency-Key": "order:o-1:reserve"}
    for payload in (
        {},
        {"orderId": "o-1"},
        {"orderId": "o-1", "lines": []},
        {"orderId": "", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"orderId": "o-1", "lines": [{"productId": "p-1001", "quantity": 0}]},
    ):
        response = client.post(RESERVE, json=payload, headers=headers)
        assert response.status_code == 400, payload
        assert response.json()["error"]["code"] == "validation_error", payload


def test_a_reservation_of_an_unknown_product_is_not_found(client: TestClient, store) -> None:
    response = reserve(client, order_id="o-7", lines=[{"productId": "p-9999", "quantity": 1}])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_product"
    assert store.movement_keys() == set()


def test_two_orders_can_reserve_independently(client: TestClient, store) -> None:
    first = reserve(client, order_id="o-a", lines=[{"productId": "p-1002", "quantity": 5}], key="order:o-a:reserve")
    second = reserve(client, order_id="o-b", lines=[{"productId": "p-1002", "quantity": 5}], key="order:o-b:reserve")

    assert [first.status_code, second.status_code] == [200, 200]
    assert store.quantity_of("p-1002") == 15
