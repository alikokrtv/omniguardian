# OmniGuardian Suite

**Enterprise Omnichannel Inventory Allocation & Commerce Operations Platform (v0.2.0)**

Async FastAPI inventory allocation service using PostgreSQL as its authoritative ledger,
Redis/ARQ for background delivery, and a transactional outbox for recoverable channel pushes.
Includes an executive bilingual dashboard (🇹🇷 TR / 🇺🇸 EN), append-only stock audit ledger,
B2B wholesale hard-locking, automated returns & carrier claims recon, true net financial settlement,
and monthly SaaS usage quotas. Python 3.11+; containerized with Python 3.12.

---

### 🏛️ The 4 Core Architectural Pillars

1. **Pillar 1: Real-Time AFS & Zero-Phantom-Stock Shield**
   - Atomic row-level PostgreSQL reservations with Redis Outbox event dispatching.
   - Eliminates overselling across Amazon, Shopify, Target Plus, Best Buy, and Walmart.
   - `AFS = SOH - Committed B2B - In-Flight Reserved`.

2. **Pillar 2: B2B Wholesale Allocation & Pallet Quarantine**
   - Full lifecycle management for retail wholesale POs (Ross, Burlington, Ollie's).
   - Hard-lock protection: confirmed wholesale allocations permanently decrement available AFS and prevent consumer oversell.
   - Dedicated pallet tagging and warehouse bin staging.

3. **Pillar 3: Returns, Quarantine & Carrier Claims Recon**
   - Automated tracking of marketplace returns (Target, Best Buy, Walmart, Amazon).
   - 14-business-day claim engine: automatically generates carrier reimbursement dossiers (FedEx, UPS, USPS) for lost parcels.
   - Quarantine ledger for damaged returns (`DAMAGED_BOX`, `DEFECTIVE_SCRAP`) preventing unsellable inventory from returning to AFS.

4. **Pillar 4: True Net Settlement & Financial Reconciliation**
   - Reconciles marketplace payout disbursements against historical ERP COGS.
   - Explicitly deducts commission fees, FBA storage, return chargebacks, and ad spend to expose real GAAP net margin per SKU.

---

### 🌐 Bilingual Executive Control Center (`/dashboard`)

Access the live executive dashboard at `http://localhost:8000/dashboard` with instant **TR (🇹🇷 Türkçe)** and **EN (🇺🇸 English)** localization:
- **Live Inventory Matrix**: Real-time SOH, B2B, in-flight holds, and live AFS with low-stock badges.
- **Loss-Prevention ROI**: Calculated penalty savings from prevented oversell events.
- **Transactional Audit Trail**: Immutable ledger of every stock movement, actor, and reference order.
- **Plan Quotas**: Visual usage progress tracking across Starter, Growth, and Enterprise tiers.

---

### 🚀 7-Day Onboarding & Enterprise Implementation Roadmap

- **Day 1–2: Catalog & Warehouse Mapping**
  - Master SKU taxonomy, barcode/UPC setup, warehouse bin allocation.
  - Zero-downtime CSV/Excel bulk snapshot ingestion via `/api/v1/import/catalog`.
- **Day 3–4: Marketplace & ERP Bridge Integration**
  - Connect Shopify, Amazon, Target, and existing ERPs (Zoho, Odoo, NetSuite).
  - Configure safety buffers (e.g. 20-unit buffer or 90% allocation rule).
- **Day 5–6: Operational Staff Training**
  - Warehouse intake and quarantine triage protocols.
  - Operations team dashboard monitoring and loss prevention reviews.
- **Day 7: Go-Live & Active Protection**
  - Shift channel stock feeds to OmniGuardian authoritative ledger.


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

Open [the dashboard](http://localhost:8000/dashboard) and enter the same API key to connect.
It provides an inventory matrix, loss-prevention estimates, recent activity, a searchable audit
trail, and plan usage. Any reader, allocator, or admin key can view its own tenant's dashboard.
The HTML shell is public; every data request is authenticated. Keys stay in browser memory
and are never written to local/session storage or URLs. Data refreshes every five seconds while
the page is visible, with stale/error indicators and an explicit disconnect control.

For an existing deployment, rebuild the image and run the forward migration before rolling out
the new API/worker: `docker compose run --rm migrate`. The migration defaults existing tenants
to STARTER and backfills all monthly allocation counts from existing orders. Choose paid tiers
before reopening allocation traffic if an existing tenant is already over the STARTER quota.
Migration 002 does not fabricate historical audit records; the ledger starts at deployment.

```sh
docker compose exec api python -m scripts.concurrency_demo
docker compose --profile test run --rm test
docker compose logs -f api worker
docker compose down
```

The concurrency script creates a unique product and warehouse, starts 50 HTTP requests,
and asserts **5 successful reservations, 45 HTTP 409 OUT_OF_STOCK responses, and 5 protected
B2B units**. It prints measured median/p95 latency and retains the demonstration records.
The integration suite uses actual PostgreSQL in unique temporary schemas and drops only those schemas.
Its database role needs CREATE SCHEMA permission. Audit immutability triggers stay enabled during tests.
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
| GET | `/api/v1/audit-trail` | Tenant audit records; SKU/date filters and cursor pagination |
| GET | `/api/v1/billing/usage` | Current plan, UTC billing cycle, usage, and remaining quota |
| GET | `/api/v1/dashboard/inventory` | Searchable, paginated SKU totals across active warehouses |
| GET | `/api/v1/dashboard/telemetry` | Observed stock rejections and estimated USD penalties avoided |
| GET | `/health/live`, `/health/ready` | Liveness and database readiness; reports Redis degradation |

Stock PUT body: `{"warehouse_code":"EH","stock_on_hand":10,"committed_b2b":5}`.
It is an absolute reconciliation, so integrations must submit current, ordered ERP snapshots;
it is not a delta event or a PO ledger. Only authoritative stock operators should have admin keys.
Online allocations, including the generic `b2b` channel, consume **uncommitted** AFS; they do not
redeem stock already represented in `committed_b2b`.

Domain errors have shape `{"error":{"code":"OUT_OF_STOCK","message":"..."}}`.
Invalid input returns 422; missing resources 404; stock/idempotency/state conflicts 409;
monthly quota exhaustion returns 402 with `USAGE_LIMIT_EXCEEDED`;
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
The same transaction consumes one unit of monthly quota and appends each line's audit entry.
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

## Audit and forensic ledger

Every reserve, confirm, cancel, fulfill, and changed manual reconciliation appends a row to
`audit_logs` in the same transaction as its inventory/order change. Failed carts leave no audit
entries or usage increments. Idempotent replays and no-op stock PUTs do not create duplicates.
Confirmation is audited even though it leaves inventory quantities unchanged.

Each record includes tenant, SKU, warehouse, action, AFS before/after, order reference, a timestamp,
and a stable `api_key:<uuid>` actor identifier. Raw API keys and their hashes are not audit actors.
`details` contains complete before/after SOH, B2B commitments, reservations, and line quantity.
`quantity_delta` consistently means **new AFS minus previous AFS**: reserve is negative,
cancel positive, and confirm/fulfill zero. Fulfillment's physical deduction remains visible in
the snapshots; B2B-only manual changes are also fully traceable.

PostgreSQL triggers reject UPDATE, DELETE, and TRUNCATE on the ledger, including ordinary DML
issued by its owner. There is no mutation endpoint. Table owners/superusers can still disable
triggers or drop objects: production runtime credentials must not own tables or have DDL or
superuser privileges. Append-only SQL controls are not a substitute for externally retained
backups or WORM storage when protection against a database administrator is required.

```text
GET /api/v1/audit-trail?sku=DEMO-SKU&start_date=2026-10-01T00:00:00Z&end_date=2026-11-01T00:00:00Z&limit=50
```

Dates must include an offset. The start is inclusive and the end exclusive. The response contains
`items` and `next_cursor`; pass that cursor as `before_id` for the next page, keeping the filters.
Records are ordered by timestamp and ID descending. Limits are 1–200, and IDs are JSON strings
to avoid JavaScript integer precision loss. Audit retention must preserve the ledger: decommission
tenants through archival policy rather than deleting their referenced rows.

## Usage metering and plans

| Plan | Successful order reservations per UTC calendar month |
| --- | ---: |
| STARTER | 1,000 |
| GROWTH | 10,000 |
| ENTERPRISE | Unlimited, still metered |

A unique `(tenant_id, cycle_start)` counter is incremented with a conditional PostgreSQL upsert
inside the allocation transaction, after idempotency resolution. The final slot can be consumed
by only one concurrent request. The guard is at the service boundary, including adapter calls,
so it cannot be bypassed by calling a different allocation endpoint. A middleware-only precheck
would not provide this concurrency guarantee.

One order counts once regardless of line count or units. Replays, rejected requests, confirmations,
and fulfillment add no usage. Cancellation does not refund a billable order. Existing allocations
can still be replayed, confirmed, cancelled, or fulfilled when the quota is exhausted. Each UTC
month gets a new counter automatically; no reset job is required. Failed stock checks roll back
their quota increment. Plan changes preserve the current cycle's count; downgrades block new
reservations if usage already meets the new cap.

`GET /api/v1/billing/usage` returns `tier_plan`, `cycle_start`, `cycle_end` (exclusive),
`timezone`, `allocation_count`, `quota`, `remaining_quota`, and `quota_exceeded`.
Unlimited quota/remaining values are `null`. Billing cycles use the database clock in UTC.

Only a trusted operator with database credentials can change plans; tenant API keys cannot
upgrade themselves. Example for the seeded tenant:

```sh
docker compose exec api python -m scripts.set_plan 00000000-0000-0000-0000-000000000001 GROWTH --penalty-estimate-usd 25.00
```

The penalty estimate defaults to **$25 per distinct external order rejected for insufficient
AFS**, an illustrative configurable assumption. Repeated deliveries or new idempotency keys
for that same `(tenant, channel, order_id)` do not inflate the count. Each rejection stores
the assumption at that time; changing it does not rewrite history. Counts start when this
module is deployed. The estimate is not verified savings or an invoice amount, and a rejected
order may later succeed after restocking. Rejection telemetry is committed in a separate
transaction after the failed cart rolls back; a telemetry database failure is logged without
changing the original OUT_OF_STOCK response. Monitor that log if complete metrics are required.
Invoice generation, taxes, payment collection, and subscription-provider webhooks are outside
this metering module; integrate them through a trusted operator/control-plane path.

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

Validation includes real PostgreSQL concurrency, immutability, actor tracking, full rollback on
audit failure, quota races, month boundaries, tier changes, migration backfill, telemetry deduplication,
tenant isolation, and dashboard APIs. The standalone contention script also runs against the HTTP server.
The completed suite passed **45 tests** on PostgreSQL 17.6/Python 3.12; the 50-request HTTP
demonstration retained its five-success/45-rejection result. The browser smoke test passed in Edge,
and desktop/mobile screenshots were visually inspected.
The browser smoke test checks login/logout, audit filters, billing, mobile layout, and JavaScript errors:

```sh
pip install -c constraints.txt -e '.[browser-test]'
python -m playwright install chromium
python -m scripts.check_dashboard
# Or use an installed Edge browser: python -m scripts.check_dashboard --browser-channel msedge
```

Screenshots are saved in `.tools/ui-check/` (ignored by Git). Run against an isolated demo tenant.
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
app/web/         self-contained HTML/CSS/JS executive dashboard
app/worker.py    ARQ dispatch and transactional outbox delivery
migrations/      versioned PostgreSQL schema
scripts/         migrations, seed, plan administration, HTTP and browser checks
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
