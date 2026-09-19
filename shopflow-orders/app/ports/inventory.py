"""The inventory port: the consumed ``shopflow-inventory`` HTTP interface (contracts.md §4).

The orders service never writes stock: it asks the inventory to check
availability and to reserve/release, always with the idempotency key the contract
fixes (``order:{orderId}:reserve`` / ``order:{orderId}:release``). Every operation
takes an optional timeout so the caller can bound a whole request against
``REQUEST_TIMEOUT_SECONDS`` (contracts.md §11).

Failures are expressed with the domain errors of contracts.md §12:
``insufficient_stock``, ``unknown_product``, ``idempotency_conflict`` and
``inventory_unavailable``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.domain.order import RequestedLine


def reservation_key(order_id: str) -> str:
    """The ``Idempotency-Key`` of a reservation (contracts.md §4)."""

    return f"order:{order_id}:reserve"


def release_key(order_id: str) -> str:
    """The ``Idempotency-Key`` of a release (contracts.md §4)."""

    return f"order:{order_id}:release"


@dataclass(frozen=True, slots=True)
class InventoryProduct:
    """The product snapshot copied into an order line (contracts.md §4, §5)."""

    product_id: str
    sku: str
    name: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class AvailabilityItem:
    """One ``items[]`` entry of ``POST /inventory/availability``."""

    product_id: str
    requested: int
    available: int
    sufficient: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "productId": self.product_id,
            "requested": self.requested,
            "available": self.available,
            "sufficient": self.sufficient,
        }


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    """The answer of ``POST /inventory/availability`` (``{available, items}``)."""

    available: bool
    items: tuple[AvailabilityItem, ...] = ()

    @property
    def insufficient_items(self) -> tuple[AvailabilityItem, ...]:
        return tuple(item for item in self.items if not item.sufficient)


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """The answer of ``POST /inventory/reservations``."""

    order_id: str
    reserved: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    """The answer of ``POST /inventory/releases``."""

    order_id: str
    restored: bool
    replayed: bool


@runtime_checkable
class InventoryPort(Protocol):
    """The inventory operations the order lifecycle needs."""

    def get_product(self, product_id: str, *, timeout_seconds: float | None = None) -> InventoryProduct:
        """``GET /products/{productId}``: the snapshot of one product.

        Raises :class:`~app.domain.errors.UnknownProduct` (``404``) or
        :class:`~app.domain.errors.InventoryUnavailable` (``503``).
        """

    def check_availability(
        self,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> AvailabilityResult:
        """``POST /inventory/availability``: read-only stock check, all lines at once."""

    def reserve(
        self,
        order_id: str,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> ReservationResult:
        """``POST /inventory/reservations`` with ``Idempotency-Key: order:{id}:reserve``.

        All-or-nothing: raises :class:`~app.domain.errors.InsufficientStock` (``409``)
        when the race was lost, so no stock remains reserved.
        """

    def release(
        self,
        order_id: str,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> ReleaseResult:
        """``POST /inventory/releases`` with ``Idempotency-Key: order:{id}:release``."""

    def is_reachable(self, *, timeout_seconds: float | None = None) -> bool:
        """``True`` when the inventory answers its liveness probe."""


__all__ = [
    "AvailabilityItem",
    "AvailabilityResult",
    "InventoryPort",
    "InventoryProduct",
    "ReleaseResult",
    "ReservationResult",
    "release_key",
    "reservation_key",
]
