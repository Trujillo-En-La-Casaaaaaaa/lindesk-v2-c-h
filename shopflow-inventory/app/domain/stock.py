"""Stock ledger, stock lines and stock movement records (contracts.md §9).

The ledger owns one invariant that the whole service depends on:

* **never negative** - a :class:`StockLevel` cannot be decremented below zero, so
  a reservation that would oversell raises :class:`~app.domain.errors.InsufficientStock`
  instead of leaking a database error to the caller.

The store enforces the same rule with ``CHECK (quantity >= 0)`` (see the Alembic
migration), so the invariant holds even for a writer that bypasses this module.

Pure domain code: no web framework and no database library is imported here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.errors import InsufficientStock, InvalidIdempotencyKey, InvalidInput

RESERVE = "reserve"
RELEASE = "release"
OPERATIONS: tuple[str, str] = (RESERVE, RELEASE)


def idempotency_key_for(order_id: str, operation: str) -> str:
    """The ``Idempotency-Key`` value the contract prescribes for one command."""

    return f"order:{order_id}:{operation}"


def require_idempotency_key(*, order_id: str, operation: str, provided: str | None) -> str:
    """Return the expected key, or refuse the command with ``400 validation_error``."""

    expected = idempotency_key_for(order_id, operation)
    if provided is None or provided.strip() != expected:
        raise InvalidIdempotencyKey(order_id, operation)
    return expected


@dataclass(frozen=True, slots=True)
class StockLine:
    """One requested ``{productId, quantity}`` line (contracts.md §4)."""

    product_id: str
    quantity: int

    def __post_init__(self) -> None:
        if not isinstance(self.product_id, str) or not self.product_id.strip():
            raise InvalidInput("productId must be a non-empty string.")
        object.__setattr__(self, "product_id", self.product_id.strip())
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise InvalidInput("quantity must be an integer.")
        if self.quantity <= 0:
            raise InvalidInput("quantity must be greater than zero.")

    def as_payload(self) -> dict[str, Any]:
        return {"productId": self.product_id, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class StockLevel:
    """The quantity of one product, plus the never-negative invariant."""

    product_id: str
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise InvalidInput(f"stock of {self.product_id} must never be negative.")

    @property
    def in_stock(self) -> bool:
        return self.quantity > 0

    def decrement(self, amount: int) -> StockLevel:
        """Return the level after removing ``amount`` units, or refuse to oversell."""

        _require_amount(amount)
        if amount > self.quantity:
            raise InsufficientStock(self.product_id, amount, self.quantity)
        return StockLevel(self.product_id, self.quantity - amount)

    def increment(self, amount: int) -> StockLevel:
        """Return the level after restoring ``amount`` units."""

        _require_amount(amount)
        return StockLevel(self.product_id, self.quantity + amount)


@dataclass(frozen=True, slots=True)
class StockMovement:
    """A recorded ``reserve`` or ``release`` for one order (contracts.md §9).

    ``unique(order_id, operation)`` in the store makes this record the durable
    idempotency guard: a retried command can never change stock a second time,
    even after a restart.
    """

    order_id: str
    operation: str
    lines: tuple[StockLine, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise InvalidInput("orderId must be a non-empty string.")
        if self.operation not in OPERATIONS:
            raise InvalidInput(f"operation must be one of {sorted(OPERATIONS)}.")
        if not self.lines:
            raise InvalidInput("a stock movement must carry at least one line.")

    def payload(self) -> dict[str, Any]:
        """The canonical JSON payload stored in ``stock_movements.payload``."""

        return {"lines": [line.as_payload() for line in self.lines]}


def normalize_lines(lines: Iterable[StockLine]) -> tuple[StockLine, ...]:
    """Aggregate duplicate product ids, keeping first-appearance order.

    A request may legally repeat a product; the effective demand is the sum, so
    the aggregate is what gets verified, reserved and echoed back.
    """

    aggregated: dict[str, int] = {}
    for line in lines:
        aggregated[line.product_id] = aggregated.get(line.product_id, 0) + line.quantity
    return tuple(StockLine(product_id, quantity) for product_id, quantity in aggregated.items())


def canonical_lines(lines: Iterable[StockLine]) -> tuple[StockLine, ...]:
    """Aggregate and sort by product id (the canonical body of a movement)."""

    return tuple(sorted(normalize_lines(lines), key=lambda line: line.product_id))


def movement_payload(lines: Iterable[StockLine]) -> dict[str, Any]:
    """The canonical payload of the given lines, used for idempotency comparison."""

    return {"lines": [line.as_payload() for line in lines]}


def plan_decrements(lines: Sequence[StockLine], available: Mapping[str, int]) -> dict[str, int]:
    """Verify every line and return the per-product decrements.

    All-or-nothing: the whole plan is verified before anything is returned, so a
    single insufficient line rejects the reservation without touching stock. A
    product the caller already resolved as existing but that has no ledger row
    counts as zero available.
    """

    plan: dict[str, int] = {}
    for line in lines:
        level = StockLevel(line.product_id, max(int(available.get(line.product_id, 0)), 0))
        level.decrement(line.quantity)  # raises InsufficientStock before anything is written
        plan[line.product_id] = line.quantity
    return plan


def plan_restoration(reserved: Sequence[StockLine], requested: Sequence[StockLine]) -> dict[str, int]:
    """The quantities a release restores.

    The recorded reservation is the source of truth (a release restores exactly
    what was reserved), so a release can never push a product above its
    pre-reservation level, whatever body the caller retries with.
    """

    restored = {line.product_id: line.quantity for line in reserved}
    for line in requested:  # requested products that were never reserved restore nothing
        restored.setdefault(line.product_id, 0)
    return {product_id: quantity for product_id, quantity in restored.items() if quantity > 0}


def _require_amount(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidInput("amount must be a positive integer.")
