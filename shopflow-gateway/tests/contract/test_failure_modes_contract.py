"""Upstream failures are normalized, bounded and never leak internals."""

from __future__ import annotations

import time

import httpx
import pytest

from tests.support.assertions import assert_error_envelope, create_order


def test_upstream_call_evidence_is_observable(client: httpx.Client, evidence: object) -> None:
    """The gateway log used for "no upstream call" claims must actually record calls."""

    assert evidence.gateway_calls("inventory") == 0  # type: ignore[attr-defined]

    assert client.get("/api/products").status_code == 200

    assert evidence.gateway_calls("inventory", "GET", "/products") == 1  # type: ignore[attr-defined]
    evidence.assert_attempts("inventory", "GET", "/products", 1)  # type: ignore[attr-defined]


def test_no_upstream_call_assertion_fails_when_a_call_happened(client: httpx.Client, evidence: object) -> None:
    assert client.get("/api/products").status_code == 200

    with pytest.raises(AssertionError):
        evidence.assert_no_upstream_call("inventory", method="GET", path="/products")  # type: ignore[attr-defined]


def test_unreachable_orders_service_is_503_upstream_unavailable(
    gateway_factory: object,
    stopped_upstream_port: int,
    direct_inventory: httpx.Client,
) -> None:
    gateway = gateway_factory(orders_base_url=f"http://127.0.0.1:{stopped_upstream_port}")  # type: ignore[operator]

    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as client:
        response = create_order(client)

    error = assert_error_envelope(response, status=503, code="upstream_unavailable")
    assert error["details"] == {"service": "orders"}
    assert "127.0.0.1" not in response.text
    assert str(stopped_upstream_port) not in response.text


def test_unreachable_inventory_service_is_503_upstream_unavailable(
    gateway_factory: object,
    stopped_upstream_port: int,
) -> None:
    gateway = gateway_factory(inventory_base_url=f"http://127.0.0.1:{stopped_upstream_port}")  # type: ignore[operator]

    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as client:
        response = client.get("/api/products")

    error = assert_error_envelope(response, status=503, code="upstream_unavailable")
    assert error["details"] == {"service": "inventory"}


def test_inventory_unavailable_from_orders_is_passed_through(client: httpx.Client, upstream_stack: object) -> None:
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        "/orders",
        status=503,
        body={"error": {"code": "inventory_unavailable", "message": "Inventory service unreachable."}},
        times=1,
    )

    response = create_order(client, "inventory-down-customer")

    assert_error_envelope(response, status=503, code="inventory_unavailable")


