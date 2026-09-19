"""Every error becomes the single contracts.md §1 envelope."""

from __future__ import annotations

import pytest

from app.upstream.errors import (
    CODE_MESSAGES,
    CODE_STATUS,
    KNOWN_CODES,
    GatewayError,
    error_payload,
    error_response,
    http_status_to_code,
    interpret_upstream_error,
    normalize_upstream_error,
)

CONTRACT_CODES = {
    "validation_error": 400,
    "invalid_reason": 400,
    "not_found": 404,
    "unknown_product": 404,
    "forbidden": 403,
    "insufficient_stock": 409,
    "order_shipped": 409,
    "order_not_cancellable": 409,
    "invalid_status_transition": 409,
    "idempotency_conflict": 409,
    "inventory_unavailable": 503,
    "internal_error": 500,
}


def test_every_contract_code_is_known_with_the_contract_status() -> None:
    for code, status in CONTRACT_CODES.items():
        assert code in KNOWN_CODES
        assert CODE_STATUS[code] == status


def test_envelope_shape_matches_the_contract() -> None:
    payload = error_payload("insufficient_stock", "cid-1", details={"productId": "p-1005"})

    assert payload == {
        "error": {
            "code": "insufficient_stock",
            "message": CODE_MESSAGES["insufficient_stock"],
            "correlationId": "cid-1",
            "details": {"productId": "p-1005"},
        }
    }


@pytest.mark.parametrize("code", sorted(CONTRACT_CODES))
def test_upstream_envelopes_keep_their_code(code: str) -> None:
    body = {"error": {"code": code, "message": f"{code} from upstream", "details": {"k": "v"}}}

    response = normalize_upstream_error(
        status_code=CONTRACT_CODES[code],
        body=body,
        correlation_id="cid-2",
        service="orders",
    )

    assert response.status_code == CONTRACT_CODES[code]
    payload = response.body.decode()
    assert f'"code":"{code}"' in payload
    assert '"correlationId":"cid-2"' in payload


def test_business_messages_are_preserved_but_technical_messages_are_ours() -> None:
    business = interpret_upstream_error(
        status_code=409,
        body={"error": {"code": "order_shipped", "message": "Order already shipped on 2026-09-18"}},
    )
    assert business[1] == "Order already shipped on 2026-09-18"

    technical = interpret_upstream_error(
        status_code=503,
        body={"error": {"code": "inventory_unavailable", "message": "connect to shopflow-inventory:8000 failed"}},
    )
    assert technical[0] == "inventory_unavailable"
    assert technical[1] == CODE_MESSAGES["inventory_unavailable"]
    assert "shopflow-inventory" not in technical[1]


def test_unknown_upstream_code_becomes_internal_error_without_leaking_details() -> None:
    response = normalize_upstream_error(
        status_code=500,
        body={"error": {"code": "kaboom", "message": "postgres://user:pw@orders-db:5432 exploded"}},
        correlation_id="cid-3",
        service="orders",
    )

    assert response.status_code == 500
    text = response.body.decode()
    assert '"code":"internal_error"' in text
    assert "kaboom" not in text
    assert "orders-db" not in text
    assert "Traceback" not in text


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_status"),
    [
        (502, "upstream_unavailable", 503),
        (503, "upstream_unavailable", 503),
        (504, "upstream_unavailable", 503),
        (400, "validation_error", 400),
        (401, "forbidden", 403),
        (403, "forbidden", 403),
        (404, "not_found", 404),
        (409, "internal_error", 500),
        (418, "internal_error", 500),
        (500, "internal_error", 500),
    ],
)
def test_status_only_failures_are_mapped_safely(status: int, expected_code: str, expected_status: int) -> None:
    code, _message, details, http_status = interpret_upstream_error(status_code=status, body=None)

    assert (code, http_status) == (expected_code, expected_status)
    assert details == {}


def test_non_json_and_malformed_envelopes_are_mapped_safely() -> None:
    code, _message, details, http_status = interpret_upstream_error(
        status_code=500,
        body={"unexpected": "shape"},
    )
    assert (code, http_status, details) == ("internal_error", 500, {})

    code, _message, _details, http_status = interpret_upstream_error(status_code=400, body="<html>bad request</html>")
    assert (code, http_status) == ("validation_error", 400)


def test_unknown_code_in_envelope_is_internal_error() -> None:
    code, _message, _details, http_status = interpret_upstream_error(
        status_code=409,
        body={"error": {"code": "weird_conflict", "message": "boom"}},
    )

    assert (code, http_status) == ("internal_error", 500)


def test_envelope_with_known_code_and_odd_status_uses_the_gateway_status() -> None:
    code, _message, _details, http_status = interpret_upstream_error(
        status_code=200,
        body={"error": {"code": "insufficient_stock", "message": "nope"}},
    )

    assert (code, http_status) == ("insufficient_stock", 409)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, "validation_error"), (403, "forbidden"), (404, "not_found"), (405, "not_found"), (422, "validation_error"),
     (503, "upstream_unavailable"), (500, "internal_error"), (502, "internal_error")],
)
def test_framework_status_mapping(status: int, expected: str) -> None:
    assert http_status_to_code(status) == expected


def test_gateway_error_defaults_come_from_the_contract() -> None:
    error = GatewayError("forbidden")

    assert error.status_code == 403
    assert error.message == CODE_MESSAGES["forbidden"]
    assert error.details == {}


def test_unknown_gateway_error_code_falls_back_to_internal_error() -> None:
    error = GatewayError("not_a_real_code")

    assert error.code == "internal_error"
    assert error.status_code == 500


def test_error_response_uses_the_requested_status() -> None:
    response = error_response("upstream_unavailable", "cid-4", status_code=503, details={"service": "inventory"})

    assert response.status_code == 503
    assert b'"service":"inventory"' in response.body
