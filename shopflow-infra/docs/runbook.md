# ShopFlow Infra — Runbook

Day-2 operations for the local ShopFlow stack. Everything here is a command an
operator can paste; the reasoning lives in `../README.md`.

## 0. Prerequisites

- Docker Engine / Docker Desktop with Compose v2 (`docker compose version`).
- Python 3.12+ on the host for the infra scripts (`python --version`). The
  scripts need no third-party packages; `pip install -r scripts/requirements.txt`
  only adds the optional `httpx` transport.
- Free host ports: `3000`, `8080`, `8001`, `8002`, `8003`, `8081`, `5433–5435`,
  `5672`, `15672` (all overridable in `.env`).
- The five sibling repositories checked out next to this one:
  `../shopflow-inventory`, `../shopflow-orders`, `../shopflow-notifications`,
  `../shopflow-gateway`, `../shopflow-web`.

## 1. Bring the stack up

```bash
docker compose up -d --build          # build + start in dependency order
python scripts/wait_for_health.py --timeout 180
```

What happens, in order: stores and broker become healthy → each service's own
migrations run → the inventory-owned seed runs → each service becomes healthy →
the gateway becomes healthy → the one-shot health gate runs → the web container
starts.

Check the result:

```bash
docker compose ps                     # long-running = "healthy"; jobs = "Exited (0)"
docker compose logs --tail=50 broker-init healthcheck-gate seed-inventory
curl http://localhost:8080/api/products
```

Documented startup budget: **180 s** (`HEALTH_GATE_TIMEOUT`). Measured on this
host (warm cache, fresh volumes) the repository's own containers reach healthy at
~5.6 s (stores), ~6.1 s (emulator) and ~20.7 s (broker), with
`docker compose up -d --wait --build` returning `0` for stores + broker +
emulator in 24.1 s; the five service images add their build and boot time, and the
per-service migrations and the seed run in between.

Note that `docker compose up -d --wait` **exits 1 once a one-shot job container
exits** (compose counts a finished job as not-running) — that is expected here,
and readiness is reported by `docker compose ps` plus
`scripts/wait_for_health.py` / the `healthcheck-gate` service.

## 2. Everyday commands

```bash
make up            # docker compose up -d --build
make health        # wait for every /health endpoint
make migrate       # apply each service's own migrations, in dependency order
make seed          # run + verify the inventory-owned seed
make smoke         # black-box end-to-end verification through the gateway
make smoke-in-stack# same test, executed inside the compose network
make test          # emulator contract tests + infra script unit tests
make ps / logs     # status / follow logs
make reset         # docker compose down -v (destroys all state)
```

`./up.sh` (POSIX) and `.\up.ps1` (Windows) run *up → health → seed → smoke* in one
go, with `--reset` / `-Reset` to drop volumes first and `--no-smoke` /
`-SkipSmoke` to stop after the seed.

Both wrappers fail closed: `./up.sh --help` prints its usage and exits `0`, an
unknown argument prints the error plus usage and exits `2`, and any failing step
stops the wrapper immediately with that step's exit code, so a partially built
stack is never reported ready. `Makefile` targets are optional conveniences
(`make` is not installed on every host) and run the same commands; they are
checked by `scripts/tests/test_makefile.py`.

## 3. Health gate

```bash
python scripts/wait_for_health.py --timeout 180
python scripts/wait_for_health.py --urls http://shopflow-gateway:8000/health   # any endpoint list
```

- Exit `0`: every endpoint answered `200`.
- Exit `1`: budget exceeded — the failing endpoints and their last error are
  printed.
- Exit `2`: bad endpoint specification.

The `healthcheck-gate` compose service runs the same script inside the network
and is a one-shot dependency of `shopflow-web`, so the UI never starts ahead of a
verified backend.

Targets are the published ports by default (`GATEWAY_PORT`, `ORDERS_PORT`,
`INVENTORY_PORT`, `NOTIFICATIONS_PORT`, `PROVIDER_PORT`). `shopflow-web` is not
polled: `contracts.md` defines `/health` for the five backend services and the
emulator only.

## 4. Migrations

```bash
python scripts/run_migrations.py                    # inventory -> orders -> notifications
python scripts/run_migrations.py --only inventory    # a single service
python scripts/run_migrations.py --dry-run           # show the docker commands
```

Each step runs the owning service's own migration command inside its own image
(`alembic upgrade head` by default), so infrastructure never creates a table. The
command is overridable per service with `INVENTORY_MIGRATION_COMMAND`,
`ORDERS_MIGRATION_COMMAND`, `NOTIFICATIONS_MIGRATION_COMMAND`. The script stops
at the first non-zero exit code.

