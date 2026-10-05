# OmniGuardian

Async FastAPI inventory allocation service using PostgreSQL as its authoritative ledger,
Redis/ARQ for background delivery, and a transactional outbox for recoverable channel pushes.
Python 3.11+; the container uses Python 3.12.

## Start

Install Docker Engine/Desktop with Compose v2, then run:

```sh
docker compose up --build -d
```

Compose runs migrations and seeds a local demo before starting the API. Open
[Swagger UI](http://localhost:8000/docs), choose **Authorize**, and enter
`local-demo-key-change-before-deploying` (or set `DEMO_API_KEY` before starting).
The demo has warehouse `EH`, SKU `DEMO-SKU`, SOH=10, B2B=5, and AFS=5.
Re-running the seed preserves inventory. Services bind only to localhost.

```sh
docker compose exec api python -m scripts.concurrency_demo
docker compose --profile test run --rm test
docker compose logs -f api worker
docker compose down
```

The concurrency script creates a unique product and warehouse, starts 50 HTTP requests,
and asserts **5 successful reservations, 45 HTTP 409 OUT_OF_STOCK responses, and 5 protected
B2B units**. It prints measured median/p95 latency and retains the demonstration records.
The integration suite uses actual PostgreSQL and deletes only its own generated tenant records.
The demo requires an admin API key because it provisions its own inventory.

## API

Every `/api/v1` route requires `X-API-Key`. Tenant identity comes exclusively from the hashed
API key record. Roles: `reader`, `allocator`, `admin`; admin includes allocation and read access.
Health endpoints are public. Amounts are integer units; money uses fixed decimal precision.

```sh
curl -X POST http://localhost:8000/api/v1/allocate \
  -H 'X-API-Key: local-demo-key-change-before-deploying' \
  -H 'Content-Type: application/json' \
  -d '{"order_id":"shopify-1001","channel":"shopify","idempotency_key":"delivery-1001","line_items":[{"sku":"DEMO-SKU","warehouse_code":"EH","quantity":1}]}'

curl http://localhost:8000/api/v1/inventory/DEMO-SKU \
  -H 'X-API-Key: local-demo-key-change-before-deploying'
```

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/api/v1/allocate` | Atomically reserve an entire order |
| GET | `/api/v1/allocations/{id}` | Read the current allocation state |
| POST | `/api/v1/allocations/{id}/confirm` | Confirm while retaining the stock hold |
| POST | `/api/v1/allocations/{id}/cancel` | Release an unfulfilled order exactly once |
| POST | `/api/v1/allocations/{id}/fulfill` | Remove shipped units from SOH and release their holds |
| GET | `/api/v1/inventory/{sku}` | Authoritative PostgreSQL inventory by warehouse and active total |
| PUT | `/api/v1/inventory/{sku}` | Admin: set SOH and aggregate committed B2B under a row lock |
| POST | `/api/v1/products` | Admin: provision a product |
| POST | `/api/v1/warehouses` | Admin: provision an active warehouse |
| POST | `/api/v1/webhooks/shopify/mock` | Authenticated mock webhook using the allocation request shape |
| GET | `/health/live`, `/health/ready` | Liveness and database readiness; reports Redis degradation |

Stock PUT body: `{"warehouse_code":"EH","stock_on_hand":10,"committed_b2b":5}`.
It is an absolute reconciliation, so integrations must submit current, ordered ERP snapshots;
it is not a delta event or a PO ledger. Only authoritative stock operators should have admin keys.
Online allocations, including the generic `b2b` channel, consume **uncommitted** AFS; they do not
redeem stock already represented in `committed_b2b`.

Domain errors have shape `{"error":{"code":"OUT_OF_STOCK","message":"..."}}`.
Invalid input returns 422; missing resources 404; stock/idempotency/state conflicts 409;
database timeout/unavailability 503 with `Retry-After: 1`. Retry transient errors using the
same key. Clients must reconcile uncertain HTTP outcomes by retrying or reading the allocation.

## Correctness and lifecycle

`AFS = stock_on_hand - committed_b2b - in_flight_reserved`

AFS is a PostgreSQL stored generated column. Database CHECK constraints enforce nonnegative
stock, commitments, reservations, and AFS. Tenant-composite foreign keys prevent cross-tenant
inventory references. Application queries enforce tenant isolation; PostgreSQL RLS is not enabled.

An allocation inserts its idempotency record and locks inventory rows using `SELECT FOR UPDATE`
in deterministic `(warehouse_code, sku)` order, checks current AFS, increments reservations,
and appends outbox events **in one transaction**. Any failed line rolls back the whole cart.
Requests never reserve using a cached stock value. A session is created per request/operation.
The row-lock behavior is documented by [PostgreSQL](https://www.postgresql.org/docs/current/explicit-locking.html);
the session ownership follows [SQLAlchemy async guidance](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).

Tenant-wide unique idempotency keys serialize concurrent duplicate deliveries. A replay returns
the allocation's current state; a different payload with the same key returns a conflict. A second
unique constraint prevents the same `(tenant, channel, order_id)` being allocated under a new key.
Line ordering does not affect payload identity. Rejected orders roll back their key, so they may
be retried after restocking. Successfully used keys remain reserved even after cancellation.

```text
RESERVED ──confirm──> CONFIRMED ──fulfill──> FULFILLED
    │                    │
    └─────cancel──────────┴───────────────> CANCELLED
```

Confirmation retains `in_flight_reserved` because stock is still physically in the warehouse.
Fulfillment decrements SOH and the hold together, leaving AFS unchanged. Cancellation releases
the hold without changing SOH. Repeating the same transition is idempotent; terminal states
cannot be reversed. Holds do not silently expire: an upstream payment/order reconciliation
process must explicitly cancel abandoned reservations. This avoids returning accepted orders'
stock to sale merely because a webhook was delayed.

## Asynchronous delivery and adapters

`BaseChannelAdapter` defines async `sync_inventory(sku, afs_qty)` and
`handle_incoming_webhook(payload)`. Register implementations in `app/adapters/registry.py`.
The Shopify adapter is a **mock**: it logs outbound quantities and normalizes authenticated
inbound requests. It makes no marketplace API calls. Amazon and Mirakl are accepted order channel
identifiers; their API integrations are not implemented.

The example publishes `floor(0.90 * AFS)` (5 → 4). Use decimal ratios, not rounding upward.
An Amazon adapter can use the same buffering policy. A buffer reduces exposure; it does not
mathematically eliminate overselling across independently accepting marketplaces. Publishing
the same AFS to several channels can double-sell those units before either webhook arrives.
Strict end-to-end prevention requires synchronous pre-order reservation or exclusive channel
quotas whose total never exceeds AFS, plus channel-specific reconciliation. This service guarantees
no over-allocation for orders admitted through its transaction boundary.

The [ARQ](https://arq-docs.helpmanual.io/) worker scans pending PostgreSQL outbox events every
two seconds, schedules tenant jobs in Redis, and reads **current** AFS when pushing. A PostgreSQL
advisory transaction lock serializes pushes per tenant. Successful batches are marked delivered;
failures use capped exponential backoff. A crash after remote acceptance may replay a push,
so real adapters must set absolute quantities idempotently and bound their network timeouts.
No events are discarded on retry exhaustion. Monitor oldest pending age and repeated attempts.

Redis stores 30-second eventual snapshots under `inventory:{tenant_uuid}:{sku}`. Neither the
API's authoritative inventory read nor reservations use these snapshots. Redis downtime delays
channel delivery but cannot create stock. A lost Redis job is recovered by outbox polling.
Inventory may change during a push; later outbox work converges it. No distributed transaction
with a marketplace is claimed. Failed/unconfigured real channels require operational reconciliation.

## Local development

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate; POSIX: source .venv/bin/activate
pip install -c constraints.txt -e '.[test]'
# Copy .env.example to .env; start PostgreSQL and Redis, or use:
docker compose up -d postgres redis
python -m scripts.migrate
python -m scripts.seed
uvicorn app.main:app --reload
# In another terminal:
arq app.worker.WorkerSettings
```

Run `pytest -q` for unit tests; PostgreSQL tests explicitly skip without `TEST_DATABASE_URL`.
Set it to a migrated PostgreSQL database to run the complete suite. For PowerShell:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://omniguardian:local-development-only@localhost:5432/omniguardian'
python -m pytest -q
python -m ruff check app scripts tests
```

The checks cover 50-way contention, concurrent duplicate deliveries, conflicting payloads,
multi-line rollback, tenant/role isolation, B2B protection, transition idempotency, database
constraints, and durable outbox recovery. `scripts/migrate.py` serializes migrations with a
database advisory lock and checks applied file hashes. Add forward migrations; never edit applied SQL.

Validation during implementation: 26 tests passed against PostgreSQL 17.6 on Windows/Python 3.12,
with no skipped tests. The standalone script also passed against a running Uvicorn HTTP server;
the seed was exercised twice to verify repeatability. Ruff and OpenAPI generation passed.
Docker image startup and a live Redis/ARQ deployment were not exercised on that machine because
Docker/Redis were unavailable; Compose and CI provide the reproducible deployment checks.
`constraints.txt` records the tested Python dependency versions. Platform-specific extras may
add dependencies (for example uvloop on Linux); regenerate and verify constraints when upgrading.

```text
app/api/         schemas and authenticated endpoints
app/core/        settings, sessions, authentication, domain errors
app/models/      SQLAlchemy domain mappings
app/services/    atomic reservations, lifecycle, stock reconciliation
app/adapters/    extension interface, registry, mock Shopify transport
app/worker.py    ARQ dispatch and transactional outbox delivery
migrations/      versioned PostgreSQL schema
scripts/         migration runner, local demo seed, HTTP contention demonstration
tests/           unit and real-PostgreSQL integration tests
```

## Deployment boundary

This is an executable service foundation with transactional safeguards, not a completed live
marketplace rollout. Compose is a local demonstration: replace its credentials and remove the demo
seed dependency for production. Provision random tenant API keys (store SHA-256 hashes), keep the
database private, terminate TLS at your ingress, rate-limit callers, and use separate runtime and
migration database roles. Real webhook endpoints need channel-specific signature verification,
tenant/shop mapping, and payload normalization before calling the allocation service.

Use managed backups/PITR and tested restore procedures, monitor outbox lag/database pool pressure,
and plan retention for terminal orders and delivered outbox rows while preserving the required
idempotency window. PO-level accounting, marketplace credentials, exclusive channel quotas,
returns/partial fulfillment, and automatic hold reconciliation are extension points. Production
capacity and sub-10ms latency require deployment-specific load tests; no unmeasured latency promise
is made. PostgreSQL is deliberately the sole reservation authority to avoid a Redis/PostgreSQL
dual-write failure window.
