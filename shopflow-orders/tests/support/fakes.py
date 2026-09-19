"""In-memory implementations of the three ports.

These fakes exist for the **unit** and **contract** suites: they implement the
ports faithfully (including the guarded status updates, the idempotency keys and
the all-or-nothing reservation) so a use case can be exercised without a store, a
broker or an inventory service. They are never used to claim persistence,
transactionality or durability - those properties are proven by the integration
suite against the real store, the real inventory and the real broker.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.errors import InsufficientStock, InventoryUnavailable, UnknownProduct
from app.domain.order import (
    InventoryReleaseState,
    Order,
    OrderLine,
    OrderStatus,
    RequestedLine,
    order_total,
    utc_now,
)
from app.ports.inventory import (
    AvailabilityItem,
    AvailabilityResult,
    InventoryProduct,
    ReleaseResult,
    ReservationResult,
    release_key,
    reservation_key,
)
from app.ports.order_repository import OrderPage
from app.ports.outbox import BrokerUnavailable, OutboxMessage, OutboxRecord

# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
DEFAULT_PRODUCTS: dict[str, InventoryProduct] = {
    "p-1001": InventoryProduct("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", Decimal("19.99")),
    "p-1002": InventoryProduct("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", Decimal("12.50")),
    "p-1003": InventoryProduct("p-1003", "SF-HOOD-003", "ShopFlow Hoodie", Decimal("49.00")),
    "p-1004": InventoryProduct("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", Decimal("4.25")),
    "p-1005": InventoryProduct("p-1005", "SF-CAP-005", "ShopFlow Cap", Decimal("22.00")),
}

DEFAULT_STOCK: dict[str, int] = {"p-1001": 50, "p-1002": 25, "p-1003": 10, "p-1004": 100, "p-1005": 0}


class FakeInventory:
    """A faithful in-memory ``shopflow-inventory`` (contracts.md §4)."""

    def __init__(
        self,
        *,
        products: dict[str, InventoryProduct] | None = None,
        stock: dict[str, int] | None = None,
    ) -> None:
        self.products = dict(products or DEFAULT_PRODUCTS)
        self.stock = dict(stock or DEFAULT_STOCK)
        self.calls: list[tuple[str, str, tuple[tuple[str, int], ...]]] = []
        self.keys_used: list[str] = []
        self.idempotency_keys: dict[str, str] = {}
        self.reserved: dict[str, tuple[tuple[str, int], ...]] = {}
        #: fault switches: ``availability``, ``reserve``, ``release``, ``product`` or ``all``
        self.unavailable_for: set[str] = set()
        self.insufficient_at_reserve = False
        self.unknown_at_reserve = False
        self.raise_timeout_for: set[str] = set()

    # -- helpers ------------------------------------------------------------
    def make_unavailable(self, *operations: str) -> None:
        self.unavailable_for.update(operations or ("all",))

    def available_again(self) -> None:
        self.unavailable_for.clear()
        self.raise_timeout_for.clear()

    def _check(self, operation: str) -> None:
        if operation in self.unavailable_for or "all" in self.unavailable_for:
            raise InventoryUnavailable(
                f"The inventory service is unavailable for {operation}.",
                details={"operation": operation},
            )
        if operation in self.raise_timeout_for:
            raise InventoryUnavailable(
                f"The inventory service timed out on {operation}.",
                details={"operation": operation, "timeout": True},
            )

    def _record(self, operation: str, order_id: str, lines: Sequence[RequestedLine]) -> None:
        self.calls.append((operation, order_id, tuple((line.product_id, line.quantity) for line in lines)))

    # -- port surface -------------------------------------------------------
    def get_product(self, product_id: str, *, timeout_seconds: float | None = None) -> InventoryProduct:
        self._check("product")
        product = self.products.get(product_id)
        if product is None:
            raise UnknownProduct(product_id)
        return product

    def check_availability(
        self,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> AvailabilityResult:
        self._check("availability")
        self._record("availability", "", lines)
        items: list[AvailabilityItem] = []
        for line in lines:
            if line.product_id not in self.products:
                raise UnknownProduct(line.product_id)
            available = max(int(self.stock.get(line.product_id, 0)), 0)
            items.append(
                AvailabilityItem(
                    product_id=line.product_id,
                    requested=line.quantity,
                    available=available,
                    sufficient=available >= line.quantity,
                )
            )
        return AvailabilityResult(available=all(item.sufficient for item in items), items=tuple(items))

    def reserve(
        self,
        order_id: str,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> ReservationResult:
        self._check("reserve")
        self._record("reserve", order_id, lines)
        self.keys_used.append(reservation_key(order_id))
        if self.unknown_at_reserve:
            raise UnknownProduct(lines[0].product_id)
        if self.insufficient_at_reserve:
            first = lines[0]
            raise InsufficientStock(
                "Requested quantity exceeds available stock.",
                details={"productId": first.product_id, "requested": first.quantity, "available": 0},
            )
        key = reservation_key(order_id)
        body = tuple((line.product_id, line.quantity) for line in lines)
        if key in self.idempotency_keys:
            return ReservationResult(order_id=order_id, reserved=True, replayed=True)
        for line in lines:
            if self.stock.get(line.product_id, 0) < line.quantity:
                raise InsufficientStock(
                    "Requested quantity exceeds available stock.",
                    details={
                        "productId": line.product_id,
                        "requested": line.quantity,
                        "available": max(int(self.stock.get(line.product_id, 0)), 0),
                    },
                )
        for line in lines:
            self.stock[line.product_id] = self.stock.get(line.product_id, 0) - line.quantity
        self.idempotency_keys[key] = str(body)
        self.reserved[order_id] = body
        return ReservationResult(order_id=order_id, reserved=True, replayed=False)

    def release(
        self,
        order_id: str,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> ReleaseResult:
        self._check("release")
        self._record("release", order_id, lines)
        self.keys_used.append(release_key(order_id))
        key = release_key(order_id)
        if key in self.idempotency_keys:
            return ReleaseResult(order_id=order_id, restored=False, replayed=True)
        reserved = self.reserved.get(order_id)
        if reserved is None:
            return ReleaseResult(order_id=order_id, restored=False, replayed=False)
        for product_id, quantity in reserved:
            self.stock[product_id] = self.stock.get(product_id, 0) + quantity
        self.idempotency_keys[key] = str(reserved)
        return ReleaseResult(order_id=order_id, restored=True, replayed=False)

    def is_reachable(self, *, timeout_seconds: float | None = None) -> bool:
        return not ({"all", "health"} & self.unavailable_for)


# --------------------------------------------------------------------------- #
# outbox store and broker
# --------------------------------------------------------------------------- #
class InMemoryOutboxStore:
    """An in-memory ``outbox`` table (publisher-facing plus ``insert``)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def insert_message(self, message: OutboxMessage) -> None:
        """Record a message (the write side the fake order repository calls)."""

        if any(row["message"].event_id == message.event_id for row in self.rows):
            return
        self.rows.append(
            {
                "record_id": f"row-{len(self.rows) + 1}",
                "message": message,
                "published_at": None,
                "attempts": 0,
            }
        )

    def fetch_unpublished(self, *, limit: int) -> Sequence[OutboxRecord]:
        return tuple(
            OutboxRecord(
                record_id=row["record_id"],
                message=row["message"],
                attempts=row["attempts"],
                published_at=row["published_at"],
            )
            for row in sorted(self.rows, key=lambda item: (item["message"].occurred_at, item["record_id"]))
            if row["published_at"] is None
        )[: max(1, int(limit))]

    def mark_published(self, record_id: str, *, published_at: datetime) -> None:
        for row in self.rows:
            if row["record_id"] == record_id:
                row["published_at"] = published_at

    def record_attempt(self, record_id: str, *, failed_at: datetime) -> int:
        for row in self.rows:
            if row["record_id"] == record_id:
                row["attempts"] += 1
                return int(row["attempts"])
        return 0

    def count_unpublished(self) -> int:
        return len([row for row in self.rows if row["published_at"] is None])


