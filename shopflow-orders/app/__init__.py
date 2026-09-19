"""shopflow-orders: the ShopFlow order lifecycle and outbox owner.

Hexagonal layout (contracts.md §5, §8, §9, §11):

* ``app/domain``    - the order aggregate, its status machine and the cancellation
  policy. Pure Python: nothing here imports FastAPI, SQLAlchemy or the broker client.
* ``app/usecases``  - the four operations of the service (create, cancel, ship, read),
  expressed against the ports only.
* ``app/ports``     - the interfaces the core needs: order repository, inventory,
  outbox/broker.
* ``app/adapters``  - the driven adapters: PostgreSQL store, inventory HTTP client,
  RabbitMQ publisher.
* ``app/api``       - the FastAPI routers of contracts.md §5.
* ``app/workers``   - the background relays: the outbox publisher and the release
  reconciler.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
