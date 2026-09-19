"""Integration tests of the read surface (HANDOFF #5, contracts.md §3)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.support.db import order_row as read_order_row
from tests.support.db import order_rows, outbox_rows


def place_order(client: TestClient, *, customer_id: str, product_id: str = "p-1004", quantity: int = 1) -> dict:
    response = client.post(
        "/orders",
        json={"customerId": customer_id, "lines": [{"productId": product_id, "quantity": quantity}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_the_detail_view_matches_the_stored_row(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    created = place_order(client, customer_id="c-read", product_id="p-1003", quantity=1)

    response = client.get(f"/orders/{created['orderId']}")

    assert response.status_code == 200
    view = response.json()
    row = read_order_row(session_factory, created["orderId"])
    assert row is not None
    assert view["orderId"] == row["id"]
    assert view["status"] == row["status"] == "CONFIRMED"
    assert view["customerId"] == row["customer_id"]
    assert view["total"] == float(row["total"])
    assert view["confirmedAt"] is not None
    assert view["cancelledAt"] is None
    assert view["cancellationReason"] is None


def test_the_detail_view_shows_the_cancellation(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    created = place_order(client, customer_id="c-read-cancel", product_id="p-1004", quantity=1)
    client.post(f"/orders/{created['orderId']}/cancel", json={"reason": "customer changed their mind"})

    view = client.get(f"/orders/{created['orderId']}").json()

    assert view["status"] == "CANCELLED"
    assert view["cancelledAt"] is not None
    assert view["cancellationReason"] == "customer changed their mind"
    row = read_order_row(session_factory, created["orderId"])
    assert row is not None
    assert row["cancellation_reason"] == "customer changed their mind"
    assert row["inventory_release_state"] == "RELEASED"


def test_the_list_is_scoped_to_the_customer_and_paginated(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    first = place_order(client, customer_id="c-list-1", product_id="p-1004", quantity=1)
    second = place_order(client, customer_id="c-list-1", product_id="p-1004", quantity=2)
    place_order(client, customer_id="c-list-2", product_id="p-1004", quantity=1)

    page = client.get("/orders", params={"customerId": "c-list-1"})
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert [item["orderId"] for item in page.json()["items"]] == [first["orderId"], second["orderId"]]

    first_page = client.get("/orders", params={"customerId": "c-list-1", "limit": 1})
    assert first_page.json()["total"] == 2
    assert [item["orderId"] for item in first_page.json()["items"]] == [first["orderId"]]

    second_page = client.get("/orders", params={"customerId": "c-list-1", "limit": 1, "offset": 1})
    assert [item["orderId"] for item in second_page.json()["items"]] == [second["orderId"]]

    beyond = client.get("/orders", params={"customerId": "c-list-1", "offset": 99})
    assert beyond.json() == {"items": [], "total": 2}

    other = client.get("/orders", params={"customerId": "c-list-2"})
    assert other.json()["total"] == 1

    nobody = client.get("/orders", params={"customerId": "c-nobody"})
    assert nobody.json() == {"items": [], "total": 0}

    # the list holds exactly the rows the store holds
    assert sorted(row["customer_id"] for row in order_rows(session_factory)) == [
        "c-list-1",
        "c-list-1",
        "c-list-2",
    ]


def test_an_unknown_order_is_404_in_the_shared_envelope(client: TestClient) -> None:
    response = client.get(
        "/orders/11111111-2222-3333-4444-555555555555", headers={"X-Correlation-Id": "corr-read-404"}
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["correlationId"] == "corr-read-404"
    assert body["error"]["details"]["orderId"] == "11111111-2222-3333-4444-555555555555"


def test_malformed_parameters_are_validation_errors(client: TestClient) -> None:
    assert client.get("/orders").status_code == 400
    assert client.get("/orders", params={"customerId": "c-1", "limit": -1}).status_code == 400
    assert client.get("/orders", params={"customerId": "c-1", "offset": -1}).status_code == 400


def test_a_read_never_writes(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    created = place_order(client, customer_id="c-read-only", product_id="p-1004", quantity=1)
    orders_before = order_rows(session_factory)
    events_before = outbox_rows(session_factory)

    client.get(f"/orders/{created['orderId']}")
    client.get("/orders", params={"customerId": "c-read-only"})

    assert order_rows(session_factory) == orders_before
    assert outbox_rows(session_factory) == events_before
