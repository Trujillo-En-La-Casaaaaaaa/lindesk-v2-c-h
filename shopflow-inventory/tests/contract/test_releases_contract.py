"""Contract tests: stock releases (contracts.md §4, handoff acceptance 4 and 5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

RESERVE = "/inventory/reservations"
RELEASE = "/inventory/releases"
LINES = [{"productId": "p-1001", "quantity": 2}]
_UNSET = object()


def reserve(client: TestClient, *, order_id: str = "o-1", lines: list[dict] | None = None):
    return client.post(
        RESERVE,
        json={"orderId": order_id, "lines": lines or LINES},
        headers={"Idempotency-Key": f"order:{order_id}:reserve"},
    )


def release(
    client: TestClient,
    *,
    order_id: str = "o-1",
    lines: list[dict] | None = None,
    key: object = _UNSET,
):
    """POST a release; ``key=None`` omits the header, otherwise the derived key is used."""

    provided = f"order:{order_id}:release" if key is _UNSET else key
    headers = {} if provided is None else {"Idempotency-Key": provided}  # type: ignore[dict-item]
    return client.post(RELEASE, json={"orderId": order_id, "lines": lines or LINES}, headers=headers)


def test_first_release_restores_the_reserved_quantity(client: TestClient, store) -> None:
    reserve(client)

    response = release(client, key="order:o-1:release")

    assert response.status_code == 200
    assert response.json() == {"orderId": "o-1", "restored": True, "replayed": False}
    assert set(response.json()) == {"orderId", "restored", "replayed"}
    assert store.quantity_of("p-1001") == 50


def test_a_repeated_release_is_a_replay_without_stock_change(client: TestClient, store) -> None:
    reserve(client)
    release(client, key="order:o-1:release")

    replay = release(client, key="order:o-1:release")

    assert replay.status_code == 200
    assert replay.json() == {"orderId": "o-1", "restored": False, "replayed": True}
    assert store.quantity_of("p-1001") == 50


def test_a_release_of_an_order_that_was_never_reserved_is_a_safe_no_op(client: TestClient, store) -> None:
    before = store.snapshot()

    response = release(client, order_id="o-never", key="order:o-never:release")

    assert response.status_code == 200
    assert response.json() == {"orderId": "o-never", "restored": False, "replayed": False}
    assert store.snapshot() == before
    assert store.movement_keys() == set()


def test_the_same_key_with_a_different_body_is_an_idempotency_conflict(client: TestClient, store) -> None:
    reserve(client)
    release(client, key="order:o-1:release")

    conflict = release(client, lines=[{"productId": "p-1001", "quantity": 1}], key="order:o-1:release")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert store.quantity_of("p-1001") == 50


def test_a_release_never_exceeds_the_pre_reservation_level(client: TestClient, store) -> None:
    reserve(client, lines=[{"productId": "p-1003", "quantity": 3}])

    response = release(client, lines=[{"productId": "p-1003", "quantity": 500}], key="order:o-1:release")

    assert response.status_code == 200
    assert response.json()["restored"] is True
    assert store.quantity_of("p-1003") == 10


def test_release_requires_the_documented_idempotency_key(client: TestClient, store) -> None:
    reserve(client)

    missing = release(client, key=None)
    wrong = release(client, key="order:o-1:reserve")

    for response in (missing, wrong):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
    assert store.quantity_of("p-1001") == 48


def test_release_rejects_malformed_bodies(client: TestClient) -> None:
    headers = {"Idempotency-Key": "order:o-1:release"}
    for payload in ({}, {"orderId": "o-1"}, {"orderId": "o-1", "lines": []}, {"lines": LINES}):
        response = client.post(RELEASE, json=payload, headers=headers)
        assert response.status_code == 400, payload
        assert response.json()["error"]["code"] == "validation_error", payload


def test_reserve_release_reserve_cycle_of_one_order(client: TestClient, store) -> None:
    reserve(client, order_id="o-cycle")
    assert store.quantity_of("p-1001") == 48

    release(client, order_id="o-cycle")
    assert store.quantity_of("p-1001") == 50

    # The reserve record of the order still exists (unique(order_id, operation)),
    # so reserving the same order again is a replay of the first reservation: the
    # contract answers reserved=true/replayed=true and does not decrement twice.
    replay = reserve(client, order_id="o-cycle")
    assert replay.json()["replayed"] is True
    assert store.quantity_of("p-1001") == 50
