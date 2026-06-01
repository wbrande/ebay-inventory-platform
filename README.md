# eBay Inventory & Order Management Platform

A system built to automate inventory management and eBay listing sync for a small retail business. Built and maintained independently.

## Overview

Managing inventory across eBay listings manually doesn't scale — a sale on one listing needs to immediately update quantities on related listings, and doing that by hand means either over-selling or constant manual updates. This system handles that automatically, end to end.

The platform consists of six components:

| Component | Stack | Purpose |
|---|---|---|
| `workers/webhook-worker` | JavaScript, Cloudflare Workers | Receives eBay order notifications and reserves stock |
| `workers/inventory-admin` | JavaScript, Cloudflare Workers | Handles inventory move and recalc enqueue requests |
| `workers/queue-consumer` | TypeScript, Cloudflare Workers | Computes listing quantities and pushes updates to eBay |
| `ebay_tool.py` | Python, PySide6 | Desktop app: inventory receiving, D1 spreadsheet browser, SQL console, eBay CSV import |
| `import_ebay_active_listings.py` | Python, CLI | Generates SQL from eBay's "All Active Listings" CSV for D1 import |
| `competitor-monitor` | Python, PySide6 | Desktop app for monitoring competitor pricing on eBay |

---

## Architecture

```
eBay Order Event
      │
      ▼
webhook-worker          ← verifies signature, stores raw event,
      │                    inserts stock reservation, enqueues recalculation job
      ▼
Cloudflare D1 (SQLite)  ← relational database: products, listings, stock_balance,
      │                    stock_ledger, recalc_queue, accounts, ebay_oauth
      ▼
queue-consumer          ← reads stock_balance, computes desired qty,
      │                    calls eBay Trading API (ReviseFixedPriceItem),
      │                    resolves reservation
      ▼
eBay listing updated
```

The `ebay_tool.py` desktop app and `import_ebay_active_listings.py` CLI both talk to the Cloudflare D1 REST API. The `inventory-admin` worker sits between the desktop app and the database for inventory moves and recalc enqueues.

### Auto-recalc flag

Each product has an `auto_recalc` column:
- `auto_recalc = 1` — product has multiple eBay listings; stock changes trigger automatic recalculation of listing quantities.
- `auto_recalc = 0` — product has exactly one listing; automatic recalc is skipped (manual recalc still works).

### Database schema (simplified)

- **products** — catalog items with UPC, brand, model, and `auto_recalc` flag
- **listings** — eBay listing IDs mapped to products, with pricing and pack quantities
- **stock_balance** — current on-hand and reserved quantities per product
- **stock_ledger** — double-entry audit trail of every inventory movement
- **recalc_queue** — jobs enqueued by webhook/inventory workers for the queue consumer
- **accounts** — eBay accounts
- **ebay_oauth** — OAuth tokens for eBay API access

---

## Components

### webhook-worker (`workers/webhook-worker`)

Cloudflare Worker triggered by eBay's Marketplace Account Deletion and Order Confirmation notifications.

- Verifies signatures on incoming webhooks using the Web Crypto API
- On order confirmation: atomically reserves stock via idempotent ledger inserts (safe to retry)
- Filters test events in non-production environments
- If the product has `auto_recalc = 1`, enqueues a recalculation job

### inventory-admin (`workers/inventory-admin`)

Cloudflare Worker that handles authenticated admin requests from the desktop app.

- `POST /admin/inventory/move` — validates stock moves, inserts ledger rows, updates `stock_balance`
- `POST /admin/recalc/enqueue` — enqueues a manual recalculation for a given account/product
- On inventory moves for products with `auto_recalc = 1`, automatically enqueues recalc jobs for all distinct accounts linked to that product

### queue-consumer (`workers/queue-consumer`)

Cloudflare Worker triggered by the internal queue.

