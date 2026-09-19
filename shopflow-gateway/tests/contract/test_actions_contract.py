"""Cancel and ship routes: validation, admin guard and pass-through semantics."""

from __future__ import annotations

import httpx
import pytest

from tests.support.assertions import (
    assert_error_envelope,
    assert_json_response,
    available_quantity,
    create_order,
)

ADMIN_HEADERS = {"X-ShopFlow-Role": "admin"}


def _confirmed_order(client: httpx.Client, customer_id: str = "actions-customer") -> str:
    response = create_order(client, customer_id, [{"productId": "p-1002", "quantity": 1}])
    assert response.status_code == 201, response.text
    return response.json()["orderId"]


def _shipped_order(client: httpx.Client, customer_id: str = "actions-customer") -> str:
    order_id = _confirmed_order(client, customer_id)
    shipped = client.post(f"/api/orders/{order_id}/ship", headers=ADMIN_HEADERS)
    assert shipped.status_code == 200, shipped.text
    return order_id


def _status_check(client: httpx.Client, order_id: str, expected_status: str) -> None:
    """Assert the order still has ``expected_status`` (no state change happened)."""

    current = client.get(f"/api/orders/{order_id}")
    assert current.status_code == 200, current.text
    assert current.json()["status"] == expected_status


def test_cancel_returns_the_cancelled_order(client: httpx.Client, direct_orders: httpx.Client) -> None:
    order_id = _confirmed_order(client)

    response = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "changed my mind"})

    order = assert_json_response(response, status=200)
    assert order["status"] == "CANCELLED"
    assert order["cancellationReason"] == "changed my mind"
    assert order["cancelledAt"]
    assert order == direct_orders.get(f"/orders/{order_id}").json()


def test_cancel_restores_stock(client: httpx.Client, direct_inventory: httpx.Client) -> None:
    stock_before = available_quantity(direct_inventory, "p-1002")
    order_id = _confirmed_order(client)
    assert available_quantity(direct_inventory, "p-1002") == stock_before - 1

    assert client.post(f"/api/orders/{order_id}/cancel", json={"reason": "no longer needed"}).status_code == 200

    assert available_quantity(direct_inventory, "p-1002") == stock_before


@pytest.mark.parametrize("reason", ["", "   ", "\t", "x" * 201])
def test_invalid_reason_is_rejected_without_an_upstream_call(
    client: httpx.Client,
    evidence: object,
    reason: str,
) -> None:
    order_id = _confirmed_order(client)
    evidence.gateway_log.clear()  # type: ignore[attr-defined]

    response = client.post(f"/api/orders/{order_id}/cancel", json={"reason": reason})

    assert_error_envelope(response, status=400, code="invalid_reason")
    evidence.assert_no_upstream_call(  # type: ignore[attr-defined]
        "orders",
        method="POST",
        path=f"/orders/{order_id}/cancel",
        state_check=lambda: assert_json_response(client.get(f"/api/orders/{order_id}"), status=200),
    )


@pytest.mark.parametrize(
    "body",
    [{}, {"reason": None}, {"reason": 5}, {"reason": {"a": 1}}, {"reason": "ok", "extra": 1}],
)
def test_cancel_type_errors_are_validation_errors(client: httpx.Client, evidence: object, body: object) -> None:
    order_id = _confirmed_order(client)
    evidence.gateway_log.clear()  # type: ignore[attr-defined]

    response = client.post(f"/api/orders/{order_id}/cancel", json=body)

    assert_error_envelope(response, status=400, code="validation_error")
    evidence.assert_no_upstream_call("orders", method="POST", path=f"/orders/{order_id}/cancel")  # type: ignore[attr-defined]


def test_cancel_unknown_order_is_not_found(client: httpx.Client) -> None:
    response = client.post("/api/orders/missing/cancel", json={"reason": "because"})

    assert_error_envelope(response, status=404, code="not_found")


def test_cancelling_a_shipped_order_is_rejected(client: httpx.Client) -> None:
    order_id = _shipped_order(client, "shipped-cancel-customer")

    response = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "too late"})

    assert_error_envelope(response, status=409, code="order_shipped")
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "SHIPPED"


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-ShopFlow-Role": "user"}, {"X-ShopFlow-Role": ""}, {"X-ShopFlow-Role": "Admin"}],
)
def test_ship_without_the_admin_role_is_forbidden_and_makes_no_upstream_call(
    client: httpx.Client,
    evidence: object,
    headers: dict[str, str],
) -> None:
    order_id = _confirmed_order(client)
    evidence.gateway_log.clear()  # type: ignore[attr-defined]

    response = client.post(f"/api/orders/{order_id}/ship", headers=headers)

    assert_error_envelope(response, status=403, code="forbidden")
    evidence.assert_no_upstream_call(  # type: ignore[attr-defined]
        "orders",
        method="POST",
        path=f"/orders/{order_id}/ship",
        state_check=lambda: _status_check(client, order_id, "CONFIRMED"),
    )


def test_ship_with_the_admin_role_returns_the_shipped_order(client: httpx.Client, direct_orders: httpx.Client) -> None:
    order_id = _confirmed_order(client, "ship-customer")

    response = client.post(f"/api/orders/{order_id}/ship", headers=ADMIN_HEADERS)

    order = assert_json_response(response, status=200)
    assert order["status"] == "SHIPPED"
    assert order["shippedAt"]
    assert order == direct_orders.get(f"/orders/{order_id}").json()


def test_shipping_twice_is_invalid_status_transition(client: httpx.Client) -> None:
    order_id = _shipped_order(client, "double-ship-customer")

    response = client.post(f"/api/orders/{order_id}/ship", headers=ADMIN_HEADERS)

    assert_error_envelope(response, status=409, code="invalid_status_transition")


def test_shipping_an_unknown_order_is_not_found(client: httpx.Client) -> None:
    response = client.post("/api/orders/missing/ship", headers=ADMIN_HEADERS)

    assert_error_envelope(response, status=404, code="not_found")


def test_order_not_cancellable_is_passed_through(
    client: httpx.Client,
    upstream_stack: object,
) -> None:
    order_id = _confirmed_order(client, "not-cancellable-customer")
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        f"/orders/{order_id}/cancel",
        status=409,
        body={"error": {"code": "order_not_cancellable", "message": "Order is not in a cancellable state."}},
        times=1,
    )

    response = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "please"})

    assert_error_envelope(response, status=409, code="order_not_cancellable")


def test_idempotency_conflict_is_passed_through(client: httpx.Client, upstream_stack: object) -> None:
    order_id = _confirmed_order(client, "conflict-customer")
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        f"/orders/{order_id}/cancel",
        status=409,
        body={"error": {"code": "idempotency_conflict", "message": "Idempotency key reused with a different body."}},
        times=1,
    )

    response = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "please"})

    assert_error_envelope(response, status=409, code="idempotency_conflict")
