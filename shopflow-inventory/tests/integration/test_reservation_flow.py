"""Integration tests: reservations against PostgreSQL (handoff acceptance 2 and 6)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.support.db import movement_rows, stock_rows
from tests.support.requests import reservation_body, reserve_headers, stock_of


def test_reserving_two_units_decrements_the_ledger_once(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/inventory/reservations",
        json=reservation_body("o-1", ("p-1001", 2)),
        headers=reserve_headers("o-1"),
    )

    assert response.status_code == 200
    assert response.json() == {"orderId": "o-1", "reserved": True, "replayed": False}
    assert stock_of(client, "p-1001") == 48
    assert stock_rows(session_factory)["p-1001"] == 48
    assert movement_rows(session_factory, order_id="o-1") == [
        {
            "orderId": "o-1",
            "operation": "reserve",
            "payload": {"lines": [{"productId": "p-1001", "quantity": 2}]},
        }
    ]


def test_resending_the_same_request_is_a_replay(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    body = reservation_body("o-1", ("p-1001", 2))
    headers = reserve_headers("o-1")

    first = client.post("/inventory/reservations", json=body, headers=headers)
    second = client.post("/inventory/reservations", json=body, headers=headers)

    assert first.json() == {"orderId": "o-1", "reserved": True, "replayed": False}
    assert second.status_code == 200
    assert second.json() == {"orderId": "o-1", "reserved": True, "replayed": True}
    assert stock_of(client, "p-1001") == 48
    assert len(movement_rows(session_factory, order_id="o-1", operation="reserve")) == 1


def test_a_replayed_reservation_after_a_restart_keeps_the_ledger(
    client: TestClient, restarted_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """The idempotency record lives in the store, so a new process sees it."""

    body = reservation_body("o-restart", ("p-1003", 4))
    headers = reserve_headers("o-restart")
    assert client.post("/inventory/reservations", json=body, headers=headers).status_code == 200

    replay = restarted_client.post("/inventory/reservations", json=body, headers=headers)

    assert replay.status_code == 200
    assert replay.json() == {"orderId": "o-restart", "reserved": True, "replayed": True}
    assert stock_rows(session_factory)["p-1003"] == 6
    assert len(movement_rows(session_factory, order_id="o-restart")) == 1


def test_a_different_body_with_the_same_key_is_a_conflict(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    headers = reserve_headers("o-1")
    client.post("/inventory/reservations", json=reservation_body("o-1", ("p-1001", 2)), headers=headers)

    conflict = client.post(
        "/inventory/reservations", json=reservation_body("o-1", ("p-1001", 3)), headers=headers
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert stock_rows(session_factory)["p-1001"] == 48
    assert len(movement_rows(session_factory, order_id="o-1")) == 1


def test_reserving_the_zero_stock_product_changes_nothing(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/inventory/reservations",
        json=reservation_body("o-1005", ("p-1005", 1)),
        headers=reserve_headers("o-1005"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    assert stock_rows(session_factory)["p-1005"] == 0
    assert movement_rows(session_factory) == []


def test_a_multi_line_reservation_is_all_or_nothing(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    response = client.post(
        "/inventory/reservations",
        json=reservation_body("o-mixed", ("p-1001", 5), ("p-1005", 1)),
        headers=reserve_headers("o-mixed"),
    )

    assert response.status_code == 409
    assert stock_rows(session_factory)["p-1001"] == 50
    assert stock_rows(session_factory)["p-1005"] == 0
    assert movement_rows(session_factory) == []


def test_an_unknown_product_in_a_reservation_is_not_found(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/inventory/reservations",
        json=reservation_body("o-unknown", ("p-9999", 1)),
        headers=reserve_headers("o-unknown"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_product"
    assert movement_rows(session_factory) == []


def test_a_failed_reservation_does_not_block_the_retry(client: TestClient) -> None:
    """Safe under retries: reject, then succeed for the same order."""

    headers = reserve_headers("o-retry")
    too_much = client.post("/inventory/reservations", json=reservation_body("o-retry", ("p-1003", 99)), headers=headers)

    retry = client.post("/inventory/reservations", json=reservation_body("o-retry", ("p-1003", 2)), headers=headers)

    assert too_much.status_code == 409
    assert retry.status_code == 200
    assert retry.json() == {"orderId": "o-retry", "reserved": True, "replayed": False}
    assert stock_of(client, "p-1003") == 8


def test_reservation_requires_the_documented_key(client: TestClient) -> None:
    missing = client.post("/inventory/reservations", json=reservation_body("o-1", ("p-1001", 1)))
    wrong = client.post(
        "/inventory/reservations",
        json=reservation_body("o-1", ("p-1001", 1)),
        headers={"Idempotency-Key": "order:o-2:reserve"},
    )

    assert [missing.status_code, wrong.status_code] == [400, 400]
    assert missing.json()["error"]["code"] == "validation_error"
