# ShopFlow Gateway

The single public HTTP surface of the ShopFlow storefront (`shopflow-gateway`).

The gateway is a layered edge adapter: it validates client requests, propagates
correlation ids, routes each request to the owning service, normalizes every
error into one envelope, guards the administrative route and exposes health and
readiness. The browser reaches the backend only through this service.

- **Owns:** nothing durable. No database, no cache, no business rules, no state
  between requests, so it scales horizontally.
- **Consumes:** `shopflow-orders` (contracts.md §5) and `shopflow-inventory`
  (contracts.md §4) over HTTP/JSON.
- **Produces:** the public API of contracts.md §2, consumed only by
  `shopflow-web`.

## Components

| Component | Module | Responsibility |
| --- | --- | --- |
| public-http-server | `app/main.py` | FastAPI app factory, exception handlers, lifespan |
| request-validator | `app/validation/schemas.py` | explicit request models, validated before any upstream call |
| route-registry | `app/routing/route_registry.py` | public route → owning service, upstream path, retry class |
| correlation-middleware | `app/middleware/correlation.py` | `X-Correlation-Id` in/out, context var for handlers |
| admin-route-guard | `app/security/admin_guard.py` | `X-ShopFlow-Role: admin` guard for the ship route |
| upstream-client | `app/upstream/client.py` | HTTP calls with timeout, bounded retry, correlation forwarding |
| error-normalizer | `app/upstream/errors.py` | every failure → the single error envelope of contracts.md §1 |
| gateway-config | `app/config.py` | environment configuration only, no secrets |
| gateway-health | `app/routes/health.py` | `/health` liveness and `/ready` upstream checks |

## Public API (contracts.md §2)

| Method | Path | Upstream | Success | Notes |
| --- | --- | --- | --- | --- |
| GET | `/api/products?limit=&offset=` | inventory `GET /products` | `200` list | validated pagination |
| GET | `/api/products/{productId}` | inventory `GET /products/{id}` | `200` product | `404 not_found` |
| POST | `/api/orders` | orders `POST /orders` | `201` order view | body validated first; never retried |
| GET | `/api/orders/{orderId}` | orders `GET /orders/{id}` | `200` order view | `404 not_found` |
| GET | `/api/orders?customerId=` | orders `GET /orders` | `200` order list | `customerId` required |
| POST | `/api/orders/{orderId}/cancel` | orders `POST /orders/{id}/cancel` | `200` cancelled order | reason validated first; never retried |
| POST | `/api/orders/{orderId}/ship` | orders `POST /orders/{id}/ship` | `200` shipped order | **admin role required**; never retried |
| GET | `/health` | — | `200 {"status":"ok"}` | liveness, calls nothing |
| GET | `/ready` | orders + inventory `/health` | `200 {"status":"ready","upstreams":{...}}` | `503` when an upstream is down |

Success responses are proxied byte-for-byte (status, body and upstream
`content-type`), so business semantics and fields are never rewritten.

Every response carries `X-Correlation-Id`: the client value when supplied,
otherwise a generated UUID. The same value is forwarded unchanged on every
upstream call and echoed in every error envelope.

## Error envelope (contracts.md §1)

```json
{ "error": { "code": "insufficient_stock", "message": "…", "correlationId": "…", "details": {} } }
```

Rules implemented by `app/upstream/errors.py`:

- An upstream envelope with a known code (§12) is passed through with its code
  **and** its HTTP status (`409 insufficient_stock`, `409 order_shipped`,
  `409 order_not_cancellable`, `409 invalid_status_transition`,
  `409 idempotency_conflict`, `404 not_found`, `404 unknown_product`,
  `400 invalid_reason`, `503 inventory_unavailable`, …).
- Business messages are preserved; messages of technical codes
  (`internal_error`, `upstream_unavailable`, `inventory_unavailable`) are always
  the gateway's own wording so a broken upstream cannot leak hostnames.
