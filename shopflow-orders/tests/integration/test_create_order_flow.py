"""Integration tests of order creation (HANDOFF #2, acceptance criteria 1, 2 and 11)."""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import DEFAULT_REQUEST_TIMEOUT_SECONDS
from tests.integration.conftest import (
    StubInventory,
    inventory_product,
    inventory_stock,
    start_orders_server,
)
from tests.support.db import count_orders, line_rows, order_row, order_rows, outbox_rows

CUSTOMER = "c-integration-create"


def create(client: TestClient, lines: list[dict], *, customer_id: str = CUSTOMER, correlation: str = "corr"):
    return client.post(
        "/orders",
        json={"customerId": customer_id, "lines": lines},
        headers={"X-Correlation-Id": correlation},
    )


def test_acceptance_1_create_order_confirms_and_decrements_stock(
    client: TestClient,
    inventory_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    """``p-1001`` x2 -> ``201`` ``CONFIRMED`` within ``REQUEST_TIMEOUT_SECONDS``, stock -2."""

    before = inventory_stock(inventory_url, "p-1001")

    started = time.monotonic()
    response = create(client, [{"productId": "p-1001", "quantity": 2}])
    elapsed = time.monotonic() - started

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["status"] == "CONFIRMED"
    assert order["confirmedAt"] is not None
    assert elapsed <= DEFAULT_REQUEST_TIMEOUT_SECONDS, f"order creation took {elapsed:.3f}s"

    after = inventory_stock(inventory_url, "p-1001")
    assert after == before - 2
    if before == 50:  # the deterministic seed of contracts.md §10
        assert after == 48

    stored = order_row(session_factory, order["orderId"])
    assert stored is not None
    assert stored["status"] == "CONFIRMED"
    assert stored["confirmed_at"] is not None
    assert stored["inventory_release_state"] == "NONE"
    assert stored["total"] == "39.98"

    lines = line_rows(session_factory, order["orderId"])
    assert len(lines) == 1
    assert lines[0]["product_id"] == "p-1001"
    assert lines[0]["quantity"] == 2
    assert lines[0]["unit_price"] == "19.99"

    events = outbox_rows(session_factory, order_id=order["orderId"])
    assert len(events) == 1
    assert events[0]["event_type"] == "order.confirmed"
    assert events[0]["published_at"] is None
    assert events[0]["payload"]["orderId"] == order["orderId"]


def test_acceptance_2_zero_stock_product_is_rejected_and_changes_nothing(
    client: TestClient,
    inventory_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    """``p-1005`` has zero stock: ``409 insufficient_stock``, no order, no stock movement."""

    before = inventory_stock(inventory_url, "p-1005")
    assert before == 0  # the documented baseline (contracts.md §10)

    response = create(client, [{"productId": "p-1005", "quantity": 1}])

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "insufficient_stock"
    assert error["details"]["productId"] == "p-1005"
    assert error["details"]["available"] == 0
    assert count_orders(session_factory) == 0
    assert inventory_stock(inventory_url, "p-1005") == 0
    assert outbox_rows(session_factory) == []


def test_the_line_snapshot_is_copied_from_the_inventory_product(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    product = inventory_product(inventory_url, "p-1003")
    response = create(client, [{"productId": "p-1003", "quantity": 1}])
    assert response.status_code == 201

    order = response.json()
    line = order["lines"][0]
    assert line["sku"] == product["sku"]
    assert line["name"] == product["name"]
    assert line["unitPrice"] == float(product["price"])
    assert order["total"] == float(product["price"])

    stored = line_rows(session_factory, order["orderId"])[0]
    assert stored["sku"] == product["sku"]
    assert stored["name"] == product["name"]
    assert float(stored["unit_price"]) == float(product["price"])


def test_a_multi_line_order_decrements_every_product_and_sums_the_total(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    before_tee = inventory_stock(inventory_url, "p-1001")
    before_stickers = inventory_stock(inventory_url, "p-1004")
    tee = inventory_product(inventory_url, "p-1001")
    stickers = inventory_product(inventory_url, "p-1004")

    response = create(
        client,
        [
            {"productId": "p-1004", "quantity": 3},
            {"productId": "p-1001", "quantity": 1},
        ],
    )

    assert response.status_code == 201
    order = response.json()
    assert [line["productId"] for line in order["lines"]] == ["p-1004", "p-1001"]
    expected_total = round(3 * float(stickers["price"]) + 1 * float(tee["price"]), 2)
    assert order["total"] == expected_total
    assert inventory_stock(inventory_url, "p-1004") == before_stickers - 3
    assert inventory_stock(inventory_url, "p-1001") == before_tee - 1
    assert len(line_rows(session_factory, order["orderId"])) == 2


def test_unknown_product_is_404_and_writes_nothing(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    response = create(client, [{"productId": "p-9999", "quantity": 1}])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_product"
    assert count_orders(session_factory) == 0
    assert outbox_rows(session_factory) == []


def test_more_than_the_available_stock_is_rejected_with_the_real_numbers(
    client: TestClient, inventory_url: str, session_factory: sessionmaker[Session]
) -> None:
    available = inventory_stock(inventory_url, "p-1003")

    response = create(client, [{"productId": "p-1003", "quantity": available + 500}])

    assert response.status_code == 409
    details = response.json()["error"]["details"]
    assert details["productId"] == "p-1003"
    assert details["available"] == available
    assert details["requested"] == available + 500
    assert count_orders(session_factory) == 0
    assert inventory_stock(inventory_url, "p-1003") == available


def test_the_created_order_is_immediately_readable_and_listed(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = create(client, [{"productId": "p-1002", "quantity": 1}]).json()

    detail = client.get(f"/orders/{created['orderId']}")
    assert detail.status_code == 200
    assert detail.json() == created

    listed = client.get("/orders", params={"customerId": CUSTOMER})
    assert listed.status_code == 200
    assert [item["orderId"] for item in listed.json()["items"]] == [created["orderId"]]
    assert listed.json()["total"] == 1


def test_two_orders_of_one_customer_are_both_persisted(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    first = create(client, [{"productId": "p-1004", "quantity": 1}]).json()
    second = create(client, [{"productId": "p-1004", "quantity": 2}]).json()

    rows = outbox_rows(session_factory)
    assert sorted(row["payload"]["orderId"] for row in rows) == sorted([first["orderId"], second["orderId"]])
    assert {row["event_type"] for row in rows} == {"order.confirmed"}
    assert count_orders(session_factory) == 2


def test_concurrent_orders_for_the_last_unit_confirm_exactly_one(
    app_factory,
    stub_inventory,
    session_factory: sessionmaker[Session],
) -> None:
    """Two competing creates for the last unit: exactly one wins, the loser fails."""

    stub: StubInventory = stub_inventory(
        products={"p-9001": {"sku": "SF-TEST-9001", "name": "Race Test Product", "price": Decimal("10.00")}},
        stock={"p-9001": 1},
    )
    app = app_factory(inventory_base_url=stub.base_url)
    server = start_orders_server(app)
    statuses: list[int] = []
    lock = threading.Lock()

    def place_order() -> None:
        with httpx.Client(base_url=server.base_url, timeout=20.0) as http:
            response = http.post(
                "/orders",
                json={"customerId": "c-race", "lines": [{"productId": "p-9001", "quantity": 1}]},
            )
        with lock:
            statuses.append(response.status_code)

    try:
        threads = [threading.Thread(target=place_order) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        server.stop()

    assert sorted(statuses) == [201, 409], statuses
    rows = order_rows(session_factory)
    # One unit, one winner: the loser is rejected either by the availability check
    # (before anything is written) or by the all-or-nothing reservation (order FAILED).
    assert [row["status"] for row in rows].count("CONFIRMED") == 1
    assert all(row["status"] in {"CONFIRMED", "FAILED"} for row in rows)
    assert stub.stock("p-9001") == 0
    winner = next(row["id"] for row in rows if row["status"] == "CONFIRMED")
    assert len(stub.movements(winner)) == 1  # exactly one reservation movement
    assert len(outbox_rows(session_factory)) == 1  # only the winner published an event
    assert stub.state.calls_of("availability") != []  # the check really ran
