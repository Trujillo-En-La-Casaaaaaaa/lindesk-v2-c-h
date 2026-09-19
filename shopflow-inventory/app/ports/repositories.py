"""Port protocols of the inventory core (hexagonal architecture).

The use cases depend on these protocols only. ``app/adapters`` implements them
with SQLAlchemy/PostgreSQL; the tests implement them with fakes. Nothing in this
module imports a web framework or a database library.

Ports:

* :class:`CatalogRepository` - read access to the product catalog (contracts.md §9
  ``products``).
* :class:`StockRepository` - access to the stock ledger and its unit of work.
* :class:`StockTransaction` - one transaction over ``stock``/``stock_movements``.
  A reservation or a release is applied through a single instance of it, which is
  what makes both commands all-or-nothing.

Note that the *decision* logic (what is sufficient, what is replayed, what is a
conflict) lives in ``app/usecases``; an adapter only locks rows, reads and writes
them and reports whether a movement row could be inserted.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from app.domain.product import Product
from app.domain.stock import StockMovement


@runtime_checkable
class CatalogRepository(Protocol):
    """Read model of the product catalog."""

    def list_products(self, *, limit: int | None, offset: int) -> tuple[Sequence[Product], int]:
        """Return one page of products (ordered by product id) and the total count."""

    def get_product(self, product_id: str) -> Product | None:
        """Return one product or ``None`` when the catalog does not know it."""


@runtime_checkable
class StockTransaction(Protocol):
    """One transaction over the stock ledger and the stock movement records."""

    def record_movement(self, movement: StockMovement) -> bool:
        """Insert the movement, guarded by ``unique(order_id, operation)``.

        Returns ``True`` when this transaction created the record and ``False``
        when the same (order, operation) was already recorded - the durable
        signal that the command is a replay. The caller must then apply no stock
        change at all.
        """

    def find_movement(self, order_id: str, operation: str) -> StockMovement | None:
        """Return the recorded movement for one (order, operation), if any."""

    def lock_levels(self, product_ids: Sequence[str]) -> dict[str, int]:
        """Lock the stock rows of ``product_ids`` (``SELECT ... FOR UPDATE``).

        Returns the locked quantities by product id; products without a ledger
        row are simply absent. Rows are always locked in a deterministic order so
        concurrent reservations cannot deadlock.
        """

    def decrement(self, decrements: dict[str, int]) -> None:
        """Apply the decrements of a reservation."""

    def increment(self, increments: dict[str, int]) -> None:
        """Apply the increments of a release."""

    def rollback(self) -> None:
        """Discard this transaction (nothing written so far is kept)."""


@runtime_checkable
class StockRepository(Protocol):
    """The stock ledger, read outside a transaction and written inside one."""

    def available_quantities(self, product_ids: Sequence[str]) -> dict[str, int]:
        """Return the current quantity per known product id (missing ids are absent)."""

    def transaction(self) -> AbstractContextManager[StockTransaction]:
        """Open a transaction; commit on clean exit, roll back on any error."""
