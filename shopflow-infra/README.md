# ShopFlow Infra

Local Docker runtime for the whole ShopFlow ecosystem: three independent stores,
one broker, the deterministic notification-provider emulator and all five
services, brought up in dependency order by one command, plus a deterministic
inventory seed and a black-box end-to-end smoke test.

`shopflow-infra` is **wiring only**: it builds and runs the other repositories, it
owns the local topology, migration order, seed invocation, health gating and the
cross-service smoke test. It contains **no business rules** and never writes a
business row (the only writer it triggers is the inventory-owned seed command).

- Interfaces produced: the local Docker runtime for every repository, and the
  deterministic notification-provider HTTP contract (`contracts.md` §7).
- Interfaces consumed: each service's Dockerfile, its `/health` and `/ready`
  endpoints, and its own migration and seed commands.

---

## 1. Quick start

```bash
# 1. build every image and start the stack in dependency order
#    (stores + broker healthy -> migrations -> seed -> services -> gateway -> web)
docker compose up -d --build

# 2. wait until every /health endpoint answers (documented budget: 180 s)
python scripts/wait_for_health.py --timeout 180

# 3. apply each service's own migrations (idempotent, dependency order)
python scripts/run_migrations.py

# 4. run the inventory-owned deterministic seed (and verify its idempotency)
python scripts/seed.py
python scripts/seed.py            # second run must be idempotent

# 5. prove the required user-visible behaviours through the public gateway
python scripts/smoke_test.py

# 6. emulator contract tests (no container, no network needed)
python -m pytest emulator/tests -q

# 7. full reset: drops containers *and* volumes, back to the deterministic seed
docker compose down -v
```

`make help`, `./up.sh` and `.\up.ps1` wrap the same sequence
(`./up.sh --reset`, `.\up.ps1 -Reset`, `make up|seed|smoke|reset`).

`docker compose up -d --build` already runs steps 3 and 4 for you through the
`migrate-*` and `seed-inventory` one-shot jobs; steps 3 and 4 are the explicit,
re-runnable equivalents and are idempotent.

The five seeded products are then visible through the public gateway:

```bash
curl http://localhost:8080/api/products     # -> 200 with the 5 products
```

---

## 2. Topology

| Compose service | Source | Host port | Container | Health gate |
| --- | --- | --- | --- | --- |
| `shopflow-web` | `../shopflow-web` | `3000` → `80` | browser SPA | HTTP probe on the serving port (`wget` → `curl` → `node`) |
| `shopflow-gateway` | `../shopflow-gateway` | `8080` → `8000` | public API (§2) | `GET /health` |
| `shopflow-orders` | `../shopflow-orders` | `8001` → `8000` | orders API (§5) | `GET /health` |
| `shopflow-inventory` | `../shopflow-inventory` | `8002` → `8000` | inventory API (§4) | `GET /health` |
| `shopflow-notifications` | `../shopflow-notifications` | `8003` → `8000` | notifications API (§6) | `GET /health` |
| `notification-provider-emulator` | `./emulator` | `8081` → `8000` | provider contract (§7) | `GET /ready` |
| `orders-db` | `postgres:16-alpine` | `5433` → `5432` | orders store (§9) | `pg_isready` |
| `inventory-db` | `postgres:16-alpine` | `5434` → `5432` | inventory store (§9) | `pg_isready` |
| `notifications-db` | `postgres:16-alpine` | `5435` → `5432` | notifications store (§9) | `pg_isready` |
| `message-broker` | `rabbitmq:3.13-management-alpine` | `5672`, `15672` | broker (§8) | `rabbitmq-diagnostics` |
| `broker-init` | `./broker` | — | one-shot topology bootstrap | must exit `0` |
| `migrate-inventory` / `migrate-orders` / `migrate-notifications` | each service repo | — | one-shot `alembic upgrade head` | must exit `0` |
| `seed-inventory` | `../shopflow-inventory` | — | one-shot `python -m app.seed` | must exit `0` |
| `healthcheck-gate` | `./scripts` | — | one-shot `wait_for_health.py` | must exit `0` |
| `smoke-test` *(profile `tools`)* | `./scripts` | — | one-shot `smoke_test.py` | must exit `0` |

