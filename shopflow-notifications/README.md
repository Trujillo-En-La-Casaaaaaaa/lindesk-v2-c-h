# shopflow-notifications

**Owner of customer notifications in the ShopFlow ecosystem.** This service consumes
`order.confirmed` and `order.cancelled` events and delivers **exactly one**
order-confirmation and **exactly one** order-cancellation notification per order -
even under at-least-once delivery, consumer restarts and provider retries.

It is a hexagonal (ports and adapters) service: `app/domain` and `app/handlers` are
plain Python and import neither FastAPI, SQLAlchemy, Alembic, psycopg2 nor the broker
client (asserted by `tests/unit/test_architecture.py`).

* node id: `shopflow-notifications`
* interfaces consumed: the broker and the event envelope of `contracts.md` §8, and the
  notification-provider HTTP contract of `contracts.md` §7
* interfaces provided: `GET /health`, `GET /ready`, `GET /notifications?orderId=`
  (`contracts.md` §6)
* data owned: `notifications` and `processed_events` (`contracts.md` §9) - no other
  service may write here, and this service writes nowhere else

## Scope

In scope: order-confirmation notification, order-cancellation notification,
notification orchestration, deduplication, bounded retry and dead-lettering.

Out of scope (deliberately not implemented): SMS/email preference management, marketing
or campaign messages, templates beyond the two required, unsubscribe handling,
notification scheduling, a customer-preferences store, real provider integration.
Order lifecycle and event production belong to `shopflow-orders`; the provider emulator
and the broker belong to `shopflow-infra`.

## Architecture

| Component (handoff) | Module |
| --- | --- |
| `notification-api` | `app/main.py`, `app/api/notifications.py` |
| `order-event-consumer` | `app/consumer/consumer.py` |
| `event-router` | `app/consumer/router.py` |
| `confirmation-handler` | `app/handlers/confirmation.py` |
| `cancellation-handler` | `app/handlers/cancellation.py` |
| `consumer-dedup-guard` + orchestration | `app/handlers/orchestrator.py` |
| `template-renderer` | `app/domain/templates.py` |
| `retry-policy` | `app/domain/retry.py` |
| notification domain model | `app/domain/notification.py` |
| `notification-repository-port` | `app/ports/repository.py` |
| `provider-port` | `app/ports/provider.py` |
| `postgres-notification-repository` | `app/adapters/postgres_repository.py` |
| `provider-http-client` | `app/adapters/provider_http_client.py` |
| `notifications-config` | `app/config.py` |
| store rows / engine | `app/db/models.py`, `app/db/session.py` |

`app/handlers/orchestrator.py` holds the shared orchestration (the dedup guard, the
delivery and the terminal failure) so the two event-type handlers stay thin and no
business rule is duplicated; `app/domain/retry.py` holds the one bounded-backoff
definition used by both retrying layers.

## The delivery guarantee

Delivery is at-least-once; **exactly once is a logical effect** produced by two
deduplication checks, by the unique constraint of the store, and by the provider's
idempotency key:

1. **`processed_events` by `event_id`** - a redelivered event whose record was already
   committed is skipped. This is the check that catches a crash *after* the commit but
   *before* the acknowledgement.
2. **`notifications` by `(order_id, notification_type)`** - a second event with a
   different `eventId` for the same order is skipped, so an order can never receive two
   confirmations or two cancellations. The skipped event id is remembered so its own
   redeliveries short-circuit at check one.
3. **`Idempotency-Key: order:{orderId}:{template}`** (`contracts.md` §7) - a retry after
   an ambiguous failure returns the original `messageId` instead of recording a second
   message. The provider payload is a pure function of the *order content* (order id,
   customer id, lines, total, and for a cancellation the reason), never of the event
   metadata, so a replayed key always carries an identical body.
4. **One transaction** - the notification row and the processed-event row are committed
   together; two concurrent deliveries of the same logical notification are settled by
   the store (`unique (order_id, notification_type)`), one wins and the other is
   reported as a skip.

If the notification record is committed but the acknowledgement is lost, the redelivery
is skipped by check 1 and the queue drains.

### Retry and dead-lettering (handoff #1)

* The consumer acknowledges **only after the record is committed** (`ack` after commit,
  never before).
* `CONSUMER_PREFETCH` (1-10) bounds the unacknowledged deliveries a consumer holds.
* A **retryable** failure (connection error, timeout, `5xx`, store hiccup) republishes
  the message to the retry queue with an incremented `x-shopflow-attempt` header and a
  per-message TTL; when the TTL expires the broker dead-letters it back to
  `shopflow.orders` (routing key preserved - verified against the broker in
  `tests/integration/test_broker_topology.py`). That is a *delayed requeue*: the attempt
  counter lives in the durable message, so it survives a restart, and the delay makes a
  hot loop impossible.
