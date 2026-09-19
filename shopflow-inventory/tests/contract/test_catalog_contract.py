"""Contract tests: catalog reads (contracts.md §4, handoff acceptance 1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PRODUCT_VIEW_KEYS = {
    "productId",
    "sku",
    "name",
    "description",
    "price",
    "currency",
    "availableQuantity",
    "inStock",
}

DOCUMENTED_CATALOG = [
    ("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", 19.99, 50, True),
    ("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", 12.50, 25, True),
    ("p-1003", "SF-HOOD-003", "ShopFlow Hoodie", 49.00, 10, True),
    ("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", 4.25, 100, True),
    ("p-1005", "SF-CAP-005", "ShopFlow Cap", 22.00, 0, False),
]


def test_list_products_returns_items_and_total(client: TestClient) -> None:
    response = client.get("/products")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert set(payload) == {"items", "total"}
    assert payload["total"] == 5
    assert len(payload["items"]) == 5
    for item in payload["items"]:
        assert set(item) == PRODUCT_VIEW_KEYS


def test_list_products_returns_the_five_seeded_products_in_id_order(client: TestClient) -> None:
    payload = client.get("/products").json()

    observed = [
        (item["productId"], item["sku"], item["name"], item["price"], item["availableQuantity"], item["inStock"])
        for item in payload["items"]
    ]
    assert observed == DOCUMENTED_CATALOG


def test_list_products_paginates(client: TestClient) -> None:
    first = client.get("/products", params={"limit": 2, "offset": 0}).json()
    second = client.get("/products", params={"limit": 2, "offset": 2}).json()
    empty = client.get("/products", params={"limit": 0}).json()

    assert [item["productId"] for item in first["items"]] == ["p-1001", "p-1002"]
    assert first["total"] == 5
    assert [item["productId"] for item in second["items"]] == ["p-1003", "p-1004"]
    assert second["total"] == 5
    assert empty == {"items": [], "total": 5}


def test_list_products_rejects_invalid_pagination(client: TestClient) -> None:
    response = client.get("/products", params={"limit": -1}, headers={"X-Correlation-Id": "corr-1"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["correlationId"] == "corr-1"
    assert body["error"]["details"] == {"parameter": "limit", "value": "-1"}
    assert "limit" in body["error"]["message"]


def test_the_documented_query_form_is_accepted(client: TestClient) -> None:
    """The handoff documents the request line as ``/products?limit=&offset=``."""

    response = client.get("/products?limit=&offset=")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 5


@pytest.mark.parametrize("params", [{"limit": "abc"}, {"offset": "1.5"}, {"offset": -1}, {"limit": " "}])
def test_list_products_rejects_non_numeric_pagination(client: TestClient, params: dict) -> None:
    response = client.get("/products", params=params)

    expected_status = 200 if params == {"limit": " "} else 400
    assert response.status_code == expected_status, (params, response.text)


def test_get_product_returns_the_documented_view(client: TestClient) -> None:
    response = client.get("/products/p-1001")

    assert response.status_code == 200
    assert response.json() == {
        "productId": "p-1001",
        "sku": "SF-TEE-001",
        "name": "ShopFlow Classic Tee",
        "description": "Heavyweight cotton t-shirt.",
        "price": 19.99,
        "currency": "USD",
        "availableQuantity": 50,
        "inStock": True,
    }


def test_get_product_of_a_zero_stock_product_is_not_in_stock(client: TestClient) -> None:
    body = client.get("/products/p-1005").json()

    assert body["availableQuantity"] == 0
    assert body["inStock"] is False


def test_get_product_of_an_unknown_id_is_not_found(client: TestClient) -> None:
    response = client.get("/products/p-9999", headers={"X-Correlation-Id": "corr-404"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Unknown product p-9999.",
            "correlationId": "corr-404",
            "details": {"productId": "p-9999"},
        }
    }


def test_catalog_reads_never_change_stock(client: TestClient, store) -> None:
    before = store.snapshot()

    client.get("/products")
    client.get("/products/p-1002")

    assert store.snapshot() == before
    assert store.movement_keys() == set()
