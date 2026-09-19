"""In-process doubles of the two ports, used by the unit and contract suites.

These are *test doubles*: the production composition root only ever wires the
PostgreSQL adapters (``app.adapters``), and no runtime path can fall back to this
module. The doubles live in the test tree on purpose, so the unit and contract
suites can run without a database while the integration suite proves the real
transaction, locking and durability behaviour against PostgreSQL.

The double mirrors the guarantees the real adapter gets from the store:

* one transaction at a time per store (the guard emulates ``SELECT ... FOR
  UPDATE`` serialization);
* a recorded ``(order_id, operation)`` is unique, so a replay is detected at the
  store, not in the use case;
* a decrement below zero raises :class:`InsufficientStock`, like the
  ``CHECK (quantity >= 0)`` of the real ledger.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy.exc import OperationalError

from app.domain.errors import InsufficientStock, UnknownProduct
from app.domain.product import Product
from app.domain.stock import StockLine, StockMovement
from app.ports.repositories import CatalogRepository, StockRepository, StockTransaction
from app.seed import SEED_PRODUCTS


class FakeCatalogRepository(CatalogRepository):
    """Read model backed by a dictionary, ordered by product id like the store."""

    def __init__(self, products: Sequence[Product]) -> None:
        self._products: dict[str, Product] = {product.product_id: product for product in products}

    def list_products(self, *, limit: int | None, offset: int) -> tuple[tuple[Product, ...], int]:
        ordered = [self._products[key] for key in sorted(self._products)]
        total = len(ordered)
        page = ordered[offset:] if offset else ordered
        if limit is not None:
            page = page[:limit]
        return tuple(page), total

    def get_product(self, product_id: str) -> Product | None:
        return self._products.get(product_id)


class FakeInventoryStore:
    """The state a double-backed service shares across requests."""

    def __init__(self, products: Sequence[Product], stock: dict[str, int]) -> None:
        self.guard = threading.RLock()
        self.products: dict[str, Product] = {product.product_id: product for product in products}
        self.stock: dict[str, int] = dict(stock)
        self.movements: dict[tuple[str, str], StockMovement] = {}

    @classmethod
    def seeded(cls) -> FakeInventoryStore:
        """A store holding exactly the deterministic seed of contracts.md §10."""

        products = [entry.product for entry in SEED_PRODUCTS]
        stock = {entry.product.product_id: entry.quantity for entry in SEED_PRODUCTS}
        return cls(products=products, stock=stock)

    def quantity_of(self, product_id: str) -> int:
        with self.guard:
            return int(self.stock.get(product_id, 0))

    def snapshot(self) -> dict[str, int]:
        with self.guard:
            return dict(self.stock)

    def movement_keys(self) -> set[tuple[str, str]]:
        with self.guard:
            return set(self.movements)

    def movements_for(self, order_id: str, operation: str) -> list[StockMovement]:
        with self.guard:
            movement = self.movements.get((order_id, operation))
            return [movement] if movement is not None else []


class FakeStockRepository(StockRepository):
    """Stock ledger backed by :class:`FakeInventoryStore`."""

    def __init__(self, store: FakeInventoryStore) -> None:
        self._store = store

    @property
    def store(self) -> FakeInventoryStore:
        return self._store

    def available_quantities(self, product_ids: Sequence[str]) -> dict[str, int]:
        with self._store.guard:
            return {
                product_id: int(self._store.stock[product_id])
                for product_id in dict.fromkeys(product_ids)
                if product_id in self._store.stock
            }

    @contextmanager
    def transaction(self) -> Iterator[FakeStockTransaction]:
        transaction = FakeStockTransaction(self._store)
        with transaction.scope():
            yield transaction


class FakeStockTransaction(StockTransaction):
    """One transaction of the double; commits on clean exit, rolls back on error."""

    def __init__(self, store: FakeInventoryStore) -> None:
        self._store = store
        self._pending: dict[str, int] = {}
        self._pending_movement: StockMovement | None = None
        self.rolled_back = False
        self._ended = False

    @contextmanager
    def scope(self) -> Iterator[None]:
        self._store.guard.acquire()
        try:
            yield
        except BaseException:
            self._end(commit=False)
            raise
        else:
            self._end(commit=not self.rolled_back)

    # -- port surface ------------------------------------------------------
    def record_movement(self, movement: StockMovement) -> bool:
        if (movement.order_id, movement.operation) in self._store.movements:
            return False
        self._pending_movement = movement
        return True

    def find_movement(self, order_id: str, operation: str) -> StockMovement | None:
        return self._store.movements.get((order_id, operation))

    def lock_levels(self, product_ids: Sequence[str]) -> dict[str, int]:
        return {
            product_id: int(self._store.stock[product_id])
            for product_id in sorted(set(product_ids))
            if product_id in self._store.stock
        }

    def decrement(self, decrements: dict[str, int]) -> None:
        for product_id in sorted(decrements):
            amount = int(decrements[product_id])
            if product_id not in self._store.stock:
                raise UnknownProduct(product_id)
            current = self._current(product_id)
            if amount > current:
                raise InsufficientStock(product_id, amount, current)
            self._pending[product_id] = self._pending.get(product_id, 0) - amount

    def increment(self, increments: dict[str, int]) -> None:
        for product_id in sorted(increments):
            amount = int(increments[product_id])
            if product_id not in self._store.stock:
                raise UnknownProduct(product_id)
            self._pending[product_id] = self._pending.get(product_id, 0) + amount

    def rollback(self) -> None:
        self.rolled_back = True

    # -- internals ---------------------------------------------------------
    def _current(self, product_id: str) -> int:
        return int(self._store.stock.get(product_id, 0)) + self._pending.get(product_id, 0)

    def _end(self, *, commit: bool) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            if commit:
                for product_id, delta in self._pending.items():
                    self._store.stock[product_id] = int(self._store.stock[product_id]) + delta
                if self._pending_movement is not None:
                    movement = self._pending_movement
                    self._store.movements[(movement.order_id, movement.operation)] = movement
        finally:
            self._pending.clear()
            self._pending_movement = None
            self._store.guard.release()


def stock_lines(*pairs: tuple[str, int]) -> tuple[StockLine, ...]:
    """Convenience builder for request lines."""

    return tuple(StockLine(product_id=product_id, quantity=quantity) for product_id, quantity in pairs)


class _StubConnection:
    """Minimal connection double for the readiness probe."""

    def __enter__(self) -> _StubConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


class HealthyEngine:
    """An engine double whose store always answers (readiness ``200``)."""

    def connect(self) -> _StubConnection:
        return _StubConnection()

    def dispose(self) -> None:
        return None


class UnreachableEngine:
    """An engine double whose store never answers (readiness ``503``)."""

    def connect(self) -> _StubConnection:
        raise OperationalError("SELECT 1", {}, Exception("inventory store unreachable"))

    def dispose(self) -> None:
        return None