## 5. Seed

```bash
python scripts/seed.py                 # seed, then verify determinism with a second run
python scripts/seed.py --no-verify     # just run the seed command once
python scripts/seed.py --strict        # an unreachable inventory API becomes an error
```

`seed.py` never writes data itself: it runs the `seed-inventory` job, which
executes the inventory repository's own command (`python -m app.seed` by default,
`INVENTORY_SEED_COMMAND` to override). With verification enabled it asserts:

- exactly the five documented products exist, with the documented sku, name and price;
- stock never exceeds the seeded quantities (`p-1005` stays at zero);
- a second run changes nothing (idempotency).

## 6. Smoke test

```bash
python scripts/smoke_test.py                     # from the host, published ports
docker compose run --rm smoke-test               # inside the compose network
python scripts/smoke_test.py --gateway-url http://localhost:8080 --event-timeout 60
```

One printed line per scenario, `PASS n name -> observed values`, and `FAIL n name
-> reason` with a non-zero exit on the first violation; the nine scenarios are
listed in the README. The test is repeatable without a reset: it measures stock
deltas rather than absolute values.

## 7. Reset, restart and cleanup

```bash
docker compose down              # stop and remove containers (volumes kept)
docker compose down -v           # also drops all volumes -> deterministic seed again
docker compose restart notification-provider-emulator   # clear the emulator's in-memory state
docker compose rm -fsv broker-init healthcheck-gate seed-inventory   # remove finished job containers
```

`down -v` is the supported way to get back to a pristine state: three empty
stores, an empty broker, an empty emulator. Run `docker compose up -d --build`
afterwards and the migrations and the deterministic seed are re-applied
automatically.

### Repeat runs and re-applying a single job

`docker compose up -d` is safe to repeat. Long-running services are left alone
(`Running`) and the one-shot jobs are re-executed idempotently; observed with
`broker-init`, which re-ran on a second `up` and exited `0` again after
re-declaring and re-verifying the durable topology.

To re-apply one job on purpose, without touching anything else:

```bash
docker compose run --rm broker-init        # re-ensure the broker topology
docker compose run --rm migrate-inventory  # one service's own migration
docker compose run --rm seed-inventory     # the inventory-owned seed
```

`make migrate` / `make seed` run the same jobs through `scripts/run_migrations.py`
and `scripts/seed.py`, which add the dependency-order loop and the idempotency
verification.

## 8. Logs and diagnostics

```bash
docker compose logs -f --tail=100                     # everything
docker compose logs broker-init                       # topology declaration + verification
docker compose logs healthcheck-gate                  # per-endpoint readiness
docker compose logs shopflow-orders shopflow-notifications
docker compose exec message-broker rabbitmqctl list_queues name durable --no-table-headers
docker compose run --rm broker-init                   # re-ensure the broker topology (idempotent)
curl http://localhost:8081/messages?orderId=<orderId> # what the emulator received
curl http://localhost:15672                           # RabbitMQ management (shopflow/shopflow)
```

## 9. Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `docker compose up` stops with `dependency failed to start: container shopflow-message-broker-1 is unhealthy` | broker still starting, or host memory pressure | `docker compose logs message-broker`; wait and re-run `up`, or raise `retries`/`start_period` for the broker healthcheck |
| `service "migrate-inventory" didn't complete successfully: exit <code>` (or `-orders` / `-notifications`) and `up` exits non-zero | a migration job exited non-zero; nothing that depends on it starts | `docker compose logs migrate-inventory`; the owning service's own migration command is the failing one — override it with `INVENTORY_MIGRATION_COMMAND` / `ORDERS_MIGRATION_COMMAND` / `NOTIFICATIONS_MIGRATION_COMMAND` |
| `service "seed-inventory" didn't complete successfully: exit <code>` | the inventory seed command failed | `docker compose logs seed-inventory`; check `INVENTORY_SEED_COMMAND` and the inventory migrations |
| `could not find an available, non-overlapping IPv4 address pool` / port already allocated | host port conflict | change `*_PORT` in `.env` (e.g. `GATEWAY_PORT=18080`) and re-run `up` |
| `python scripts/wait_for_health.py` times out on one endpoint | that service is unhealthy | `docker compose logs <service>`; check its `DATABASE_URL`/`BROKER_URL` and the store/broker it owns |
| smoke scenario 4 or 7 times out waiting for a message | consumer or provider path broken | `docker compose logs shopflow-notifications`, then `docker compose logs broker-init` and confirm the queue and both routing keys exist |
| smoke scenario 3/6 stock assertion fails | reservation or release did not land exactly once | `docker compose logs shopflow-orders shopflow-inventory`; check the `stock_movements` rows and the release reconciler |
| `shopflow-web` reports `unhealthy` although the SPA is served | the image listens on another port, or answers `>= 400` for `/` | set `WEB_CONTAINER_PORT` to the port the image listens on; the probe tries `wget`/`curl`/`node` on `${WEB_CONTAINER_PORT}`, `3000`, `4173`, `8080`, `80` |
| queue `shopflow.notifications.order-events` is missing | constraints of this host prevented the declarative file from being read | `docker compose run --rm broker-init` (idempotent) and check its exit code; the services are gated on it |
| published ports disappear (nothing on `localhost:8080`) | the network is `internal` (`SHOPFLOW_NETWORK_INTERNAL=true` or the offline override) | remove the override / set `SHOPFLOW_NETWORK_INTERNAL=false`; Docker Engine does not publish ports on internal networks |
| a service cannot resolve a peer name right after switching between the default and offline networks | containers were reconnected instead of recreated | `docker compose down && docker compose up -d` so all containers are created on the new network |
| `docker compose build` fails on `../shopflow-*` | sibling repository not checked out | clone the five repositories next to `shopflow-infra` |
| `docker compose up -d --wait` exits `1` reporting `container <job> exited (0)` | compose counts a finished one-shot job as not-running, even when it succeeded | expected here: judge readiness with `docker compose ps` plus `scripts/wait_for_health.py` (or pass `--wait` only the long-running services) |