* When the budget (`CONSUMER_MAX_DELIVERY_ATTEMPTS`) is exhausted - or the failure is
  permanent (`4xx`, unsupported event type/version, malformed envelope) - the message is
  published to `shopflow.notifications.order-events.dead` with its failure metadata and
  **only then acknowledged**. If that publish cannot be confirmed, the delivery is
  `nack`ed with requeue instead, so a message is never silently dropped.
* The terminal state is a `notifications` row with `status='FAILED'`, `attempts` equal
  to the attempts really made and `provider_message_id = NULL`. The §9 schema has no
  error column, so the failure **reason** is preserved where it cannot be lost: in the
  dead-letter message headers (`x-shopflow-failure-reason`, `x-shopflow-failed-at`,
  `x-shopflow-event-id`, `x-shopflow-attempt`, `x-shopflow-failure-recorded`) and in the
  structured log line keyed by `eventId`.

### Recipient addressing

`contracts.md` §8 carries `customerId` only and this service owns no preferences store,
so the recipient is derived deterministically: `{customerId}@{NOTIFICATION_RECIPIENT_DOMAIN}`
(a `customerId` that already looks like an address is used verbatim).

## HTTP surface (contracts.md §6)

| Method | Path | Behaviour |
| --- | --- | --- |
| GET | `/health` | `200 {"status":"ok"}` - liveness only, no upstream contacted |
| GET | `/ready` | `200 {"status":"ready","upstreams":{"store","broker","provider":true},"consumer":"running"}`; `503 {"status":"not_ready", ...}` as soon as one upstream refuses |
| GET | `/notifications?orderId=` | `200 {items:[{orderId, notificationType, status, providerMessageId, createdAt}], total}`; `400 validation_error` when `orderId` is missing (contracts.md §1 envelope, `X-Correlation-Id` echoed) |

The API documentation routes are disabled: contracts.md §6 defines exactly these three
routes and nothing else is exposed (`tests/contract/test_surface_contract.py`).

The consumer runs **inside the API process** (a background thread started by the
lifespan), because `shopflow-infra` runs one process per service. It can also run on its
own with `python -m app.consumer.consumer`.

## Configuration (contracts.md §11)

Everything is read from the environment; no credential is stored in code. See
`.env.example` for the full list.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://shopflow:shopflow@notifications-db:5432/shopflow_notifications` | owned store |
| `BROKER_URL` | `amqp://shopflow:shopflow@message-broker:5672/` | broker |
| `PROVIDER_BASE_URL` | `http://notification-provider-emulator:8000` | provider (§7) |
| `REQUEST_TIMEOUT_SECONDS` | `5` | provider call and readiness probes |
| `PORT` | `8000` | HTTP port |
| `CONSUMER_ENABLED` | `true` | run the consumer in this process |
| `CONSUMER_PREFETCH` | `5` | unacknowledged deliveries (1-10) |
| `CONSUMER_MAX_DELIVERY_ATTEMPTS` | `3` | bounded retry bound |
| `CONSUMER_RETRY_BACKOFF_SECONDS` / `..._MAX_SECONDS` | `1` / `30` | retry backoff |
| `CONSUMER_RECONNECT_INTERVAL_SECONDS` | `5` | broker reconnect interval |
| `PROVIDER_MAX_ATTEMPTS` | `3` | provider-level retry bound |
| `PROVIDER_BACKOFF_SECONDS` / `..._MAX_SECONDS` | `0.5` / `5` | provider backoff |
| `NOTIFICATION_RECIPIENT_DOMAIN` | `notifications.shopflow.local` | deterministic addressing |
| `BROKER_EXCHANGE` / `BROKER_QUEUE` / `BROKER_ROUTING_KEYS` | `shopflow.orders` / `shopflow.notifications.order-events` / `order.confirmed,order.cancelled` | contract binding (§8) |
| `BROKER_RETRY_EXCHANGE` / `BROKER_RETRY_QUEUE` | `shopflow.notifications.retry` / `shopflow.notifications.order-events.retry` | retry objects this service owns |
| `BROKER_DEAD_LETTER_EXCHANGE` / `BROKER_DEAD_LETTER_QUEUE` | `shopflow.notifications.dead-letter` / `shopflow.notifications.order-events.dead` | dead letters this service owns |

Invalid configuration (wrong scheme, prefetch outside 1-10, a backoff ceiling below its
base, no routing keys) fails at startup instead of looping at runtime.

## Store and migrations

`notifications` and `processed_events` exactly as `contracts.md` §9 declares them;
migration `0001_notifications_schema` creates them (and drops them again on
`downgrade`). This repository owns its schema: `shopflow-infra` only *invokes*
`alembic upgrade head` with the service's own `DATABASE_URL`.

```bash
alembic upgrade head        # inside the image, cwd /app
```

## Running it locally

