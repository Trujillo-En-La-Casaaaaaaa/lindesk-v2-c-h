# shopflow-inventory

Product catalogue and stock owner of the ShopFlow ecosystem. It implements the
inventory HTTP API of `contracts.md` §4 (consumed by `shopflow-gateway` for
catalogue reads and by `shopflow-orders` for availability, reservation and
release) and owns exactly three tables of its own PostgreSQL store: `products`,
`stock` and `stock_movements` (contracts.md §9).

The service exists to make one guarantee hold under load and under retries:

> ShopFlow never oversells. Stock is decremented exactly once per order and
> restored exactly once per cancellation.

## Architecture: hexagonal (ports and adapters)

```
app/api/            HTTP adapters (FastAPI routers: contracts.md §4 routing, error envelope)
        │  depends on ▼
app/usecases/       application core: list_products, get_product, check_availability,
                    reserve_stock, release_stock  (all decision logic lives here)
        │  depends on ▼
app/ports/          protocols the core needs: CatalogRepository, StockRepository, StockTransaction
app/domain/         entities and invariants: Product, StockLine, StockLevel, StockMovement, errors
        ▲  implemented by
app/adapters/       PostgreSQL adapters: postgres_catalog, postgres_stock (SQLAlchemy, row locking)
app/db/             SQLAlchemy rows (contracts.md §9) and the engine/session factory
app/main.py         composition root: settings (§11) -> adapters -> FastAPI app factory
app/config.py       environment configuration (§11)
app/seed.py         deterministic seed command (§10): `python -m app.seed`
```

Dependency rule, enforced by `tests/unit/test_architecture.py` (statically **and**
by importing the core in an interpreter where `fastapi`, `sqlalchemy` and
`pydantic` are blocked):

* `app/domain` and `app/usecases` import **no** web framework and **no** database
  library; they depend on `app/ports` protocols only;
* route handlers contain no business logic - they translate HTTP into a use-case
  call and map domain error codes (contracts.md §12) onto HTTP statuses;
* there is exactly one writable owner per datum: this repository writes the
  catalogue, the ledger and the movement log, and nothing else.

## HTTP interface (contracts.md §4)

| Method | Path | Body | Success | Errors |
| --- | --- | --- | --- | --- |
| GET | `/products?limit=&offset=` | — | `200 { items, total }` | `400` |
| GET | `/products/{productId}` | — | `200 { productId, sku, name, description, price, currency, availableQuantity, inStock }` | `404 not_found` |
| POST | `/inventory/availability` | `{ lines: [{ productId, quantity }] }` | `200 { available, items: [{ productId, requested, available, sufficient }] }` | `400`, `404 unknown_product` |
| POST | `/inventory/reservations` | `{ orderId, lines: [{ productId, quantity }] }` | `200 { orderId, reserved, replayed }` | `400`, `404 unknown_product`, `409 insufficient_stock`, `409 idempotency_conflict` |
| POST | `/inventory/releases` | `{ orderId, lines: [{ productId, quantity }] }` | `200 { orderId, restored, replayed }` | `400`, `409 idempotency_conflict` |
| GET | `/health` | — | `200 { "status": "ok" }` | — |
| GET | `/ready` | — | `200 { "status": "ready" }` | `503` |

Writes require the header `Idempotency-Key: order:{orderId}:reserve` (or
`:release`); a missing or mismatched key is `400 validation_error`. Every error is
returned as the shared envelope of contracts.md §1, with `X-Correlation-Id`
echoed unchanged (empty when the caller did not send one):

```json
{ "error": { "code": "insufficient_stock", "message": "...", "correlationId": "", "details": {} } }
```

No other route exists: documentation routes are disabled and
`tests/contract/test_surface_contract.py` asserts the exact endpoint set.

```bash
curl -s http://localhost:8002/products | jq '.total, .items[0]'
curl -s -X POST http://localhost:8002/inventory/reservations \
     -H 'content-type: application/json' -H 'Idempotency-Key: order:o-1:reserve' \
     -d '{"orderId":"o-1","lines":[{"productId":"p-1001","quantity":2}]}'
# -> {"orderId":"o-1","reserved":true,"replayed":false}
```

## Consistency and failure semantics

`reserve_stock` and `release_stock` are the only writers of stock, and each is a
single transaction:

* **All-or-nothing.** Every line is verified under row locks inside one
  transaction before a single unit moves, so a partially reserved order cannot
  exist. An insufficient line rolls the whole command back: no stock change and
  no `stock_movements` row (acceptance 6).
* **The store is the idempotency authority.** The movement row is inserted first
  with `INSERT ... ON CONFLICT (order_id, operation) DO NOTHING RETURNING id`.
  `unique(order_id, operation)` (contracts.md §9) therefore decides whether a
  command is a replay - not process memory - so the guarantee survives restarts,
  crashed workers and concurrent duplicate retries (see
  `tests/integration/test_durability_and_recovery.py` and the four-way concurrent
  retry test in `test_reservation_concurrency.py`):
  * insert accepted, stock sufficient -> decrement, `reserved: true, replayed: false`;
  * insert skipped, same body -> `reserved: true, replayed: true`, no stock change;
  * insert skipped, different body -> `409 idempotency_conflict`.
