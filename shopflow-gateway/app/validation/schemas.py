"""Explicit request models for the public gateway API (contracts.md §2).

Validation happens in the gateway, before any upstream call. Values are never
rewritten on the way through: a body that passes validation is forwarded with
the same field values the client sent.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_CANCELLATION_REASON_LENGTH = 200
MAX_IDENTIFIER_LENGTH = 256


class OrderLine(BaseModel):
    """One order line of ``POST /api/orders``."""

    model_config = ConfigDict(extra="forbid")

    productId: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    quantity: int = Field(..., strict=True, gt=0)

    @field_validator("productId")
    @classmethod
    def _reject_blank_product_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("productId must be a non-empty string")
        return value


class CreateOrderRequest(BaseModel):
    """Body of ``POST /api/orders``."""

    model_config = ConfigDict(extra="forbid")

    customerId: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    lines: list[OrderLine] = Field(..., min_length=1)

    @field_validator("customerId")
    @classmethod
    def _reject_blank_customer_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("customerId must be a non-empty string")
        return value

    def to_upstream_payload(self) -> dict[str, Any]:
        """Return the body forwarded to ``shopflow-orders`` (values unchanged)."""

        return {
            "customerId": self.customerId,
            "lines": [{"productId": line.productId, "quantity": line.quantity} for line in self.lines],
        }


class CancelOrderRequest(BaseModel):
    """Body of ``POST /api/orders/{orderId}/cancel``.

    The type is validated here (``validation_error``); emptiness and length are
    business validation of the cancellation reason and produce ``invalid_reason``.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str


def cancellation_reason_error(reason: str) -> str | None:
    """Return the ``invalid_reason`` message for a bad reason, otherwise ``None``."""

    if not reason.strip():
        return "Cancellation reason must not be empty."
    if len(reason) > MAX_CANCELLATION_REASON_LENGTH:
        return f"Cancellation reason must not exceed {MAX_CANCELLATION_REASON_LENGTH} characters."
    return None


def pagination_params(limit: int | None, offset: int | None) -> dict[str, str]:
    """Return the pagination query parameters that were actually supplied."""

    params: dict[str, str] = {}
    if limit is not None:
        params["limit"] = str(limit)
    if offset is not None:
        params["offset"] = str(offset)
    return params
