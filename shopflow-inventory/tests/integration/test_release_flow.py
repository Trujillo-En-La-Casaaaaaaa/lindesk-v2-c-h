"""Integration tests: releases against PostgreSQL (acceptance 4, 5 and 7)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.support.db import movement_rows, stock_rows
from tests.support.requests import release_body, release_headers, reservation_body, reserve_headers, stock_of


def reserve(client: TestClient, order_id: str, *lines: tuple[str, int]):
    return client.post(
        "/inventory/reservations",
        json=reservation_body(order_id, *lines),
        headers=reserve_headers(order_id),
    )


def release(client: TestClient, order_id: str, *lines: tuple[str, int]):
    return client.post(
        "/inventory/releases",
        json=release_body(order_id, *lines),
        headers=release_headers(order_id),
    )


def test_release_restores_exactly_the_reserved_quantities(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert reserve(client, "o-1", ("p-1001", 2), ("p-1003", 3)).status_code == 200
    assert stock_rows(session_factory)["p-1001"] == 48
    assert stock_rows(session_factory)["p-1003"] == 7

    response = release(client, "o-1", ("p-1001", 2), ("p-1003", 3))

    assert response.status_code == 200
    assert response.json() == {"orderId": "o-1", "restored": True, "replayed": False}
    assert stock_rows(session_factory)["p-1001"] == 50
    assert stock_rows(session_factory)["p-1003"] == 10
    assert movement_rows(session_factory, order_id="o-1", operation="release") == [
        {
            "orderId": "o-1",
            "operation": "release",
            "payload": {
                "lines": [
                    {"productId": "p-1001", "quantity": 2},
                    {"productId": "p-1003", "quantity": 3},
                ]
            },
        }
    ]


def test_releasing_twice_restores_once(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    reserve(client, "o-1", ("p-1001", 2))

    first = release(client, "o-1", ("p-1001", 2))
    second = release(client, "o-1", ("p-1001", 2))

    assert first.json() == {"orderId": "o-1", "restored": True, "replayed": False}
    assert second.status_code == 200
    assert second.json() == {"orderId": "o-1", "restored": False, "replayed": True}
    assert stock_of(client, "p-1001") == 50
    assert len(movement_rows(session_factory, order_id="o-1", operation="release")) == 1


def test_release_of_a_never_reserved_order_is_a_no_op(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    before = stock_rows(session_factory)

    response = release(client, "o-never", ("p-1001", 2))

    assert response.status_code == 200
    assert response.json() == {"orderId": "o-never", "restored": False, "replayed": False}
    assert stock_rows(session_factory) == before
    assert movement_rows(session_factory) == []


def test_a_no_op_release_does_not_poison_a_later_reservation_and_release(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert release(client, "o-late", ("p-1001", 2)).json()["restored"] is False

    assert reserve(client, "o-late", ("p-1001", 2)).status_code == 200
    assert stock_rows(session_factory)["p-1001"] == 48

    restored = release(client, "o-late", ("p-1001", 2))

    assert restored.json() == {"orderId": "o-late", "restored": True, "replayed": False}
    assert stock_rows(session_factory)["p-1001"] == 50


def test_release_never_exceeds_the_pre_reservation_level(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    pre_reservation = stock_rows(session_factory)["p-1004"]
    assert reserve(client, "o-bounded", ("p-1004", 90)).status_code == 200
    assert stock_rows(session_factory)["p-1004"] == pre_reservation - 90

    response = release(client, "o-bounded", ("p-1004", 90))

    assert response.json()["restored"] is True
    assert stock_rows(session_factory)["p-1004"] == pre_reservation


def test_a_release_with_a_stale_body_still_cannot_inflate_stock(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """The recorded reservation is the source of truth, not the retried body."""

    assert reserve(client, "o-stale", ("p-1003", 4)).status_code == 200

    response = release(client, "o-stale", ("p-1003", 400))

    assert response.json()["restored"] is True
    assert stock_rows(session_factory)["p-1003"] == 10


def test_a_release_ignores_products_it_never_reserved(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert reserve(client, "o-mixed", ("p-1003", 4), ("p-1004", 10)).status_code == 200

    response = release(client, "o-mixed", ("p-1003", 4), ("p-1002", 5))

    assert response.json()["restored"] is True
    assert stock_rows(session_factory)["p-1003"] == 10
    assert stock_rows(session_factory)["p-1004"] == 100
    assert stock_rows(session_factory)["p-1002"] == 25


def test_a_repeated_release_after_a_restart_is_still_a_replay(
    client: TestClient, restarted_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    reserve(client, "o-restart-release", ("p-1002", 5))
    assert release(client, "o-restart-release", ("p-1002", 5)).json()["restored"] is True

    replay = release(restarted_client, "o-restart-release", ("p-1002", 5))

    assert replay.status_code == 200
    assert replay.json() == {"orderId": "o-restart-release", "restored": False, "replayed": True}
    assert stock_rows(session_factory)["p-1002"] == 25


def test_a_release_with_a_different_body_is_an_idempotency_conflict(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    reserve(client, "o-1", ("p-1001", 2))
    release(client, "o-1", ("p-1001", 2))

    conflict = release(client, "o-1", ("p-1001", 1))

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert stock_rows(session_factory)["p-1001"] == 50


def test_release_rejects_an_empty_order_id(client: TestClient) -> None:
    response = release(client, "", ("p-1001", 1))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
