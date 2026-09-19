"""Catalog routes proxy ``shopflow-inventory`` transparently."""

from __future__ import annotations

import httpx
import pytest

from tests.support.assertions import assert_error_envelope, assert_json_response


def test_product_list_is_the_inventory_list_unchanged(client: httpx.Client, direct_inventory: httpx.Client) -> None:
    direct = direct_inventory.get("/products")
    through_gateway = client.get("/api/products")

    assert through_gateway.status_code == 200
    assert through_gateway.json() == direct.json()
    assert through_gateway.headers.get("X-Correlation-Id")


def test_pagination_is_forwarded_unchanged(
    client: httpx.Client,
    direct_inventory: httpx.Client,
    upstream_stack: object,
) -> None:
    direct = direct_inventory.get("/products", params={"limit": 2, "offset": 1})

    response = client.get("/api/products", params={"limit": 2, "offset": 1})

    assert response.status_code == 200
    assert response.json() == direct.json()
    log = upstream_stack.log_for("inventory")  # type: ignore[attr-defined]
    assert log is not None
    queries = [record.query for record in log.matching("GET", "/products")]
    assert queries[-1] == "limit=2&offset=1"


def test_without_pagination_no_query_is_sent(client: httpx.Client, upstream_stack: object) -> None:
    response = client.get("/api/products")

    assert response.status_code == 200
    log = upstream_stack.log_for("inventory")  # type: ignore[attr-defined]
    assert log is not None
    assert log.matching("GET", "/products")[-1].query == ""


@pytest.mark.parametrize(
    "params",
    [{"limit": -1}, {"offset": -1}, {"limit": "abc"}, {"offset": "1.5"}, {"limit": ""}],
)
def test_invalid_pagination_is_rejected_without_an_upstream_call(
    client: httpx.Client,
    evidence: object,
    params: dict[str, object],
) -> None:
    response = client.get("/api/products", params=params)

    assert_error_envelope(response, status=400, code="validation_error")
    evidence.assert_no_upstream_call("inventory", method="GET", path="/products")  # type: ignore[attr-defined]


def test_single_product_is_the_inventory_product_unchanged(
    client: httpx.Client,
    direct_inventory: httpx.Client,
) -> None:
    direct = direct_inventory.get("/products/p-1003")

    response = client.get("/api/products/p-1003")

    assert response.status_code == 200
    assert response.json() == direct.json()
    assert response.json()["productId"] == "p-1003"


def test_unknown_product_is_a_404_envelope(client: httpx.Client) -> None:
    response = client.get("/api/products/p-does-not-exist")

    assert_error_envelope(response, status=404, code="not_found")


def test_zero_stock_product_is_still_visible_in_the_catalog(client: httpx.Client) -> None:
    product = assert_json_response(client.get("/api/products/p-1005"), status=200)

    assert product["availableQuantity"] == 0
    assert product["inStock"] is False