* **Concurrency.** Reservations lock the affected `stock` rows with
  `SELECT ... FOR UPDATE`, always in the same (product id) order, so two orders
  racing for the last unit are serialized: exactly one commits, the loser is
  told `409 insufficient_stock`, and the ledger lands on `0`. `CHECK (quantity >= 0)`
  is the second line of defence; a violation is translated to the domain error
  instead of leaking a database error.
* **Releases are safe no-ops.** A release for an order that was never reserved
  returns `200 { restored: false, replayed: false }` and discards its guard row,
  so a retry after an ambiguous failure can never fail and never restore twice.
  A repeated release returns `{ restored: false, replayed: true }` without
  touching stock.
* **Releases are bounded.** The recorded reservation payload - not the retried
  request body - decides how much is restored, so a release can never raise a
  product above its pre-reservation level (acceptance 7). Products the order never
  reserved are ignored.
* **Bodies.** Duplicate product ids inside one request are aggregated, and the
  canonical (product-id sorted) line list is what is compared for idempotency, so
  a reordered retry is still recognised as the same body. Unknown fields in a body
  are ignored (forward compatibility with sibling services); unknown *products*
  are `404 unknown_product`.

## Owned store and migrations

This repository owns the schema; `shopflow-infra` only invokes
`alembic upgrade head` with this service's `DATABASE_URL`.

| Table | Purpose | Invariants |
| --- | --- | --- |
| `products` | catalogue (contracts.md §10) | `id` primary key, `sku` unique |
| `stock` | the ledger, one row per product | `CHECK (quantity >= 0)`, FK to `products` |
| `stock_movements` | `reserve`/`release` records | `unique(order_id, operation)`, `operation in ('reserve','release')` |

```bash
export DATABASE_URL=postgresql://shopflow:shopflow@localhost:5434/shopflow_inventory
python -m alembic upgrade head          # create/migrate the three tables
python -m alembic current               # -> 0001_inventory_schema (head)
```

`tests/integration/test_migrated_schema.py` asserts the applied schema (tables,
columns, the never-negative check, the uniqueness rule, the FK) and proves the
store itself refuses a negative quantity and a duplicate movement.

## Deterministic seed (contracts.md §10)

```bash
python -m app.seed        # documented entry point (shopflow-infra: seed-inventory)
```

| productId | sku | name | price | seeded stock |
| --- | --- | --- | --- | --- |
| `p-1001` | `SF-TEE-001` | ShopFlow Classic Tee | 19.99 | 50 |
| `p-1002` | `SF-MUG-002` | ShopFlow Ceramic Mug | 12.50 | 25 |
| `p-1003` | `SF-HOOD-003` | ShopFlow Hoodie | 49.00 | 10 |
| `p-1004` | `SF-STICK-004` | ShopFlow Sticker Pack | 4.25 | 100 |
| `p-1005` | `SF-CAP-005` | ShopFlow Cap | 22.00 | 0 |

The command is idempotent and never inflates stock: products are upserted by
primary key (`sku` stays unique, so no duplicates can appear) and stock is set
with `GREATEST(current, documented)`, so a drained catalogue converges back to
the documented levels while no existing quantity is ever raised above them.
`p-1005` stays at zero, which keeps the insufficient-stock path deterministic.
The movement log is not touched by the seed.

## Configuration (contracts.md §11)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://shopflow:shopflow@inventory-db:5432/shopflow_inventory` | the owned store; the only credential-bearing value, and it is never stored in the repository |
| `PORT` | `8000` | HTTP port (shopflow-infra publishes `8002 -> 8000`) |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_RECYCLE_SECONDS` | `5`, `5`, `10`, `1800` | connection pool tuning |

`.env.example` documents the local (throwaway, credential-free) values; nothing
is read from a file at runtime. No production internet service is required.

## Running it

```bash
python -m pip install -r requirements-dev.txt         # pinned lockfiles

# the store (shopflow-infra owns the compose file)
docker compose -f ../shopflow-infra/docker-compose.yml up -d inventory-db

# configuration comes from the environment only (contracts.md §11), exactly like
# the sibling services: copy .env.example for the host values, then export it.
export DATABASE_URL=postgresql://shopflow:shopflow@localhost:5434/shopflow_inventory

python -m alembic upgrade head
python -m app.seed
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`docker compose` itself injects the in-network `DATABASE_URL`
(`...@inventory-db:5432/shopflow_inventory`, the §11 default) into the service,
the `migrate-inventory` job and the `seed-inventory` job, so nothing has to be
configured for the composed stack.

Container image (built from this directory; used three ways by
`shopflow-infra`: the service, the `migrate-inventory` job and the
`seed-inventory` job):

