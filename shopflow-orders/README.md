# shopflow-orders

The ShopFlow **order lifecycle owner**: `orders-api` (contracts.md §5), the order
aggregate and its status machine, the cancellation rules, and the transactional
outbox that publishes `order.confirmed` / `order.cancelled` to the broker
(contracts.md §8).

This repository owns the `orders`, `order_lines` and `outbox` tables of
contracts.md §9 and is their only writer. It never writes catalog or stock data: it
asks `shopflow-inventory` (contracts.md §4) to check availability and to
reserve/release stock.

## Architecture

Hexagonal (ports and adapters). The core depends only on the ports, and neither
`app/domain` nor `app/usecases` imports `fastapi`, `sqlalchemy` or the broker client
(acceptance criterion 12 - enforced by `tests/unit/test_architecture.py`).

```
app/
  main.py                      composition root: routes, /health, /ready, worker startup
  api/orders.py, api/actions.py HTTP surface of contracts.md §5
  domain/order.py              order aggregate, status machine, money/time helpers
  domain/cancellation.py       cancellation policy (reason + status rules)
  domain/errors.py             the error codes of contracts.md §12
  usecases/create_order.py     availability -> persist -> reserve -> confirm (+ event)
  usecases/cancel_order.py     guarded cancel (+ event) -> release -> RELEASED/PENDING
  usecases/ship_order.py       admin mark-as-SHIPPED
  usecases/get_order.py        order detail and order list
  ports/order_repository.py    the store port (status change and outbox row in one transaction)
  ports/inventory.py           availability / get product / reserve / release
  ports/outbox.py              outbox record, outbox store, message broker
  adapters/postgres_order_repository.py, adapters/postgres_outbox.py
  adapters/inventory_http_client.py
  adapters/rabbitmq_publisher.py
  events/contracts.py          the §8 envelope (eventId, eventType, lines, reason, ...)
  workers/outbox_publisher.py  relay with publisher confirms, backoff, restart safety
  workers/release_reconciler.py pending releases + ambiguous reservations
  db/models.py, db/session.py  the owned store (contracts.md §9)
migrations/versions/0001_orders_schema.py
```

### Durability decisions

* **Status change + outbox row are one transaction.** Every status write is a
  guarded `UPDATE ... WHERE id = :id AND status = :expected` on the same session
  that inserts the outbox row, so a writer that loses a race writes no event.
* **The outbox is the durable intent.** A broker outage leaves the rows
  unpublished; the relay retries with backoff and never loses an event. A crash
  between the broker's confirmation and the `published_at` write republishes the
  same `eventId`, which is what contracts.md §8 relies on (at-least-once delivery,
  consumers deduplicate by `eventId`; `shopflow-notifications` also deduplicates per
  order and notification type).
* **Cancellation is durable.** The cancellation commits with
  `inventory_release_state = 'PENDING'`; the release is attempted right after the
  commit and retried by the reconciler with the same idempotency key
  (`order:{orderId}:release`) until the inventory reports it. Stock is restored
  exactly once.
* **A `CREATED` order is never reported as confirmed.** A failed or ambiguous
  reservation leaves the order `CREATED` and answers `503 inventory_unavailable`;
  the reconciler retries the reservation with the same idempotency key
  (`order:{orderId}:reserve`) and confirms only after a reservation is known to have
  succeeded.

## Running it

```bash
pip install -r requirements-dev.txt

# start the owned store and the consumed inventory (from the sibling infra repo)
docker compose -f ../shopflow-infra/docker-compose.yml up -d orders-db inventory-db shopflow-inventory

# apply this repository's own migration, then run the service
DATABASE_URL=postgresql://shopflow:shopflow@localhost:5433/shopflow_orders alembic upgrade head
DATABASE_URL=postgresql://shopflow:shopflow@localhost:5433/shopflow_orders \
INVENTORY_BASE_URL=http://localhost:8002 \
BROKER_URL=amqp://shopflow:shopflow@localhost:5672/ \
uvicorn app.main:app --port 8001
```