class FakeBroker:
    """An in-memory broker that records the messages it accepted."""

    def __init__(self) -> None:
        self.published: list[OutboxMessage] = []
        self.reachable = True
        self.failures_remaining = 0
        self.closed = False
        #: called with the message before it is accepted (used to crash the relay)
        self.before_publish: Callable[[OutboxMessage], None] | None = None

    def publish(self, message: OutboxMessage) -> None:
        if self.before_publish is not None:
            self.before_publish(message)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise BrokerUnavailable("the broker did not confirm the publish")
        self.published.append(message)

    def is_reachable(self) -> bool:
        return self.reachable

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# order repository
# --------------------------------------------------------------------------- #
class InMemoryOrderRepository:
    """A faithful in-memory ``orders``/``order_lines``/``outbox`` store."""

    def __init__(self, *, outbox: InMemoryOutboxStore | None = None) -> None:
        self.orders: dict[str, Order] = {}
        self.outbox = outbox if outbox is not None else InMemoryOutboxStore()
        #: hooks keyed by method name, run at the start of the method
        self.hooks: dict[str, Callable[[], None]] = {}
        self.calls: list[str] = []

    # -- helpers ------------------------------------------------------------
    @property
    def messages(self) -> tuple[OutboxMessage, ...]:
        return tuple(row["message"] for row in self.outbox.rows)

    @property
    def event_types(self) -> list[str]:
        return [row["message"].event_type for row in self.outbox.rows]

    def force_cancel(self, order_id: str, *, at: datetime, reason: str, event: OutboxMessage) -> None:
        """Simulate a competing writer that wins the guarded update."""

        order = self.orders[order_id]
        self.orders[order_id] = order.cancelled(at=at, reason=reason)
        self.outbox.insert_message(event)

    def force_confirm(self, order_id: str, *, at: datetime, event: OutboxMessage) -> None:
        order = self.orders[order_id]
        self.orders[order_id] = order.confirmed(at=at)
        self.outbox.insert_message(event)

    def _hook(self, name: str) -> None:
        hook = self.hooks.get(name)
        if hook is not None:
            hook()

    # -- port surface -------------------------------------------------------
    def create(self, order: Order, *, event: OutboxMessage | None = None) -> Order:
        self.calls.append("create")
        self.orders[order.order_id] = order
        if event is not None:
            self.outbox.insert_message(event)
        return order

    def get(self, order_id: str) -> Order | None:
        self.calls.append("get")
        return self.orders.get(order_id)

    def list_by_customer(
        self,
        customer_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> OrderPage:
        matching = [
            order
            for order in sorted(self.orders.values(), key=lambda item: (item.created_at, item.order_id))
            if order.customer_id == customer_id
        ]
        page = matching[max(0, offset) :]
        if limit is not None:
            page = page[: max(0, limit)]
        return OrderPage(items=tuple(page), total=len(matching))

    def mark_confirmed(self, order_id: str, *, confirmed_at: datetime, event: OutboxMessage) -> bool:
        self.calls.append("mark_confirmed")
        self._hook("mark_confirmed")
        order = self.orders.get(order_id)
        if order is None or order.status is not OrderStatus.CREATED:
            return False
        self.orders[order_id] = order.confirmed(at=confirmed_at)
        self.outbox.insert_message(event)
        return True

    def mark_failed(self, order_id: str, *, failed_at: datetime) -> bool:
        self.calls.append("mark_failed")
        self._hook("mark_failed")
        order = self.orders.get(order_id)
        if order is None or order.status is not OrderStatus.CREATED:
            return False
        self.orders[order_id] = order.failed(at=failed_at)
        return True

    def mark_cancelled(
        self,
        order_id: str,
        *,
        cancelled_at: datetime,
        reason: str,
        event: OutboxMessage,
    ) -> bool:
        self.calls.append("mark_cancelled")
        self._hook("mark_cancelled")
        order = self.orders.get(order_id)
        if order is None or order.status is not OrderStatus.CONFIRMED:
            return False
        self.orders[order_id] = order.cancelled(at=cancelled_at, reason=reason)
        self.outbox.insert_message(event)
        return True

    def mark_shipped(self, order_id: str, *, shipped_at: datetime) -> bool:
        self.calls.append("mark_shipped")
        self._hook("mark_shipped")
        order = self.orders.get(order_id)
        if order is None or order.status is not OrderStatus.CONFIRMED:
            return False
        self.orders[order_id] = order.shipped(at=shipped_at)
        return True

    def set_release_state(self, order_id: str, *, state: InventoryReleaseState) -> bool:
        self.calls.append("set_release_state")
        order = self.orders.get(order_id)
        if order is None or order.inventory_release_state is state:
            return False
        self.orders[order_id] = order.with_release_state(state)
        return True

    def find_pending_releases(self, *, limit: int) -> Sequence[Order]:
        self.calls.append("find_pending_releases")
        return tuple(
            order
            for order in sorted(self.orders.values(), key=lambda item: (item.cancelled_at or item.created_at))
            if order.status is OrderStatus.CANCELLED
            and order.inventory_release_state is InventoryReleaseState.PENDING
        )[: max(1, limit)]

    def find_pending_reservations(self, *, limit: int, older_than: datetime) -> Sequence[Order]:
        self.calls.append("find_pending_reservations")
        return tuple(
            order
            for order in sorted(self.orders.values(), key=lambda item: (item.created_at, item.order_id))
            if order.status is OrderStatus.CREATED and order.created_at < older_than
        )[: max(1, limit)]


def new_order(
    *,
    order_id: str = "0f14de0e-6f2e-4b7a-9f5b-1f3f1d9c1111",
    customer_id: str = "c-1",
    status: OrderStatus = OrderStatus.CONFIRMED,
    created_at: datetime | None = None,
) -> Order:
    """A small order fixture for the fake store."""

    moment = created_at or utc_now()
    lines = (
        OrderLine(
            product_id="p-1001",
            sku="SF-TEE-001",
            name="ShopFlow Classic Tee",
            quantity=2,
            unit_price=Decimal("19.99"),
        ),
    )
    return Order(
        order_id=order_id,
        customer_id=customer_id,
        status=status,
        lines=lines,
        total=order_total(lines),
        created_at=moment,
        updated_at=moment,
        confirmed_at=moment if status is not OrderStatus.CREATED else None,
    )