- Unknown upstream codes, malformed or non-JSON error bodies and unexpected
  statuses become `500 internal_error` with a generic message. Stack traces,
  upstream hostnames and internal identifiers are never returned.
- Timeouts and connection errors become `503 upstream_unavailable` with
  `details.service`; `502/503/504` responses without a parseable envelope are
  treated the same way.
- Gateway-side rejections reuse the same envelope: `400 validation_error`,
  `400 invalid_reason`, `403 forbidden`, `404 not_found`
  (unknown path or method).

## Behaviour

**Validation before routing.** `POST /api/orders` requires a non-empty
`customerId` and at least one line with a positive integer `quantity`;
`POST /api/orders/{orderId}/cancel` requires a `reason` (empty, whitespace-only
or longer than 200 characters is `400 invalid_reason`); `GET /api/orders`
requires a non-empty `customerId`; `limit`/`offset` must be non-negative
integers. Unknown body fields are rejected. A rejected request never reaches a
backend.

**Correlation.** `X-Correlation-Id` is accepted or generated, stored in a
context variable, forwarded unchanged upstream and echoed on the response,
including on errors produced by the gateway itself.

**Upstream client.** Timeout from `REQUEST_TIMEOUT_SECONDS`. Idempotent GETs get
one bounded retry (`GET_RETRY_ATTEMPTS`, default 1, capped at 2) on transport
failure or `502/503/504`. State-changing POSTs are **never** retried, so the
gateway cannot amplify a cancellation or an order creation. A client-supplied
`Idempotency-Key` is forwarded unchanged and never invented.

**Administrative guard.** `POST /api/orders/{orderId}/ship` requires
`X-ShopFlow-Role: admin` (surrounding whitespace is trimmed). Anything else is
`403 forbidden` and no backend is called, because the guard runs before routing.

**Health and readiness.** `/health` is liveness only and contacts nothing.
`/ready` probes both owning services' `/health` concurrently: `200` with an
`upstreams` map when both answer 2xx, otherwise `503 upstream_unavailable` with
`details.upstreams` naming the unreachable service.

**Statelessness.** The gateway holds no database connection, no cache and no
durable state; `app.state` contains only the settings and the route registry,
and the process depends on no database client (see `tests/unit/test_statelessness.py`).

## Configuration

Environment variables only (contracts.md §11); no secrets, no defaults for real
credentials. See `.env.example`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ORDERS_BASE_URL` | `http://shopflow-orders:8000` | base url of `shopflow-orders` |
| `INVENTORY_BASE_URL` | `http://shopflow-inventory:8000` | base url of `shopflow-inventory` |
| `REQUEST_TIMEOUT_SECONDS` | `5` | upstream request timeout (must be > 0) |
| `GET_RETRY_ATTEMPTS` | `1` | bounded retries for idempotent GETs (0–2) |

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# local development (host: use the compose service names or localhost ports)
ORDERS_BASE_URL=http://localhost:8001 INVENTORY_BASE_URL=http://localhost:8002 \
  uvicorn app.main:app --host 0.0.0.0 --port 8080

