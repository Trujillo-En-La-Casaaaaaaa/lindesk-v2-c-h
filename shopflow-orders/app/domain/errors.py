"""Domain errors of the orders core (contracts.md §12).

These are the only failures the core knows about. Each carries the snake_case
error code of contracts.md §12 plus an optional ``details`` mapping, but no
transport detail at all: the API layer maps a code onto its HTTP status and the
shared error envelope, so ``app/domain`` and ``app/usecases`` stay framework-free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class DomainError(Exception):
    """Base class for every rule violation of the orders domain."""

    #: contracts.md §12 error code.
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class ValidationError(DomainError):
    """The request payload or parameter violates a domain invariant (§12 ``validation_error``)."""

    code = "validation_error"


class InvalidReason(DomainError):
    """The cancellation reason is missing, blank or longer than 200 characters."""

    code = "invalid_reason"


class NotFound(DomainError):
    """The requested order does not exist (§12 ``not_found``)."""

    code = "not_found"


class UnknownProduct(DomainError):
    """A line references a product the inventory does not know (§12 ``unknown_product``)."""

    code = "unknown_product"

    def __init__(self, product_id: str) -> None:
        super().__init__(f"Unknown product {product_id}.", details={"productId": product_id})
        self.product_id = product_id


class InsufficientStock(DomainError):
    """A line requests more units than the inventory can make available."""

    code = "insufficient_stock"

    def __init__(
        self,
        message: str = "Requested quantity exceeds available stock.",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)


class OrderShipped(DomainError):
    """A shipped order may not be cancelled (§12 ``order_shipped``)."""

    code = "order_shipped"

    def __init__(self, order_id: str) -> None:
        super().__init__(
            f"Order {order_id} has been shipped and can no longer be cancelled.",
            details={"orderId": order_id, "status": "SHIPPED"},
        )
        self.order_id = order_id


class OrderNotCancellable(DomainError):
    """The order is in a state that is neither shippable nor cancellable (§12 ``order_not_cancellable``)."""

    code = "order_not_cancellable"

    def __init__(self, order_id: str, status: str) -> None:
        super().__init__(
            f"Order {order_id} is {status} and cannot be cancelled.",
            details={"orderId": order_id, "status": status},
        )
        self.order_id = order_id
        self.status = status


class InvalidStatusTransition(DomainError):
    """A status change the state machine does not allow (§12 ``invalid_status_transition``)."""

    code = "invalid_status_transition"

    def __init__(self, order_id: str, current: str, target: str) -> None:
        super().__init__(
            f"Order {order_id} cannot move from {current} to {target}.",
            details={"orderId": order_id, "status": current, "targetStatus": target},
        )
        self.order_id = order_id
        self.current = current
        self.target = target


class IdempotencyConflict(DomainError):
    """The same ``Idempotency-Key`` was replayed with a different body (§12 ``idempotency_conflict``)."""

    code = "idempotency_conflict"

    def __init__(self, order_id: str, operation: str) -> None:
        super().__init__(
            f"Idempotency key for order {order_id} operation {operation} was already used with a different body.",
            details={"orderId": order_id, "operation": operation},
        )
        self.order_id = order_id
        self.operation = operation


class InventoryUnavailable(DomainError):
    """The inventory service could not be reached (§12 ``inventory_unavailable``)."""

    code = "inventory_unavailable"

    def __init__(
        self,
        message: str = "The inventory service is unavailable.",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)


class InternalError(DomainError):
    """An unexpected server-side failure (§12 ``internal_error``)."""

    code = "internal_error"


def insufficient_stock_details(product_id: str, requested: int, available: int) -> dict[str, Any]:
    """Build the ``details`` payload for the failing product of a create request."""

    return {"productId": product_id, "requested": requested, "available": available}


def insufficient_stock_details_for_items(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build ``details`` from the failing availability items reported by the inventory.

    The failing product is promoted to the top level (``productId``/``requested``/
    ``available``) so a caller sees the reason without walking the list, and the
    full failing set is kept under ``items``.
    """

    failing = [
        {
            "productId": str(item.get("productId", "")),
            "requested": int(item.get("requested", 0)),
            "available": int(item.get("available", 0)),
        }
        for item in items
    ]
    details: dict[str, Any] = {"items": failing}
    if failing:
        details.update(failing[0])
    return details


__all__ = [
    "DomainError",
    "IdempotencyConflict",
    "InsufficientStock",
    "InternalError",
    "InvalidReason",
    "InvalidStatusTransition",
    "InventoryUnavailable",
    "NotFound",
    "OrderNotCancellable",
    "OrderShipped",
    "UnknownProduct",
    "ValidationError",
    "insufficient_stock_details",
    "insufficient_stock_details_for_items",
]