- Reads `stock_balance` to compute desired listing quantity: `floor((on_hand - reserved) / units_per_listing)`
- Pushes `ReviseFixedPriceItem` XML calls to the eBay Trading API
- Resolves stock reservations using snapshot-safe updates to avoid race conditions
- Deduplicates jobs within a batch; retries with exponential backoff on D1 timeouts

### ebay_tool (`ebay_tool.py`)

Python desktop app (PySide6) — the primary day-to-day operations tool. Four tabs:

**Inventory tab** — Receive shipments and adjust stock.
- Barcode scan mode: scanner sends Enter, app auto-selects the matching product and moves focus to quantity
- Action templates for common workflows: receive shipment, customer return, damage write-off, manual adjustment
- Reads live stock from D1; shows current on-hand and reserved before submission
- "Queue Recheck" button to manually trigger a recalculation without touching inventory

**Spreadsheet Browser tab** — Browse and edit D1 tables directly.
- Paginated table browser with row/cell editing, add row, delete row
- Filtering and conditional formatting
- Edits are queued locally and committed in batch to minimize API calls

**SQL Query tab** — Execute arbitrary SQL against a local SQLite copy of the D1 database.

**Import eBay Listings tab** — Parse eBay's "All Active Listings" CSV and import into D1.

**Setup:**
```bash
pip install PySide6 requests pandas
python ebay_tool.py
```

On first launch, go to **File → Settings** to enter your Cloudflare credentials (`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`). Optionally configure `INVENTORY_WORKER_URL` and `INVENTORY_API_KEY` for the Inventory tab. Settings are persisted via QSettings — no `.env` file needed.

### import_ebay_active_listings (`import_ebay_active_listings.py`)

CLI tool for importing eBay listings from a CSV export.

- Processes all listings where Condition = "New" (single items and multi-packs)
- Inserts only missing products (avoids autoincrement burn)
- Inserts only missing listings; updates existing ones
- Does not touch stock_balance or stock_ledger
- Generates a SQL file for `wrangler d1 execute`

**Usage:**
```bash
python import_ebay_active_listings.py --csv "eBay-all-active-listings-report.csv" --out import.sql --db ebay_information --account-id 1
npx wrangler d1 execute ebay_information --remote --file import.sql
```

### competitor-monitor (`competitor-monitor`)

PySide6 desktop app for monitoring competitor pricing on eBay.

- Reads your product catalog live from D1
- Searches eBay Browse API for competitor listings, filtered to a configured seller list
- Background worker pool (QThreadPool) updates rows progressively without freezing the UI
- Sortable, filterable results table; refresh visible, selected, or all rows

See `competitor-monitor/README.md` for full setup instructions.

Required env vars: `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_DEV_ID`, `EBAY_USER_TOKEN`

---

## Setup

### Environment variables

The `competitor-monitor` reads from `.env` via `python-dotenv`. Copy `.env.example` and fill in the required values:

```bash
cp .env.example .env
```

See the `.env.example` file (included with `competitor-monitor`) for all available variables.

### Cloudflare Workers

The workers are deployed via Wrangler. Secrets are stored as Cloudflare Worker secrets (not in source):

```bash
wrangler secret put ADMIN_API_KEY
wrangler secret put EBAY_CLIENT_ID
wrangler secret put EBAY_CLIENT_SECRET
wrangler secret put VERIFICATION_TOKEN
wrangler secret put ENDPOINT
# etc.
```

See the worker source files for full configuration.

### Desktop apps

**ebay_tool:**
```bash
pip install PySide6 requests pandas
python ebay_tool.py
```

Credentials are entered through the app's **File → Settings** dialog and persisted via QSettings. No `.env` file needed.

**competitor-monitor:**
```bash
pip install PySide6 requests pandas python-dotenv
cp .env.example .env   # fill in values
python competitor-monitor/competitor_monitor.py
```


---

## Notes

This is not a general-purpose tool. The schema, worker logic, and app behavior are all specific to the inventory and listing structure of one business. It is published here as a reference implementation, not a library.