# container
docker build -t shopflow-gateway .
docker run --rm -p 8080:8000 -e ORDERS_BASE_URL=http://shopflow-orders:8000 shopflow-gateway
```

`shopflow-infra` runs this image and waits on `/health` before declaring the
stack ready. The browser only ever talks to this service; internal service
ports stay private.

## Tests

```bash
python -m pytest tests/unit -q          # gateway internals (no servers)
python -m pytest tests/contract -q      # public API behaviour ("harness" upstreams)
python -m pytest tests/integration -q   # end-to-end over real HTTP
python -m pytest -q                     # everything
```

- **`tests/unit`** – configuration, route registry and the exact public
  surface, validation models, correlation middleware, error normalizer,
  upstream client (timeout/retry/correlation via `httpx.MockTransport`), admin
  guard, statelessness (AC 9).
- **`tests/contract`** – black-box public contract: statuses, envelopes, headers,
  pass-through of codes, validation-before-call, retry evidence, readiness.
  This suite needs the in-process harness upstreams (fault injection and request
  logs); it skips with an explicit reason when live mode is enabled.
- **`tests/integration`** – the nine acceptance criteria of the handoff plus full
  order lifecycle, cancellation idempotency and concurrent order creation,
  driven over real sockets. It runs in both modes below.

### Upstream services in the suites

The two sibling repositories are separate deliverables, so the suites start
contract-faithful implementations of their HTTP interfaces for the gateway to
route to: `tests/support/upstreams/inventory.py` (contracts.md §4 with the owned
schema and seed data of §9/§10) and `tests/support/upstreams/orders.py`
(contracts.md §5). They run as independent uvicorn servers behind real HTTP, so
timeouts, retries, error codes and concurrency are exercised over a real network
path; SQLite stands in for PostgreSQL because the gateway itself must not depend
on a store.

**Live mode** points the same integration suite at the real services:

```bash
docker compose -f ../shopflow-infra/docker-compose.yml up -d \
  orders-db inventory-db inventory orders gateway

# ORDERS_BASE_URL / INVENTORY_BASE_URL must be reachable from the test host
# (the ports published by shopflow-infra for shopflow-orders / shopflow-inventory)
SHOPFLOW_GATEWAY_LIVE_UPSTREAMS=1 \
ORDERS_BASE_URL=http://localhost:8001 \
INVENTORY_BASE_URL=http://localhost:8002 \
python -m pytest tests/integration -q
```

In live mode the fixtures verify that both services answer `/health` and then
assert the same criteria against them, using gateway-side call logging plus
backend state reads (`GET /products/{id}`, `GET /orders/{orderId}`) as evidence.
If a service is unreachable the fixtures fail with the URL and the command to
start it, instead of silently skipping.

### Evidence for "no upstream call"

Acceptance criteria 3 and 6 require proof that a rejected request never reaches a
backend. Both mechanisms are asserted: the gateway's own structured upstream-call
log (authoritative in every mode, and additionally checked to be non-empty by
`tests/contract/test_failure_modes_contract.py`) and, in harness mode, the
upstream request log, plus a backend state check (no order exists / the order is
still `CONFIRMED`).

## Decisions and interpretations

- `docs_url`, `redoc_url` and `openapi_url` are disabled so the gateway exposes
  exactly the routes of contracts.md §2 and nothing else.
- `/ready` returns `503` with the single error envelope and the diagnostics in
  `error.details.upstreams`, so infra can keep waiting on the status code while
  operators see which service is down.
- An upstream failure without a parseable envelope is mapped by status:
  `502/503/504` → `503 upstream_unavailable`, `400` → `validation_error`,
  `401/403` → `forbidden`, `404` → `not_found`, anything else (including `409`
  and `500`) → `500 internal_error`. A `409` is never guessed into a business
  code: the client must receive the real code from the owning service.
- `X-ShopFlow-Role` is matched exactly (`admin`, whitespace-trimmed); role
  variants such as `Admin` are rejected to fail closed.
- Requests to unknown paths or with unsupported methods are normalized to the
  envelope (`404`/`405` with code `not_found`).
- A non-JSON body on a `2xx` upstream response is treated as an unknown upstream
  failure (`500 internal_error`) instead of being proxied.
- A client-supplied `Idempotency-Key` is forwarded unchanged; the gateway never
  generates one, because reservation idempotency belongs to the services.
- Business rules, persistence, notification delivery, UI and authentication
  providers stay out of scope (see the handoff).

## Lockfiles

`requirements.txt` and `requirements-dev.txt` are fully pinned (including
transitive dependencies) and contain no database client. `pyproject.toml` pins
the same versions and holds the pytest and ruff configuration.