```bash
docker build -t shopflow-inventory .
docker run --rm -e DATABASE_URL=... shopflow-inventory alembic upgrade head
docker run --rm -e DATABASE_URL=... shopflow-inventory python -m app.seed
```

## Tests

```bash
python -m pytest tests/unit -q          # domain + use cases with port doubles
python -m pytest tests/contract -q      # HTTP contract of contracts.md §4 (no store needed)
docker compose -f ../shopflow-infra/docker-compose.yml up -d inventory-db
python -m pytest tests/integration -q   # real PostgreSQL: migrations, locking, concurrency, restarts
python -m app.seed && python -m app.seed
```

* `tests/unit` - domain invariants and every use case against in-process doubles
  of the ports, including all-or-nothing, replay, conflict and bounded-release
  cases, plus the architecture rules (`test_architecture.py`).
* `tests/contract` - the exact request/response shapes, error codes, statuses,
  correlation echo, the exact route set and `/health` + `/ready` semantics.
* `tests/integration` - the real store: applied migration and its constraints,
  catalogue persistence, reservation/release flows, seven-way concurrent
  oversell attempts, four-way concurrent retries of the same command, reversed
  line order (deadlock check), restarts (new engine/app instance), readiness
  failure and recovery, the seed command and the acceptance walk.

The doubles live in `tests/support/fakes.py`; production wiring never falls back
to them (`tests/contract/test_surface_contract.py::test_the_default_app_is_the_production_composition_root`).

| Handoff acceptance criterion | Evidence |
| --- | --- |
| 1. five seeded products with stock/`inStock` | `test_acceptance_criteria.py::test_acceptance_1_catalog`, `test_catalog_persistence.py`, `test_catalog_contract.py` |
| 2. reserve 2 -> 48, replay -> 48/`replayed: true` | `test_acceptance_criteria.py::test_acceptance_2_reserve_once_and_replay`, `test_reservation_flow.py` |
| 3. concurrent reservations: one 200, one 409, never negative | `test_acceptance_criteria.py::test_acceptance_3_concurrent_reservations_never_oversell`, `test_reservation_concurrency.py` |
| 4. release restores once, repeated release changes nothing | `test_acceptance_criteria.py::test_acceptance_4_release_once_and_replay`, `test_release_flow.py` |
| 5. release of a never reserved order is a `200` no-op | `test_acceptance_criteria.py::test_acceptance_5_release_of_a_never_reserved_order`, `test_release_flow.py` |
| 6. `p-1005` -> `409` and no movement row | `test_acceptance_criteria.py::test_acceptance_6_zero_stock_reservation`, `test_reservation_flow.py` |
| 7. release never exceeds the pre-reservation value | `test_acceptance_criteria.py::test_acceptance_7_release_never_exceeds_the_pre_reservation_value`, `test_release_flow.py` |
| 8. seed twice -> five products, documented levels | `test_acceptance_criteria.py::test_acceptance_8_seed_twice_leaves_the_documented_catalogue`, `test_seed_idempotency.py` |
| 9. `domain/` and `usecases/` import no framework | `test_acceptance_criteria.py::test_acceptance_9_the_core_imports_no_framework`, `tests/unit/test_architecture.py` |

Ruff (line length 120, `E,F,I,UP,B,SIM`) is configured in `pyproject.toml`:
`python -m ruff check app migrations tests`.

## Repository layout

```
README.md  Dockerfile  .env.example  .dockerignore  pyproject.toml
requirements.txt  requirements-dev.txt  alembic.ini
app/
  main.py                     FastAPI app factory, /health, /ready, error envelope
  api/products.py             GET /products, GET /products/{productId}
  api/inventory.py            POST /inventory/availability|reservations|releases
  domain/product.py           product entity + validation
  domain/stock.py             ledger, lines, movements, never-negative invariant
  domain/errors.py            domain errors and their contracts.md §12 codes
  usecases/catalog.py         list_products, get_product
  usecases/availability.py    check_availability
  usecases/reserve.py         reserve_stock (idempotent, all-or-nothing)
  usecases/release.py         release_stock (idempotent, bounded, safe no-op)
  ports/repositories.py       port protocols
  adapters/postgres_catalog.py  SQLAlchemy catalogue adapter
  adapters/postgres_stock.py    SQLAlchemy ledger adapter with row locking
  db/models.py  db/session.py   owned rows + engine/session factory
  config.py                     environment configuration
  seed.py                       deterministic seed command
migrations/                 Alembic environment and versions/0001_inventory_schema.py
tests/unit  tests/contract  tests/integration  tests/support  tests/conftest.py
```

## Scope lock

In scope: product catalogue, product inventory, availability validation, stock
reservation (decrement), stock release (restore), deterministic seed data. Out of
scope by handoff, and deliberately absent: pricing engines, discounts,
promotions, warehouses, suppliers, backorders, returns, shipping, notifications,
order lifecycle and cancellation rules, store provisioning (`shopflow-infra`) and
the gateway (`shopflow-gateway`). This service never reads or writes orders or
notifications data.
