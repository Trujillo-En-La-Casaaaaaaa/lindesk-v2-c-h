"""Integration tests: catalogue reads against the real store (handoff acceptance 1).

The catalogue comes from ``products`` and ``availableQuantity`` from the stock
ledger, so the test moves the ledger through the public API and re-reads it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.support.db import product_rows
from tests.support.requests import quantity_from, reservation_body, reserve_headers

DOCUMENTED_CATALOG = [
    ("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", 19.99, 50, True),
    ("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", 12.50, 25, True),
    ("p-1003", "SF-HOOD-003", "ShopFlow Hoodie", 49.00, 10, True),
    ("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", 4.25, 100, True),
    ("p-1005", "SF-CAP-005", "ShopFlow Cap", 22.00, 0, False),
]


def test_the_catalogue_holds_the_documented_products(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    rows = product_rows(session_factory)

    assert [row["productId"] for row in rows] == [entry[0] for entry in DOCUMENTED_CATALOG]
    assert [row["sku"] for row in rows] == [entry[1] for entry in DOCUMENTED_CATALOG]
    assert [row["name"] for row in rows] == [entry[2] for entry in DOCUMENTED_CATALOG]
    assert [row["price"] for row in rows] == ["19.99", "12.50", "49.00", "4.25", "22.00"]
    assert {row["currency"] for row in rows} == {"USD"}


def test_get_products_returns_the_five_seeded_products_with_stock(client: TestClient) -> None:
    response = client.get("/products")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    observed = [
        (item["productId"], item["sku"], item["name"], item["price"], item["availableQuantity"], item["inStock"])
        for item in body["items"]
    ]
    assert observed == DOCUMENTED_CATALOG


def test_available_quantity_follows_the_ledger(client: TestClient) -> None:
    reserved = client.post(
        "/inventory/reservations",
        json=reservation_body("o-catalog", ("p-1001", 2), ("p-1004", 5)),
        headers=reserve_headers("o-catalog"),
    )
    assert reserved.status_code == 200, reserved.text

    body = client.get("/products").json()
    assert quantity_from(body, "p-1001") == 48
    assert quantity_from(body, "p-1004") == 95

    single = client.get("/products/p-1001").json()
    assert single["availableQuantity"] == 48
    assert single["inStock"] is True


def test_a_product_without_a_ledger_row_reads_as_zero(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """The catalogue read must not depend on a stock row existing (defensive read)."""

    with session_factory() as session, session.begin():
        session.execute(text("DELETE FROM stock WHERE product_id = 'p-1002'"))

    body = client.get("/products/p-1002").json()

    assert body["availableQuantity"] == 0
    assert body["inStock"] is False


def test_catalog_reads_are_paginated_and_stable(client: TestClient) -> None:
    page = client.get("/products", params={"limit": 2, "offset": 3}).json()

    assert page["total"] == 5
    assert [item["productId"] for item in page["items"]] == ["p-1004", "p-1005"]
