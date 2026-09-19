"""Ports of the orders hexagon.

The core (``app/domain`` + ``app/usecases``) depends only on these interfaces, so
it can be exercised with fakes and never imports an adapter, FastAPI, SQLAlchemy
or the broker client.

* ``order_repository`` - the writable owner of the order data and of the
  transactional outbox rows written next to a status change;
* ``inventory`` - the consumed ``shopflow-inventory`` HTTP interface
  (contracts.md §4);
* ``outbox`` - the outbox record, the publisher-facing outbox store and the
  message broker of contracts.md §8.
"""

from app.ports.inventory import (
    AvailabilityItem,
    AvailabilityResult,
    InventoryPort,
    InventoryProduct,
    ReleaseResult,
    ReservationResult,
    release_key,
    reservation_key,
)
from app.ports.order_repository import OrderPage, OrderRepository
from app.ports.outbox import (
    BrokerUnavailable,
    MessageBroker,
    OutboxMessage,
    OutboxRecord,
    OutboxStore,
)

__all__ = [
    "AvailabilityItem",
    "AvailabilityResult",
    "BrokerUnavailable",
    "InventoryPort",
    "InventoryProduct",
    "MessageBroker",
    "OrderPage",
    "OrderRepository",
    "OutboxMessage",
    "OutboxRecord",
    "OutboxStore",
    "ReleaseResult",
    "ReservationResult",
    "release_key",
    "reservation_key",
]