def test_slow_upstream_is_cut_off_by_the_configured_timeout(
    gateway_factory: object,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("inventory").add(  # type: ignore[attr-defined]
        "GET",
        "/products",
        status=200,
        body={"items": [], "total": 0},
        delay_seconds=1.5,
        times=None,
    )
    gateway = gateway_factory(request_timeout_seconds=0.3, get_retry_attempts=0)  # type: ignore[operator]

    started = time.monotonic()
    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as client:
        response = client.get("/api/products")
    elapsed = time.monotonic() - started

    assert_error_envelope(response, status=503, code="upstream_unavailable")
    assert elapsed < 1.4, f"the gateway waited {elapsed:.2f}s instead of honouring the timeout"


def test_idempotent_get_is_retried_once_and_then_succeeds(
    client: httpx.Client,
    evidence: object,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("inventory").add(  # type: ignore[attr-defined]
        "GET",
        "/products",
        status=502,
        raw_body="<html>bad gateway</html>",
        content_type="text/html",
        times=1,
    )

    response = client.get("/api/products")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 5
    evidence.assert_attempts("inventory", "GET", "/products", 2)  # type: ignore[attr-defined]


def test_persistent_transient_failure_becomes_503(
    client: httpx.Client,
    evidence: object,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("inventory").add(  # type: ignore[attr-defined]
        "GET",
        "/products",
        status=503,
        raw_body="<html>unavailable</html>",
        content_type="text/html",
        times=None,
    )

    response = client.get("/api/products")

    assert_error_envelope(response, status=503, code="upstream_unavailable")
    evidence.assert_attempts("inventory", "GET", "/products", 2)  # type: ignore[attr-defined]


def test_state_changing_post_is_never_retried(
    client: httpx.Client,
    evidence: object,
    upstream_stack: object,
    direct_orders: httpx.Client,
) -> None:
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        "/orders",
        status=502,
        raw_body="<html>bad gateway</html>",
        content_type="text/html",
        times=None,
    )

    response = create_order(client, "no-retry-customer")

    assert_error_envelope(response, status=503, code="upstream_unavailable")
    evidence.assert_attempts("orders", "POST", "/orders", 1)  # type: ignore[attr-defined]
    assert direct_orders.get("/orders", params={"customerId": "no-retry-customer"}).json()["total"] == 0


def test_cancel_is_never_retried(client: httpx.Client, evidence: object, upstream_stack: object) -> None:
    order_id = create_order(client, "cancel-no-retry").json()["orderId"]
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        f"/orders/{order_id}/cancel",
        status=502,
        raw_body="<html>bad gateway</html>",
        content_type="text/html",
        times=None,
    )

    response = client.post(f"/api/orders/{order_id}/cancel", json={"reason": "because"})

    assert_error_envelope(response, status=503, code="upstream_unavailable")
    evidence.assert_attempts("orders", "POST", f"/orders/{order_id}/cancel", 1)  # type: ignore[attr-defined]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "CONFIRMED"


def test_unknown_upstream_code_becomes_500_internal_error_without_leaks(
    client: httpx.Client,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        "/orders",
        status=500,
        body={
            "error": {
                "code": "kaboom_code",
                "message": "postgres://shopflow:shopflow@orders-db:5432/shopflow_orders refused",
                "details": {"host": "orders-db"},
            }
        },
        times=1,
    )

    response = create_order(client, "unknown-code-customer")

    assert_error_envelope(response, status=500, code="internal_error")
    for leaked in ("kaboom_code", "orders-db", "postgres://", "shopflow:shopflow", "Traceback"):
        assert leaked not in response.text, f"gateway leaked {leaked!r}"


def test_upstream_error_without_envelope_becomes_500_internal_error(
    client: httpx.Client,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "GET",
        "/orders",
        status=500,
        raw_body="<html>internal server error</html>",
        content_type="text/html",
        times=None,
    )

    response = client.get("/api/orders", params={"customerId": "whatever"})

    assert_error_envelope(response, status=500, code="internal_error")
    assert "html" not in response.text


def test_upstream_conflict_without_envelope_is_not_guessed(
    client: httpx.Client,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        "/orders",
        status=409,
        body={"unexpected": "shape"},
        times=1,
    )

    response = create_order(client, "guess-customer")

    assert_error_envelope(response, status=500, code="internal_error")


def test_non_json_success_body_is_rejected_as_internal_error(
    client: httpx.Client,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("inventory").add(  # type: ignore[attr-defined]
        "GET",
        "/products",
        status=200,
        raw_body="<html>not json</html>",
        content_type="text/html",
        times=None,
    )

    response = client.get("/api/products")

    assert_error_envelope(response, status=500, code="internal_error")


def test_unknown_product_passthrough_from_orders(client: httpx.Client, upstream_stack: object) -> None:
    upstream_stack.faults_for("orders").add(  # type: ignore[attr-defined]
        "POST",
        "/orders",
        status=404,
        body={"error": {"code": "unknown_product", "message": "Unknown product p-9999."}},
        times=1,
    )

    response = create_order(client, "unknown-product-customer", [{"productId": "p-9999", "quantity": 1}])

    assert_error_envelope(response, status=404, code="unknown_product")
