"""Driven adapters of the orders hexagon.

* ``postgres_order_repository`` - the owned order store;
* ``postgres_outbox`` - the owned transactional outbox (write side and relay side);
* ``inventory_http_client`` - the consumed inventory HTTP interface (contracts.md §4);
* ``rabbitmq_publisher`` - the broker client of contracts.md §8, with publisher confirms.
"""

from app.adapters.inventory_http_client import InventoryHttpClient
from app.adapters.postgres_order_repository import PostgresOrderRepository
from app.adapters.postgres_outbox import PostgresOutbox
from app.adapters.rabbitmq_publisher import RabbitMqPublisher

__all__ = [
    "InventoryHttpClient",
    "PostgresOrderRepository",
    "PostgresOutbox",
    "RabbitMqPublisher",
]
