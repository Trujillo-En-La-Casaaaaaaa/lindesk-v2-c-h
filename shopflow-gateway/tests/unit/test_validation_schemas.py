"""Explicit request validation happens before any upstream call."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.validation.schemas import (
    MAX_CANCELLATION_REASON_LENGTH,
    CancelOrderRequest,
    CreateOrderRequest,
    cancellation_reason_error,
    pagination_params,
)


def test_valid_order_body_is_accepted_and_forwarded_unchanged() -> None:
    payload = {
        "customerId": "customer-42",
        "lines": [
            {"productId": "p-1001", "quantity": 2},
            {"productId": "p-1002", "quantity": 1},
        ],
    }

    request = CreateOrderRequest.model_validate(payload)

    assert request.to_upstream_payload() == payload


@pytest.mark.parametrize(
    "body",
    [
        {"customerId": "", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "   ", "lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "customer-42", "lines": []},
        {"customerId": "customer-42"},
        {"lines": [{"productId": "p-1001", "quantity": 1}]},
        {"customerId": "customer-42", "lines": [{"productId": "p-1001", "quantity": 0}]},
        {"customerId": "customer-42", "lines": [{"productId": "p-1001", "quantity": -3}]},
        {"customerId": "customer-42", "lines": [{"productId": "p-1001", "quantity": 1.5}]},
        {"customerId": "customer-42", "lines": [{"productId": "p-1001", "quantity": "2"}]},
        {"customerId": "customer-42", "lines": [{"productId": "p-1001", "quantity": True}]},
        {"customerId": "customer-42", "lines": [{"productId": "", "quantity": 1}]},
        {"customerId": "customer-42", "lines": [{"productId": "p-1001", "quantity": 1, "extra": 1}]},
        {"customerId": "customer-42", "lines": [{"productId": "p-1001", "quantity": 1}], "extra": "x"},
        "not-an-object",
    ],
)
def test_invalid_order_bodies_are_rejected(body: object) -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest.model_validate(body)


def test_quantity_must_be_a_positive_integer() -> None:
    request = CreateOrderRequest.model_validate(
        {"customerId": "c", "lines": [{"productId": "p-1001", "quantity": 5}]}
    )

    assert request.lines[0].quantity == 5
    assert isinstance(request.lines[0].quantity, int)


@pytest.mark.parametrize("body", [{}, {"reason": None}, {"reason": 12}, {"reason": {"a": 1}}, {"reason": "x", "y": 1}])
def test_cancel_body_type_errors_are_validation_errors(body: object) -> None:
    with pytest.raises(ValidationError):
        CancelOrderRequest.model_validate(body)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("\t\n", "empty"),
        ("x" * (MAX_CANCELLATION_REASON_LENGTH + 1), "long"),
    ],
)
def test_invalid_cancellation_reasons_are_reported(reason: str, expected: str) -> None:
    message = cancellation_reason_error(reason)

    assert message is not None
    if expected == "empty":
        assert "empty" in message
    else:
        assert str(MAX_CANCELLATION_REASON_LENGTH) in message


@pytest.mark.parametrize("reason", ["changed my mind", " x ", "r" * MAX_CANCELLATION_REASON_LENGTH])
def test_valid_cancellation_reasons_pass(reason: str) -> None:
    assert cancellation_reason_error(reason) is None


def test_cancel_body_keeps_the_reason_value() -> None:
    assert CancelOrderRequest.model_validate({"reason": "  spaced  "}).reason == "  spaced  "


def test_pagination_params_only_forward_supplied_values() -> None:
    assert pagination_params(None, None) == {}
    assert pagination_params(10, None) == {"limit": "10"}
    assert pagination_params(None, 5) == {"offset": "5"}
    assert pagination_params(0, 0) == {"limit": "0", "offset": "0"}