Ports are documented, non-secret defaults from `.env.example`; every one of them
can be overridden in `.env` (`GATEWAY_PORT`, `ORDERS_DB_PORT`, …) without editing
the compose file.

### Startup order (dependency-driven, enforced by `depends_on`)

```
orders-db, inventory-db, notifications-db, message-broker, notification-provider-emulator
        │  (service_healthy)
        ├── migrate-inventory ──► seed-inventory ──► shopflow-inventory
        │                                              │
        ├── broker-init ──┬── migrate-orders ──► shopflow-orders
        │                 └── migrate-notifications ──► shopflow-notifications
        │
        └── shopflow-gateway ──► healthcheck-gate ──► shopflow-web
```

Every dependency edge uses a condition: `service_healthy` for long-running
containers and `service_completed_successfully` for the one-shot jobs. A
container that never becomes healthy stops the chain, so a half-built stack can
never look ready.

Long-running containers declare a `healthcheck`; the one-shot jobs are gated by
exit status instead (a completed job is not "healthy", it is *done*), which is
why `docker compose ps` shows them as `Exited (0)`.

### Startup budget

`HEALTH_GATE_TIMEOUT` (default `180` s) is the documented budget enforced by
`scripts/wait_for_health.py` and by the `healthcheck-gate` service.

Measured on this host (warm image cache, fresh volumes) for the containers that
belong to this repository:

| Elapsed | What became healthy |
| --- | --- |
| ~5.6 s | each store (`pg_isready`) |
| ~6.1 s | the provider emulator (`GET /ready`) |
| ~20.7 s | the broker (`check_running` + `check_port_connectivity`) |
| 24.1 s | `docker compose up -d --wait --build` returned `0` for stores + broker + emulator |

The five service images add their own build time (first run) and boot time to
that, and the budget also has to cover per-service migrations, the seed and the
gateway — which is what the 180 s covers.

Two operational notes. `docker compose up -d --wait` **exits 1 as soon as a
one-shot job container exits** (compose counts a finished job as not-running),
even though the job succeeded; the supported readiness signals are
`docker compose ps` and `scripts/wait_for_health.py` / the `healthcheck-gate`
service. And the very first run is slower because every image must be built.

---

## 3. Three isolated stores — no service sees another service's database

One relational instance per owning service, each with its own database name and
its own volume. The exact environment lines of `docker-compose.yml` are:

```yaml
x-database-urls:
  orders:        &orders-database-url        "postgresql://${SHOPFLOW_DB_USER:-shopflow}:${SHOPFLOW_DB_PASSWORD:-shopflow}@orders-db:5432/${ORDERS_DB_NAME:-shopflow_orders}"
  inventory:     &inventory-database-url     "postgresql://${SHOPFLOW_DB_USER:-shopflow}:${SHOPFLOW_DB_PASSWORD:-shopflow}@inventory-db:5432/${INVENTORY_DB_NAME:-shopflow_inventory}"
  notifications: &notifications-database-url "postgresql://${SHOPFLOW_DB_USER:-shopflow}:${SHOPFLOW_DB_PASSWORD:-shopflow}@notifications-db:5432/${NOTIFICATIONS_DB_NAME:-shopflow_notifications}"

x-inventory-environment: &inventory-environment
  DATABASE_URL: *inventory-database-url        # -> inventory-db/shopflow_inventory

x-orders-environment: &orders-environment
  DATABASE_URL: *orders-database-url           # -> orders-db/shopflow_orders

x-notifications-environment: &notifications-environment
  DATABASE_URL: *notifications-database-url    # -> notifications-db/shopflow_notifications
```