## 10. Evidence for a reviewer

```bash
# topology and configuration
docker compose config | findstr DATABASE_URL                # one url per owner
docker compose ps                                           # healthy + exited(0) jobs
docker compose config --format json | python -c "import json,sys;print(sorted(json.load(sys.stdin)['services']))"

# broker contract (§8): durable exchange, durable queue, both routing keys
docker compose exec message-broker rabbitmqctl list_exchanges name type durable --no-table-headers | findstr shopflow
docker compose exec message-broker rabbitmqctl list_queues name durable --no-table-headers
docker compose exec message-broker rabbitmqctl list_bindings source_name destination_name routing_key --no-table-headers | findstr shopflow

# emulator contract (§7), the scripts' own logic, their flows, and the harness
python -m pytest emulator/tests -q          # 25 tests, no container needed
python -m pytest scripts/tests -q           # 112 tests, no docker/services needed
#   tests/test_script_flows.py        migration order + stop-at-first-failure, seed
#                                    idempotency flow, health-gate exit codes, and that
#                                    jobs run as `docker compose run --rm -T <service>`
#   tests/test_smoke_harness.py       the nine scenarios pass on a compliant stack and
#                                    each required violation fails at its own scenario
#   tests/test_makefile.py            make targets/.PHONY/variables and the exact
#                                    command each target runs
#   tests/test_broker_topology.py     the §8 topology declared by the bootstrap job and by
#                                    definitions.json, and that both still agree
#   tests/test_repository_layout.py   the handoff's required layout exists, and the README's
#                                    layout block lists only paths that really exist
#   tests/test_env_template.py        every variable docker compose interpolates is
#                                    documented in .env.example (and vice versa), and every
#                                    contracts.md §11 name appears in the template
#   tests/test_script_dependencies.py the scripts import only the standard library (httpx
#                                    optional) and never a container SDK

# the wrappers themselves
"C:\Program Files\Git\bin\bash.exe" up.sh --help        # usage, exit 0
powershell -NoProfile -File .\up.ps1 -SkipSmoke         # runs the same steps

# store isolation, read-only: each instance owns its own database...
docker compose exec -T orders-db        psql -U shopflow -d shopflow_orders        -c "\l"
docker compose exec -T inventory-db     psql -U shopflow -d shopflow_inventory     -c "\l"
docker compose exec -T notifications-db psql -U shopflow -d shopflow_notifications -c "\l"
# ...and infrastructure created no schema in any of them (before migrations run)
docker compose exec -T orders-db psql -U shopflow -d shopflow_orders -c "\dt"      # "Did not find any relations."

# seeds (§10) and end-to-end behaviour
python scripts/seed.py
python scripts/smoke_test.py
curl http://localhost:8080/api/products

# offline guarantee
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d --build
docker run --rm --network shopflow python:3.12-slim python -c "import socket;s=socket.socket();s.settimeout(4);print(s.connect_ex(('1.1.1.1',443)))"   # 101 = unreachable
docker compose -f docker-compose.yml -f docker-compose.offline.yml run --rm smoke-test

# teardown
docker compose down -v
```
