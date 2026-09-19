"""Error normalization: every gateway error becomes the envelope of contracts.md §1.

Upstream envelopes are passed through with their ``code`` preserved so clients
can distinguish ``insufficient_stock``, ``order_shipped`` and ``invalid_reason``.
Unknown upstream failures become ``500 internal_error`` and never leak stack
traces, upstream hostnames or upstream identifiers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse

#: contracts.md §12 error codes plus the gateway-level ``upstream_unavailable``.
CODE_MESSAGES: dict[str, str] = {
    "validation_error": "Request validation failed.",
    "invalid_reason": "Cancellation reason must not be empty.",
    "not_found": "The requested resource does not exist.",
    "unknown_product": "A referenced product does not exist.",
    "forbidden": "Administrative route called without the admin role.",
    "insufficient_stock": "Requested quantity exceeds available stock.",
    "order_shipped": "Cancellation rejected because the order is SHIPPED.",
    "order_not_cancellable": "Order is not in a cancellable state.",
    "invalid_status_transition": "Ship attempted from a status other than CONFIRMED.",
    "idempotency_conflict": "Idempotency key reused with a different body.",
    "inventory_unavailable": "Inventory service unreachable during order creation.",
    "upstream_unavailable": "An upstream service is unavailable.",
    "internal_error": "Unexpected server error.",
}

CODE_STATUS: dict[str, int] = {
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
    "upstream_unavailable": 503,
    "internal_error": 500,
}

KNOWN_CODES: frozenset[str] = frozenset(CODE_MESSAGES)

UPSTREAM_UNAVAILABLE = "upstream_unavailable"
INTERNAL_ERROR = "internal_error"

#: Statuses of an upstream that carry no parseable envelope and mean "not available".
UPSTREAM_DOWN_STATUSES: frozenset[int] = frozenset({502, 503, 504})

#: Technical codes whose message is always the gateway's own wording, so a
#: broken upstream can never leak hostnames or stack traces through them.
TECHNICAL_CODES: frozenset[str] = frozenset({"internal_error", "upstream_unavailable", "inventory_unavailable"})

_STATUS_TO_CODE: dict[int, str] = {
    400: "validation_error",
    401: "forbidden",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "internal_error",
    422: "validation_error",
    503: "upstream_unavailable",
}


class GatewayError(Exception):
    """An error raised inside the gateway that must be returned as an envelope."""

    def __init__(
        self,
        code: str,
        status_code: int | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code if code in KNOWN_CODES else INTERNAL_ERROR
        self.status_code = status_code or CODE_STATUS[self.code]
        self.message = message or CODE_MESSAGES[self.code]
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(f"{self.code}: {self.message}")


def error_payload(
    code: str,
    correlation_id: str,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the single error envelope of contracts.md §1."""

    resolved_code = code if code in KNOWN_CODES else INTERNAL_ERROR
    return {
        "error": {
            "code": resolved_code,
            "message": message or CODE_MESSAGES[resolved_code],
            "correlationId": correlation_id,
            "details": dict(details or {}),
        }
    }


def error_response(
    code: str,
    correlation_id: str,
    status_code: int | None = None,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Return the envelope as an ``application/json`` response."""

    resolved_code = code if code in KNOWN_CODES else INTERNAL_ERROR
    resolved_status = status_code or CODE_STATUS[resolved_code]
    return JSONResponse(
        error_payload(resolved_code, correlation_id, message=message, details=details),
        status_code=resolved_status,
    )


def http_status_to_code(status_code: int) -> str:
    """Map a framework-level HTTP status to the closest contract code."""

    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if 400 <= status_code < 500:
        return "validation_error"
    return INTERNAL_ERROR


def normalize_upstream_error(
    *,
    status_code: int,
    body: Any,
    correlation_id: str,
    service: str,
) -> JSONResponse:
    """Convert an upstream error response into the gateway envelope."""

    code, message, details, http_status = interpret_upstream_error(status_code=status_code, body=body)
    return error_response(
        code=code,
        status_code=http_status,
        message=message,
        correlation_id=correlation_id,
        details=details,
    )


def interpret_upstream_error(*, status_code: int, body: Any) -> tuple[str, str, dict[str, Any], int]:
    """Return ``(code, message, details, http_status)`` for an upstream failure."""

    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            raw_code = error.get("code")
            if isinstance(raw_code, str) and raw_code in KNOWN_CODES:
                http_status = status_code if 400 <= status_code <= 599 else CODE_STATUS[raw_code]
                raw_message = error.get("message")
                keep_upstream_message = (
                    raw_code not in TECHNICAL_CODES and isinstance(raw_message, str) and bool(raw_message.strip())
                )
                message = raw_message if keep_upstream_message else CODE_MESSAGES[raw_code]
                raw_details = error.get("details")
                details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
                return raw_code, message, details, http_status

    if status_code in UPSTREAM_DOWN_STATUSES:
        return UPSTREAM_UNAVAILABLE, CODE_MESSAGES[UPSTREAM_UNAVAILABLE], {}, 503
    if status_code == 400:
        return "validation_error", CODE_MESSAGES["validation_error"], {}, 400
    if status_code in (401, 403):
        return "forbidden", CODE_MESSAGES["forbidden"], {}, 403
    if status_code == 404:
        return "not_found", CODE_MESSAGES["not_found"], {}, 404
    return INTERNAL_ERROR, CODE_MESSAGES[INTERNAL_ERROR], {}, 500
