# eBay Inventory & Order Management Platform

A production system built to automate inventory management and eBay listing sync for a small retail business. Built and maintained independently.

## Overview

Managing inventory across eBay listings manually doesn't scale — a sale on one listing needs to immediately update quantities on related listings, and doing that by hand means either over-selling or constant manual updates. This system handles that automatically, end to end.

The platform consists of five components:

| Component | Stack | Purpose |
|---|---|---|
| `workers/webhook-worker` | JavaScript, Cloudflare Workers | Receives eBay order notifications and reserves stock |
| `workers/queue-consumer` | TypeScript, Cloudflare Workers | Computes listing quantities and pushes updates to eBay |
| `tools/inventory-receive` | Python, Textual | Terminal UI for receiving shipments and adjusting inventory |
| `tools/d1-tui` | Python, Textual | Interactive SQL browser for the Cloudflare D1 database |
| `competitor-monitor` | Python, PySide6 | Desktop app for monitoring competitor pricing on eBay |

---

## Architecture

```
eBay Order Event
      │
      ▼
webhook-worker          ← verifies ECDSA signature, writes idempotent ledger entry,
      │                   enqueues recalculation job
      ▼
Cloudflare D1 (SQLite)  ← relational database: products, listings, stock_balance,
      │                   stock_ledger, recalc_queue, accounts
      ▼
queue-consumer          ← reads stock_balance, computes desired qty,
      │                   calls eBay Trading API (ReviseFixedPriceItem),
      │                   resolves reservation
      ▼
eBay listing updated
```

The `inventory-receive` and `d1-tui` tools both talk directly to the D1 REST API for day-to-day operations. The `competitor-monitor` talks to the eBay Browse API to fetch live competitor listings.

### Database schema (simplified)

- **products** — catalog items with UPC, brand, model
- **listings** — eBay listing IDs mapped to products, with pricing and pack quantities
- **stock_balance** — current on-hand and reserved quantities per product
- **stock_ledger** — double-entry audit trail of every inventory movement
- **recalc_queue** — jobs enqueued by the webhook worker for the queue consumer
- **accounts** — chart of accounts for the ledger

---

## Components

### webhook-worker (`workers/webhook-worker`)

Cloudflare Worker triggered by eBay's Marketplace Account Deletion and Order Confirmation notifications.

- Verifies ECDSA signatures on incoming webhooks using the Web Crypto API
- On order confirmation: atomically reserves stock via idempotent ledger inserts (safe to retry)
- Filters test events in non-production environments
- Enqueues a recalculation job to the queue consumer

### queue-consumer (`workers/queue-consumer`)

Cloudflare Worker triggered by the internal queue.

- Reads `stock_balance` to compute desired listing quantity: `floor((on_hand - reserved) / units_per_listing)`
- Pushes `ReviseFixedPriceItem` XML calls to the eBay Trading API
- Resolves stock reservations using snapshot-safe updates to avoid race conditions
- Deduplicates jobs within a batch; retries with exponential backoff on D1 timeouts

### inventory-receive (`tools/inventory-receive`)

Python terminal UI (Textual) for receiving shipments and adjusting stock.

- Barcode scan mode: scanner sends Enter, app auto-selects the matching product and moves focus to quantity
- Action templates for common workflows: receive shipment, customer return, damage write-off, manual adjustment
- Reads live stock from D1; shows current on-hand and reserved before submission
- Writes to the inventory worker via authenticated POST; logs every action locally to a `.jsonl` file

**Setup:**
```bash
pip install textual requests python-dotenv
cp .env.example .env   # fill in values
python tools/inventory-receive/inventory-receive.py
```

Required env vars: `INVENTORY_WORKER_URL`, `INVENTORY_API_KEY`  
Optional (enables product search): `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`

### d1-tui (`tools/d1-tui`)

Python terminal UI (Textual) for browsing and editing the Cloudflare D1 database directly.

- Paginated table browser with row/cell editing, add row, delete row
- SQL console with dot-commands: `.tables`, `.schema`, `.mode`, `.params`, `.limit`
- Edits are queued locally and committed in batch to minimize API calls
- Batches identical-shape INSERTs into a single multi-values statement

**Setup:**
```bash
pip install textual requests python-dotenv
cp .env.example .env   # fill in values
python tools/d1-tui/d1_tui.py
```

Required env vars: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`

### competitor-monitor (`competitor-monitor`)

PySide6 desktop app for monitoring competitor pricing on eBay.

- Reads your product catalog live from D1
- Searches eBay Browse API for competitor listings, filtered to a configured seller list
- Background worker pool (QThreadPool) updates rows progressively without freezing the UI
- Sortable, filterable results table; refresh visible, selected, or all rows

See `competitor-monitor/README.md` for full setup instructions.

Required env vars: `CLOUDFLARE_*`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `COMPETITOR_SELLERS`

---

## Setup

### Environment variables

Copy `.env.example` to `.env` and fill in the required values:

```bash
cp .env.example .env
```

Each tool reads from `.env` automatically via `python-dotenv`. See `.env.example` for all available variables.

### Cloudflare Workers

The workers are deployed via Wrangler. Secrets are stored as Cloudflare Worker secrets (not in source):

```bash
wrangler secret put ADMIN_API_KEY
wrangler secret put EBAY_CLIENT_ID
# etc.
```

See `workers/webhook-worker/wrangler.toml` and `workers/queue-consumer/wrangler.toml` for configuration.

---

## Notes

This is a working production system, not a general-purpose tool. The schema, worker logic, and TUI behavior are all specific to the inventory and listing structure of one business. It is published here as a reference implementation, not a library.