Each variable is referenced by exactly one service (and by that service's own
migration/seed job). There is no shared business database and no cross-service
`DATABASE_URL`.

Verify it:

```bash
# every DATABASE_URL rendered by compose, and the service it belongs to
docker compose config | findstr DATABASE_URL

# what a running service actually sees
docker compose exec shopflow-orders        printenv DATABASE_URL   # ...@orders-db:5432/shopflow_orders
docker compose exec shopflow-inventory     printenv DATABASE_URL   # ...@inventory-db:5432/shopflow_inventory
docker compose exec shopflow-notifications printenv DATABASE_URL   # ...@notifications-db:5432/shopflow_notifications

# each instance owns its own database name and volume
docker compose exec orders-db        psql -U shopflow -c "\l"       # shopflow_orders
docker compose exec inventory-db     psql -U shopflow -c "\l"       # shopflow_inventory
docker compose exec notifications-db psql -U shopflow -c "\l"       # shopflow_notifications
docker volume ls | findstr shopflow_                                  # three -db-data volumes + broker-data
```

`down -v` removes all four volumes, so a repeat run starts from the same
deterministic seed.

---

## 4. Broker (`contracts.md` §8)

`message-broker` runs RabbitMQ with:

| Object | Name | Settings |
| --- | --- | --- |
| exchange | `shopflow.orders` | `topic`, durable |
| queue | `shopflow.notifications.order-events` | durable |
| binding | `shopflow.orders` → queue | routing key `order.confirmed` |
| binding | `shopflow.orders` → queue | routing key `order.cancelled` |

Two mechanisms declare the same topology, so the stack is deterministic on every
Docker host:

1. `broker/definitions.json` is loaded by the broker at boot
   (`management.load_definitions` in `broker/rabbitmq.conf`) — the standard
   declarative RabbitMQ approach.
2. `broker-init` is a one-shot job that runs after the broker reports
   `healthy` and **re-declares and verifies** the topology over AMQP, exiting
   non-zero if it cannot. `shopflow-orders` and `shopflow-notifications` start
   only after it succeeds, so a consumer can never race broker bootstrapping nor
   an unread config file.

Verify it:

```bash
docker compose run --rm broker-init                  # idempotent re-run, exits 0
docker compose logs broker-init                      # one line per declared object
docker compose exec message-broker \
  rabbitmqctl list_exchanges name type durable --no-table-headers | findstr shopflow
docker compose exec message-broker \
  rabbitmqctl list_queues name durable --no-table-headers
docker compose exec message-broker \
  rabbitmqctl list_bindings source_name destination_name routing_key --no-table-headers | findstr shopflow
```

RabbitMQ supports publisher confirms natively; confirms are enabled on the
*publisher* side (`shopflow-orders`), so there is nothing to configure here.
The broker never needs the internet.

---

## 5. Provider emulator (`contracts.md` §7)

`notification-provider-emulator` (built from `./emulator`) **replaces the
production notification provider** in the local stack. It implements exactly
`POST /messages`, `GET /messages`, `/health` and `/ready`, and nothing else
(`docs_url`/`openapi_url` are disabled):

- `messageId` is deterministic: `"msg-" + sha256(template + ":" + orderId).hexdigest()[:16]`.
- Idempotent by `Idempotency-Key: order:{orderId}:{template}` — a repeated key
  returns the original outcome and records no second message; the same key with a
  different body returns `409 idempotency_conflict`.
- State is in memory only: the emulator is a **test double, not a system of
  record**, and resetting it between runs is expected and safe
  (`docker compose restart notification-provider-emulator`, or `down -v`).
- It is offline and deterministic: it performs no DNS lookup and no outbound
  request (asserted by `emulator/tests/test_emulator.py`), and never talks to a
  production provider.

```bash
python -m pytest emulator/tests -q                    # 25 contract tests
curl http://localhost:8081/health                     # {"status":"ok"}
curl "http://localhost:8081/messages?orderId=<uuid>"  # test affordance used by the smoke test
```