```bash
# 1. dependencies (store, broker, provider emulator) - from the shopflow-infra checkout
docker compose -f ../shopflow-infra/docker-compose.yml up -d \
    notifications-db message-broker notification-provider-emulator

# 2. schema
DATABASE_URL=postgresql://shopflow:shopflow@localhost:5435/shopflow_notifications alembic upgrade head

# 3. service + consumer (host ports: store 5435, broker 5672, emulator 8081)
DATABASE_URL=postgresql://shopflow:shopflow@localhost:5435/shopflow_notifications \
BROKER_URL=amqp://shopflow:shopflow@localhost:5672/ \
PROVIDER_BASE_URL=http://localhost:8081 \
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The whole ecosystem (this service included) is brought up by
`shopflow-infra`: `docker compose up -d --build` builds this image, runs
`migrate-notifications`, then starts `shopflow-notifications` on port 8003.

## Tests

| Suite | Needs infrastructure | What it proves |
| --- | --- | --- |
| `tests/unit` | no | domain model, deterministic templates, retry policy, handler orchestration and dedup guard, provider adapter request/retry logic, consumer decisions, topology declaration, ack-after-commit order, architecture rules (criterion 8) |
| `tests/contract` | no | HTTP surface and error envelope (§1/§6), diagnostic item shape, event envelope (§8), provider body/headers (§7), store schema (§9) |
| `tests/integration` | store, broker, emulator | migrations and migrated schema, repository atomicity/rollback/concurrency against PostgreSQL, provider adapter against the real emulator, end-to-end delivery, broker topology and the delayed requeue, diagnostic endpoint over a real socket, readiness |
| `tests/failure` | store, broker, emulator | duplicate delivery (criteria 2, 3), provider outage with bounded retries + dead-lettering + replay (criterion 6), permanent rejection, and **two real process crashes** (after provider delivery, after the commit) with restart and no second message (criterion 5) |

```bash
python -m pytest tests/unit -q
python -m pytest tests/contract -q
docker compose -f ../shopflow-infra/docker-compose.yml up -d \
    notifications-db message-broker notification-provider-emulator
python -m pytest tests/integration tests/failure -q
```

`DATABASE_URL` / `BROKER_URL` / `PROVIDER_BASE_URL` are honoured first and fall back to
the host-published ports of the same dependencies, so the suites run from a workstation
as well as inside the compose network. A missing dependency fails the suite with the
command that starts it - nothing is skipped.

### Fault injection (test only, inert by default)

`SHOPFLOW_FAULT_CRASH_AFTER_PROVIDER_SEND=1` and `SHOPFLOW_FAULT_CRASH_AFTER_COMMIT=1`
make the consumer process exit hard inside the two crash windows of the handoff, so the
failure suite can prove recovery from a real crash instead of guessing at timing. Both
switches are off unless they are set to an enabling value, and the production paths are
unchanged by them.

### Acceptance criteria → evidence

| Criterion | Evidence |
| --- | --- |
| 1. one `order.confirmed` → one message `template=order_confirmation` + one `SENT` row | `tests/integration/test_delivery_end_to_end.py::test_one_order_confirmed_event_delivers_exactly_one_confirmation` |
| 2. the same `eventId` again → no additional message | `tests/failure/test_duplicate_delivery.py::test_the_same_event_id_delivered_twice_produces_one_message` |
| 3. a different `eventId`, same order → no second message | `...::test_a_second_event_id_for_the_same_order_produces_no_second_message`, `...::test_the_duplicate_never_reaches_the_provider` |
| 4. one `order.cancelled` with a reason → one `template=order_cancellation` message containing the reason + one row | `tests/integration/test_delivery_end_to_end.py::test_one_order_cancelled_event_delivers_exactly_one_cancellation`, `...::test_the_cancellation_body_carries_the_event_reason` |
| 5. crash after provider delivery, before ack, then restart → no second message | `tests/failure/test_crash_after_delivery.py` (real child process, exit code 9) and `tests/failure/test_crash_after_commit.py` |
| 6. provider down → retries, `FAILED`, dead letter; no duplicates afterwards | `tests/failure/test_provider_outage.py` |
| 7. `GET /notifications?orderId=` returns types and provider message ids | `tests/integration/test_diagnostic_endpoint.py`, `tests/contract/test_notifications_api_contract.py` |
| 8. `domain/` and `handlers/` import none of fastapi/sqlalchemy/broker client | `tests/unit/test_architecture.py` (static + a fresh interpreter) |

## Deliberate design notes

* **The payload excludes event metadata** on purpose: the provider is idempotent per
  `order:{orderId}:{template}`, so two events describing the same order must render the
  same body, otherwise a replay would answer `409 idempotency_conflict` instead of
  returning the original `messageId`.
* **A `FAILED` record is terminal.** The uniqueness rule of §9 means a failed
  notification is never silently retried into a second message; the dead-letter message
  and the log line carry the reason for an operator.
* **The contract queue is declared exactly as `shopflow-infra` declares it** (durable, no
  extra arguments) so the two declarations are equivalent and neither can fail with
  `406 precondition failed`.
