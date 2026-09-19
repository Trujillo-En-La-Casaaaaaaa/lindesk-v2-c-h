"""Small assertion helpers shared by the contract and integration suites."""

from __future__ import annotations

from typing import Any

import httpx

CORRELATION_HEADER = "X-Correlation-Id"


def assert_error_envelope(
    response: httpx.Response,
    *,
    status: int | None = None,
    code: str | None = None,
    correlation_id: str | None = None,
    message_contains: str | None = None,
) -> dict[str, Any]:
    """Assert the single error envelope of contracts.md §1."""

    if status is not None:
        assert response.status_code == status, f"expected {status}, got {response.status_code}: {response.text}"
    header_correlation = response.headers.get(CORRELATION_HEADER)
    assert header_correlation, "the gateway must echo X-Correlation-Id on every response"

    body = response.json()
    assert set(body) == {"error"}, f"error responses must only carry the envelope, got {sorted(body)}"
    error = body["error"]
    assert set(error) == {"code", "message", "correlationId", "details"}, sorted(error)
    if code is not None:
        assert error["code"] == code, f"expected code {code}, got {error['code']}: {response.text}"
    assert isinstance(error["message"], str) and error["message"].strip()
    assert isinstance(error["details"], dict)
    assert error["correlationId"] == header_correlation
    if correlation_id is not None:
        assert header_correlation == correlation_id
        assert error["correlationId"] == correlation_id
    if message_contains is not None:
        assert message_contains in error["message"]
    assert "Traceback" not in response.text
    return error


def assert_json_response(response: httpx.Response, *, status: int | None = None) -> Any:
    """Assert status and JSON content type, then return the decoded body."""

    if status is not None:
        assert response.status_code == status, f"expected {status}, got {response.status_code}: {response.text}"
    content_type = response.headers.get("content-type", "")
    assert "application/json" in content_type, content_type
    assert response.headers.get(CORRELATION_HEADER)
    return response.json()


def order_payload(customer_id: str = "customer-contract", lines: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "customerId": customer_id,
        "lines": lines if lines is not None else [{"productId": "p-1001", "quantity": 2}],
    }


def create_order(
    client: httpx.Client,
    customer_id: str = "customer-contract",
    lines: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    return client.post("/api/orders", json=order_payload(customer_id, lines), **kwargs)


def available_quantity(direct_inventory: httpx.Client, product_id: str) -> int:
    """Read the stock level straight from the inventory service."""

    response = direct_inventory.get(f"/products/{product_id}")
    assert response.status_code == 200, response.text
    return int(response.json()["availableQuantity"])