The suite covers the deterministic `messageId`, replay with the same key, replay
with a different body (`409 idempotency_conflict`), missing/blank key, every
invalid body shape, both templates, filtering by `orderId`, correlation
id echo/generation, the error envelope, **eight concurrent identical replays
recording exactly one message**, and the offline guarantee (an outbound
connection attempt fails the test).

---

## 6. Scripts

| Script | Purpose | Exit behaviour |
| --- | --- | --- |
| `scripts/wait_for_health.py` | polls `/health` on every service until all answer `200` | `0` ready, `1` timeout (names the unhealthy endpoints), `2` bad endpoint spec |
| `scripts/run_migrations.py` | runs each service's own migration tool in the order **inventory → orders → notifications** | `0` all up to head, non-zero at the first failure |
| `scripts/seed.py` | invokes the inventory-owned seed command and (by default) verifies determinism | `0` seeded+verified, `1` seed/verification failure |
| `scripts/smoke_test.py` | end-to-end black-box verification through the public gateway | `0` all scenarios pass, `1` on the first failed assertion |

All four use only the Python standard library; `httpx` (pinned in
`scripts/requirements.txt`) is picked up automatically when installed. The
migration, seed and broker jobs are **not** re-implemented here: the scripts ask
`docker compose run` to execute the owning service's own job container
(`alembic upgrade head`, `python -m app.seed`), which is overridable per service
through `INVENTORY_MIGRATION_COMMAND`, `ORDERS_MIGRATION_COMMAND`,
`NOTIFICATIONS_MIGRATION_COMMAND` and `INVENTORY_SEED_COMMAND`.

Useful flags:

```bash
python scripts/wait_for_health.py --timeout 180 --interval 2
python scripts/run_migrations.py --only inventory,orders --dry-run
python scripts/seed.py --no-verify          # just run the seed command
python scripts/seed.py --strict             # an unreachable inventory API is an error
python scripts/smoke_test.py --event-timeout 60
```

Tests for the scripts themselves (no docker, no services required):

```bash
python -m pytest scripts/tests -q     # 112 tests: contract constants, seed checks, polling/timeout
                                      # logic, the scripts' main() orchestration flows, their
                                      # dependencies, the wrapper/Makefile structure, the broker
                                      # topology of §8, the required repository layout, the
                                      # `.env.example`/compose correspondence, and the smoke
                                      # harness's fail-closed behaviour
python -m pytest emulator/tests scripts/tests -q   # both suites (also `make test`)
python -m pytest -q                   # same two suites from the repository root
```

The flow tests cover what the scripts are *for*, without a docker daemon: the
migration runner's dependency order and its stop-at-first-failure behaviour, the
seed runner's two-pass idempotency proof (including the changed-catalogue,
failed-job, unreachable-API and `--strict` outcomes), the health gate's endpoint
resolution and exit codes, and the fact that a job is executed as
`docker compose run --rm -T <service>` rather than re-implemented in infra.
`scripts/tests/test_makefile.py` checks `make` targets structurally (they exist,
are `.PHONY`, stay tab-indented, reference only defined variables and expand to
exactly the documented commands) because `make` is not installed everywhere —
keeping the wrapper honest even where it cannot be run.

---

## 7. Smoke test (the ecosystem's completion proof)

`scripts/smoke_test.py` drives **the public gateway only** for every business
action and reads the emulator's deterministic test affordance
(`GET /messages`) to count notifications. It never touches a database and never
calls a backend service directly. It prints one line per asserted scenario and
exits non-zero on the first violation:

| # | Scenario | What is asserted |
| --- | --- | --- |
| 1 | list products | `GET /api/products` → `200`, the five seeded ids, sku and price match §10 |
| 2 | create order | `POST /api/orders` → `201`, `status=CONFIRMED`, non-null `confirmedAt`, line echoed |
| 3 | stock decremented | `p-1001` `availableQuantity` is exactly the pre-order value minus the ordered quantity |
| 4 | confirmation message | exactly one `order_confirmation` at the emulator, with the deterministic `messageId` |
| 5 | cancel order | `POST /api/orders/{id}/cancel` → `200`, `status=CANCELLED`, non-null `cancelledAt`, `cancellationReason` echoed |
| 6 | stock restored | `p-1001` returns to its pre-order value (the release is retried until it lands) |
| 7 | duplicate cancellation | repeat cancel changes no stock, keeps a single `cancelledAt`, and produces no second `order_cancellation` message |
| 8 | SHIPPED cannot be cancelled | `POST …/ship` (admin) → `SHIPPED`; `POST …/cancel` → `409 order_shipped`; status stays `SHIPPED` |
| 9 | insufficient stock | order for `p-1005` → `409 insufficient_stock`, error envelope carries a `correlationId`, and no order exists for that customer |

```bash
python scripts/smoke_test.py                  # on the host, through the published ports
docker compose run --rm smoke-test            # same test, inside the compose network
```

Notification counts are asserted with a wait-then-settle window
(`--event-timeout`, default 60 s), so an asynchronous — or wrongly duplicated —
delivery is caught instead of racing the assertion.

The harness is itself verified, so the completion proof cannot pass vacuously.
`scripts/tests/test_smoke_harness.py` runs the nine scenarios against a
contract-faithful fake of the gateway and the emulator (all nine pass, exit `0`,
exactly one line each) and then injects one violation at a time to prove the
proof fails closed: wrong order status, no stock decrement, a duplicated
confirmation, a missing `cancelledAt`, no stock restore, a repeat cancellation
that changes state, a cancellable `SHIPPED` order, an order created for `p-1005`
and a missing `correlationId` — each fails at its own scenario number with exit
`1`. Only a live stack can show that the real services behave; that is what
running the script against the compose stack does.

---

## 8. No outbound internet dependency at runtime

Images are built once (build-time package downloads from public registries are
expected); at runtime every call stays on the compose network: each service
receives only in-network URLs (`INVENTORY_BASE_URL`, `ORDERS_BASE_URL`,
`BROKER_URL`, `PROVIDER_BASE_URL`) and the emulator never leaves the process.

Docker Engine cannot publish ports for containers on an `internal` network, so
the default network keeps its route (the host needs `localhost:8080` for the
public gateway) and the restricted case is an explicit override:

```bash
# start the whole stack with no outbound route at all
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d --build

# services still reach each other...
docker compose run --rm broker-init
# ...and the completion proof runs inside the network:
docker compose -f docker-compose.yml -f docker-compose.offline.yml run --rm smoke-test
```

Measured on this stack: on the internal network a container's
`socket.connect_ex(("1.1.1.1", 443))` returns `101` (`ENETUNREACH`) while the same
probe on the default bridge returns `0`, and internal traffic
(`notification-provider-emulator:8000/health`, `message-broker:5672`,
`*-db:5432`) still works. `SHOPFLOW_NETWORK_INTERNAL=true` in `.env` produces the
same effect without the override file.

---

## 9. Configuration

`contracts.md` §11 names every variable; `.env.example` documents a
credential-free local default for each one plus the infra-owned ports. Copy it to
`.env` to override anything (compose reads `.env` automatically); `.env` is
git-ignored, `.env.example` is not.

