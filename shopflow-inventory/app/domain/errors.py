"""Domain errors of the inventory core.

These are the only failures the domain knows about. They carry the snake_case
error code of contracts.md §12 but no transport details: the API layer maps a
code to its HTTP status and error envelope, so the core stays framework-free
(no ``fastapi`` and no ``sqlalchemy`` import in ``app/domain`` or
``app/usecases``).
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for every rule violation of the inventory domain."""

    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class InvalidInput(DomainError):
    """A request value violates a domain invariant (malformed payload/parameter)."""

    code = "validation_error"


class InvalidIdempotencyKey(InvalidInput):
    """The ``Idempotency-Key`` header is missing or does not name this command."""

    code = "validation_error"

    def __init__(self, order_id: str, operation: str) -> None:
        expected = f"order:{order_id}:{operation}"
        super().__init__(
            f"Header Idempotency-Key must be '{expected}'.",
            details={"expectedIdempotencyKey": expected},
        )
        self.order_id = order_id
        self.operation = operation


class NotFound(DomainError):
    """A requested resource does not exist (contracts.md §12: ``not_found``)."""

    code = "not_found"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class UnknownProduct(DomainError):
    """A *line* references a product id that the catalog does not know."""

    code = "unknown_product"

    def __init__(self, product_id: str) -> None:
        super().__init__(f"Unknown product {product_id}.", details={"productId": product_id})
        self.product_id = product_id


class InsufficientStock(DomainError):
    """A line requests more units than the stock ledger currently holds."""

    code = "insufficient_stock"

    def __init__(self, product_id: str, requested: int, available: int) -> None:
        super().__init__(
            f"Requested {requested} unit(s) of {product_id} but only {available} are available.",
            details={"productId": product_id, "requested": requested, "available": available},
        )
        self.product_id = product_id
        self.requested = requested
        self.available = available


class IdempotencyConflict(DomainError):
    """The same ``Idempotency-Key`` was replayed with a different body."""

    code = "idempotency_conflict"

    def __init__(self, order_id: str, operation: str) -> None:
        super().__init__(
            f"Idempotency key for order {order_id} operation {operation} was already used with a different body.",
            details={"orderId": order_id, "operation": operation},
        )
        self.order_id = order_id
        self.operation = operation
