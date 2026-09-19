"""Order routes proxy ``shopflow-orders`` transparently."""

from __future__ import annotations

import httpx
import pytest

from tests.support.assertions import (
    assert_error_envelope,
    assert_json_response,
    available_quantity,
    create_order,
    order_payload,
)


def test_valid_order_is_created_and_returned_unchanged(
    client: httpx.Client,
    direct_orders: httpx.Client,
    direct_inventory: httpx.Client,
) -> None:
    stock_before = available_quantity(direct_inventory, "p-1001")

    response = create_order(client, "orders-contract", [{"productId": "p-1001", "quantity": 2}])

    order = assert_json_response(response, status=201)
    assert order["status"] == "CONFIRMED"
    assert order["customerId"] == "orders-contract"
    assert order["lines"][0]["productId"] == "p-1001"
    assert order["lines"][0]["quantity"] == 2
    assert order["lines"][0]["lineTotal"] == 39.98
    assert order["total"] == 39.98

    direct = direct_orders.get(f"/orders/{order['orderId']}")
    assert direct.json() == order, "the gateway must return the order view unchanged"
    assert available_quantity(direct_inventory, "p-1001") == stock_before - 2


def test_create_forwards_the_correlation_id(client: httpx.Client, upstream_stack: object) -> None:
    response = create_order(client, "corr-customer", headers={"X-Correlation-Id": "create-corr-1"})

    assert response.status_code == 201
    log = upstream_stack.log_for("orders")  # type: ignore[attr-defined]
    assert log is not None
    forwarded = [record.correlation_id for record in log.matching("POST", "/orders")]
    assert forwarded and all(value == "create-corr-1" for value in forwarded)


@pytest.mark.parametrize(
    "body",
    [
        {"customerId": "", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "   ", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "c", "lines": []},
        {"customerId": "c"},
        {"lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "c", "lines": [{"productId": "p-1001", "quantity": -1}]},
        {"customerId": "c", "lines": [{"productId": "p-1001", "quantity": 0}]},
        {"customerId": "c", "lines": [{"productId": "p-1001"}]},
        {"customerId": "c", "lines": [{"quantity": 1}]},
        {"customerId": "c", "lines": [{"productId": "p-1001", "quantity": 1, "unitPrice": 0}]},
        {"customerId": "c", "lines": [{"productId": "p-1001", "quantity": 1}], "total": 0},
    ],
)
def test_validation_failures_never_reach_the_backend(
    client: httpx.Client,
    evidence: object,
    body: dict[str, object],
) -> None:
    response = client.post("/api/orders", json=body)

    assert_error_envelope(response, status=400, code="validation_error")
    evidence.assert_no_upstream_call("orders", method="POST", path="/orders")  # type: ignore[attr-defined]


def test_malformed_json_is_a_validation_error(client: httpx.Client, evidence: object) -> None:
    response = client.post("/api/orders", content=b"{not json", headers={"content-type": "application/json"})

    assert_error_envelope(response, status=400, code="validation_error")
    evidence.assert_no_upstream_call("orders", method="POST", path="/orders")  # type: ignore[attr-defined]


def test_zero_stock_product_returns_insufficient_stock(
    client: httpx.Client,
    direct_inventory: httpx.Client,
) -> None:
    stock_before = available_quantity(direct_inventory, "p-1005")

    response = create_order(client, "stock-customer", [{"productId": "p-1005", "quantity": 1}])

    assert_error_envelope(response, status=409, code="insufficient_stock")
    assert available_quantity(direct_inventory, "p-1005") == stock_before == 0


def test_order_read_returns_the_order_view(client: httpx.Client, direct_orders: httpx.Client) -> None:
    order_id = create_order(client, "read-customer").json()["orderId"]

    response = client.get(f"/api/orders/{order_id}")

    assert response.json() == direct_orders.get(f"/orders/{order_id}").json()
    assert set(response.json()) == {
        "orderId",
        "status",
        "customerId",
        "createdAt",
        "updatedAt",
        "confirmedAt",
        "shippedAt",
        "cancelledAt",
        "cancellationReason",
        "total",
        "lines",
    }


def test_unknown_order_is_a_404_envelope(client: httpx.Client) -> None:
    assert_error_envelope(client.get("/api/orders/missing-order"), status=404, code="not_found")


def test_order_list_is_filtered_by_customer(client: httpx.Client, direct_orders: httpx.Client) -> None:
    first = create_order(client, "list-customer").json()["orderId"]
    create_order(client, "other-customer")

    response = client.get("/api/orders", params={"customerId": "list-customer"})

    body = assert_json_response(response, status=200)
    assert body["total"] == 1
    assert [item["orderId"] for item in body["items"]] == [first]
    assert body == direct_orders.get("/orders", params={"customerId": "list-customer"}).json()


@pytest.mark.parametrize("params", [{}, {"customerId": ""}, {"customerId": "   "}])
def test_order_list_requires_a_customer(client: httpx.Client, evidence: object, params: dict[str, str]) -> None:
    response = client.get("/api/orders", params=params)

    assert_error_envelope(response, status=400, code="validation_error")
    evidence.assert_no_upstream_call("orders", method="GET", path="/orders")  # type: ignore[attr-defined]


def test_order_list_forwards_pagination(client: httpx.Client, upstream_stack: object) -> None:
    create_order(client, "page-customer")

    response = client.get("/api/orders", params={"customerId": "page-customer", "limit": 5, "offset": 0})

    assert response.status_code == 200
    log = upstream_stack.log_for("orders")  # type: ignore[attr-defined]
    assert log is not None
    assert log.matching("GET", "/orders")[-1].query == "customerId=page-customer&limit=5&offset=0"


def test_idempotency_key_is_not_invented_by_the_gateway(client: httpx.Client, upstream_stack: object) -> None:
    create_order(client, "no-key-customer")

    log = upstream_stack.log_for("orders")  # type: ignore[attr-defined]
    assert log is not None
    assert log.matching("POST", "/orders")[-1].header("idempotency-key") is None


def test_client_idempotency_key_is_forwarded_when_supplied(client: httpx.Client, upstream_stack: object) -> None:
    response = client.post(
        "/api/orders",
        json=order_payload("key-customer"),
        headers={"Idempotency-Key": "order:x:reserve"},
    )

    assert response.status_code == 201
    log = upstream_stack.log_for("orders")  # type: ignore[attr-defined]
    assert log is not None
    assert log.matching("POST", "/orders")[-1].header("idempotency-key") == "order:x:reserve"