| Variable | Used by | Local default |
| --- | --- | --- |
| `DATABASE_URL` | orders, inventory, notifications | owner-specific (`…@orders-db:5432/shopflow_orders`, …) |
| `BROKER_URL` | orders, notifications | `amqp://shopflow:shopflow@message-broker:5672/` |
| `INVENTORY_BASE_URL` | orders, gateway | `http://shopflow-inventory:8000` |
| `ORDERS_BASE_URL` | gateway | `http://shopflow-orders:8000` |
| `PROVIDER_BASE_URL` | notifications | `http://notification-provider-emulator:8000` |
| `REQUEST_TIMEOUT_SECONDS` | gateway, orders, notifications | `5` |
| `RELEASE_RETRY_INTERVAL_SECONDS` | orders | `5` |
| `GATEWAY_BASE_URL` | browser bundle, host scripts | `http://localhost:8080` |
| `SHOPFLOW_PROVIDER_URL` | host scripts only | `http://localhost:8081` |
| `SHOPFLOW_NETWORK_INTERNAL` | compose network | `false` |

**No secrets.** Every value is a throwaway local default (`shopflow`/`shopflow`
for the local stores and broker). Nothing here may ever contain a real
credential.

The template and the compose file are kept in step by
`scripts/tests/test_env_template.py`: every variable compose interpolates must be
documented (and vice versa, apart from the two declared host-script-only inputs),
and every name in `contracts.md` §11 must appear. One consequence worth knowing:
`BROKER_URL` is now read by compose directly, so it must agree with
`BROKER_USER`/`BROKER_PASSWORD`/`BROKER_VHOST` — a mismatch makes `broker-init`
fail loudly instead of letting services authenticate-fail later.

---

## 10. Acceptance criteria → evidence

| # | Criterion | Evidence |
| --- | --- | --- |
| 1 | `docker compose up -d --build` completes, every container healthy in budget | `docker compose ps` (all `healthy`; one-shot jobs `Exited (0)`), `healthcheck-gate`, `python scripts/wait_for_health.py --timeout 180` |
| 2 | three separate stores, no cross-service `DATABASE_URL` | §3 env lines, `docker compose exec … printenv DATABASE_URL`, three separate databases and volumes |
| 3 | `python scripts/seed.py` idempotent | `--verify` runs the seed twice and asserts identical products/stock; prints the five products with stock ≤ the seeded quantities |
| 4 | `python scripts/smoke_test.py` exits `0`, one line per scenario | §7, including the duplicate-cancellation and SHIPPED cases; `pytest scripts/tests -q` proves the same harness returns `0` on a compliant stack and `1` for each required violation |
| 5 | `GET http://localhost:8080/api/products` returns the five products | smoke scenario 1 and the `curl` above |
| 6 | no runtime internet dependency | §8 offline override + the measured `ENETUNREACH`/`0` comparison and in-network smoke run |
| 7 | no business rules in infra | infra only builds/runs images, orders migrations, invokes the inventory-owned seed, gates on health and asserts behaviour; no table is created and no business row is written by infra code |

---

## 11. Repository layout

```
shopflow-infra/
  README.md                        # this file
  docker-compose.yml               # the whole topology
  docker-compose.offline.yml       # network-restricted override (acceptance #6)
  .env.example                     # credential-free local defaults
  .gitignore
  Makefile                         # make up|health|migrate|seed|smoke|test|reset
  pytest.ini                       # bare `pytest` runs both suites
  up.sh / up.ps1                   # one-command bootstrap wrappers
  broker/
    Dockerfile, requirements.txt   # pinned pika lockfile
    bootstrap.py                   # declare + verify the durable topology (§8)
    rabbitmq.conf, definitions.json# declarative topology loaded at broker boot
  emulator/
    Dockerfile, requirements.txt   # pinned runtime lockfile
    requirements-dev.txt, pytest.ini
    app/main.py                    # POST /messages, GET /messages, /health, /ready
    app/store.py                   # in-memory idempotent message store
    tests/test_emulator.py         # 25 contract tests
  scripts/
    Dockerfile, requirements.txt   # one-shot container image for the gate/smoke jobs
    shopflow_http.py               # stdlib-first HTTP helper (httpx when available)
    shopflow_compose.py            # `docker compose run` wrapper for job containers
    wait_for_health.py             # health gate
    run_migrations.py              # inventory -> orders -> notifications
    seed.py                        # inventory-owned seed + idempotency check
    smoke_test.py                  # end-to-end black-box scenarios
    tests/test_scripts.py          # 32 unit tests for the scripts' own logic
    tests/test_smoke_harness.py    # 13 tests: the smoke suite passes on a compliant
                                   # stack and fails closed on every required violation
    tests/test_script_flows.py     # 19 tests: migration order/abort, seed idempotency
                                   # flow, health-gate exit codes, compose job invocation
    tests/test_makefile.py         # 19 tests: Makefile targets, .PHONY, variables,
                                   # expanded commands, referenced files
    tests/test_broker_topology.py  # 10 tests: §8 topology in the declarative file and in
                                   # the bootstrap job, and that the two agree
    tests/test_repository_layout.py# 6 tests: the handoff's required layout exists and the
                                   # README's own layout block only lists real paths
    tests/test_env_template.py     # 7 tests: .env.example ↔ docker compose correspondence
                                   # and the contracts.md §11 variable coverage
    tests/test_script_dependencies.py # 6 tests: the scripts need only the standard library
                                   # and drive the compose CLI instead of a container SDK
    tests/conftest.py              # import bootstrap
  docs/runbook.md                  # day-2 operations, troubleshooting, evidence
```