Inside the composed stack `shopflow-infra` builds this repository's `Dockerfile`,
runs `alembic upgrade head` in the `migrate-orders` job, waits on `/health` and
publishes the service on `ORDERS_PORT` (default `8001`).

The two relays can also be run standalone (they hold no state in memory):

```bash
python -m app.workers.outbox_publisher
python -m app.workers.release_reconciler
```

## API (contracts.md §5)

| Method | Path | Success | Errors |
| --- | --- | --- | --- |
| POST | `/orders` | `201` order view | `400 validation_error`, `409 insufficient_stock`, `503 inventory_unavailable` |
| GET | `/orders/{orderId}` | `200` order view | `404 not_found` |
| GET | `/orders?customerId=&limit=&offset=` | `200 {items, total}` | `400 validation_error` |
| POST | `/orders/{orderId}/cancel` | `200` order view (`CANCELLED`) | `400 invalid_reason`, `404 not_found`, `409 order_shipped`, `409 order_not_cancellable` |
| POST | `/orders/{orderId}/ship` | `200` order view (`SHIPPED`) | `404 not_found`, `409 invalid_status_transition` |
| GET | `/health`, `/ready` | `200` | `503` (store or broker unreachable) |

Statuses: `CREATED`, `CONFIRMED`, `FAILED`, `SHIPPED`, `CANCELLED`. Allowed
transitions: `CREATED → CONFIRMED`, `CREATED → FAILED`, `CONFIRMED → SHIPPED`,
`CONFIRMED → CANCELLED`; anything else is `409 invalid_status_transition`.
`inventory_release_state` is `NONE`, `PENDING` or `RELEASED`.

## Tests

```bash
python -m pytest tests/unit -q          # domain, policy, use cases, workers, architecture rule
python -m pytest tests/contract -q      # HTTP + event contract, no store needed
docker compose -f ../shopflow-infra/docker-compose.yml up -d orders-db inventory-db shopflow-inventory
python -m pytest tests/integration -q   # real PostgreSQL store + real shopflow-inventory
docker compose -f ../shopflow-infra/docker-compose.yml up -d --build   # whole stack
python -m pytest tests/e2e -q           # black-box workflow through the public gateway
```

`tests/integration` uses the real store (`ORDERS_TEST_DATABASE_URL`, default
`postgresql://shopflow:shopflow@localhost:5433/shopflow_orders`), the real
`shopflow-inventory` HTTP API (`INVENTORY_BASE_URL`, default
`http://localhost:8002`) and, for fault injection, a controllable inventory stub
server on a real socket: an unreachable inventory, a lost reservation race, an
ambiguous reserve and a failing release are therefore reproduced without switching
the store for a fake. When the real inventory service is not running, the suite warns
and starts that same stub (contracts.md §4 and the §10 seed) for the inventory side -
the store and the broker are never substituted. It drives the outbox relay, the
reconciler and the store itself, so it assumes it is the **only writer of the orders
store**: run it against `orders-db` alone (that is what the command above does), not
while the `shopflow-orders` container of the composed stack is serving the same
database.

`tests/e2e` is the opposite: it needs the whole composed stack and drives only the
public gateway and the provider emulator. It also runs shopflow-infra's own
black-box smoke test (`../shopflow-infra/scripts/smoke_test.py`), which is the
ecosystem's completion proof. `shopflow-web` must be built too if you use
`docker compose up -d --build` for the whole file; the backend services can be
started without it:
`docker compose -f ../shopflow-infra/docker-compose.yml up -d --build shopflow-gateway shopflow-orders shopflow-notifications notification-provider-emulator healthcheck-gate`.

## Configuration (contracts.md §11)

See `.env.example`. `DATABASE_URL`, `BROKER_URL`, `INVENTORY_BASE_URL`,
`REQUEST_TIMEOUT_SECONDS` and `RELEASE_RETRY_INTERVAL_SECONDS` are the documented
variables; the file also lists optional tuning knobs (pool size, outbox poll
interval, batch sizes, reservation grace). No secret is stored in this repository.