---

## 12. Interpretation notes

Decisions taken where the handoff, `contracts.md` and the sibling repositories'
handoffs do not fully agree. Each is deliberate and observable in the files.

1. **Service names.** The handoff names the compose services
   `shopflow-inventory`, `shopflow-orders`, `shopflow-notifications`,
   `shopflow-gateway`, `shopflow-web`, and `contracts.md` §11 addresses them by
   those names — so those are the canonical service names here. Some sibling
   handoffs' snippets use shorter names (`inventory`, `orders`, `gateway`); each
   service is therefore *also* reachable on the compose network under its short
   name through a network alias, and the store/broker/emulator names
   (`orders-db`, `inventory-db`, `notifications-db`, `message-broker`,
   `notification-provider-emulator`) are exactly the ones every handoff shares.
2. **One-shot jobs and healthchecks.** Every long-running container declares a
   `healthcheck`. Migration, seed, broker-bootstrap and health-gate jobs are
   gated on `service_completed_successfully` rather than on health, because a
   finished job is done, not healthy.
3. **Duplicate cancellation tolerance.** Scenario 7 accepts `200` (the safe
   repeat documented by the orders handoff) or `409 order_not_cancellable`
   (listed for cancel in `contracts.md` §5) and asserts the handoff's actual
   requirement in both cases: one `cancelledAt` value, unchanged stock and no
   second notification. The observed status is printed in the evidence line.
4. **`SHOPFLOW_PROVIDER_URL` for host runs.** `PROVIDER_BASE_URL` is the
   in-network name used by `shopflow-notifications`; the host-side smoke test
   defaults to the emulator's *published* port instead, so sourcing `.env` can
   never point a host script at an unresolvable service name.
5. **Web image port and probe.** `shopflow-web` serves the SPA on whatever port
   its own Dockerfile chooses, so the published mapping defaults to `3000 → 80`
   (set `WEB_CONTAINER_PORT` if it differs) and the healthcheck tries the
   configured port plus the usual static-server ports (`3000`, `4173`, `8080`,
   `80`). Because this is the only container whose base image this repository does
   not own, the probe uses whichever HTTP client that image actually ships.
   Measured: `nginx:alpine` has `wget`+`curl`, `nginx:latest` (Debian) has **only
   `curl`**, `node:20-alpine` has `wget`+`node`, `node:20-slim` has **only
   `node`**. All four return `0` against a container that is serving, and a
   tool-less image with nothing listening returns `1` — so the gate neither
   misses a served app nor passes a dead one.
6. **Internal network vs published ports.** See §8: the strict offline mode is
   the documented override rather than the default, because Docker Engine will
   not publish ports on an internal network and the host must reach
   `localhost:8080`.
