export interface Env {
  DB_INVENTORY: D1Database;

  EBAY_CLIENT_ID: string;
  EBAY_CLIENT_SECRET: string;

  // optional overrides
  EBAY_SITE_ID?: string;        // default "0"
  EBAY_COMPAT_LEVEL?: string;   // default "1271"
  EBAY_OAUTH_SCOPE?: string;

  // optional admin protection
  ADMIN_API_KEY?: string;

  // Queue producer binding (so /admin/process-queue can enqueue)
  RECALC_QUEUE: Queue;
}

type RecalcMsg = {
  account_id: number;
  product_id: number;
  reason?: string;
};

const TRADING_ENDPOINT = "https://api.ebay.com/ws/api.dll"; // production
const TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token";
const XMLNS = "urn:ebay:apis:eBLBaseComponents";

function nowUnix() {
  return Math.floor(Date.now() / 1000);
}

function escapeXml(s: string) {
  return (s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

function requireAdmin(req: Request, env: Env) {
  if (!env.ADMIN_API_KEY) return;
  if (req.headers.get("x-api-key") !== env.ADMIN_API_KEY) {
    throw new Response("Unauthorized", { status: 401 });
  }
}

/**
 * Refresh token if expiring soon.
 */
async function getEbayAccessToken(env: Env, accountId: number): Promise<string> {
  const row = await env.DB_INVENTORY.prepare(
    `SELECT access_token, refresh_token, expires_at
     FROM ebay_oauth
     WHERE account_id = ?`
  )
    .bind(accountId)
    .first<{ access_token: string; refresh_token: string; expires_at: number }>();

  if (!row) throw new Error(`Missing ebay_oauth row for account_id=${accountId}`);

  const buffer = 300;
  if (row.expires_at > nowUnix() + buffer) return row.access_token;

  const basic = btoa(`${env.EBAY_CLIENT_ID}:${env.EBAY_CLIENT_SECRET}`);

  const scope =
    env.EBAY_OAUTH_SCOPE ||
    "https://api.ebay.com/oauth/api_scope " +
      "https://api.ebay.com/oauth/api_scope/sell.inventory " +
      "https://api.ebay.com/oauth/api_scope/sell.account";

  const resp = await fetch(TOKEN_URL, {
    method: "POST",
    headers: {
      Authorization: `Basic ${basic}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: row.refresh_token,
      scope,
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Token refresh failed: ${resp.status} ${text}`);
  }

  const json = (await resp.json()) as { access_token: string; expires_in: number };
  const newToken = json.access_token;
  const expiresAt = nowUnix() + Number(json.expires_in || 0);

  await env.DB_INVENTORY.prepare(
    `UPDATE ebay_oauth
     SET access_token = ?, expires_at = ?
     WHERE account_id = ?`
  )
    .bind(newToken, expiresAt, accountId)
    .run();

  return newToken;
}

/**
 * Trading call wrapper with ack/errors parsing.
 */
async function tradingCall(env: Env, accountId: number, callName: string, requestXml: string): Promise<string> {
  const token = await getEbayAccessToken(env, accountId);

  const resp = await fetch(TRADING_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "text/xml",
      "X-EBAY-API-CALL-NAME": callName,
      "X-EBAY-API-SITEID": (env.EBAY_SITE_ID || "0").trim(),
      "X-EBAY-API-COMPATIBILITY-LEVEL": (env.EBAY_COMPAT_LEVEL || "1271").trim(),
      "X-EBAY-API-IAF-TOKEN": token,
    },
    body: requestXml,
  });

  const xmlText = await resp.text();
  if (!resp.ok) throw new Error(`Trading ${callName} HTTP ${resp.status}: ${xmlText}`);

  const ack = getXmlTag(xmlText, "Ack");
  if (ack && ack.toUpperCase() !== "SUCCESS" && ack.toUpperCase() !== "WARNING") {
    const errs = extractTradingErrors(xmlText);
    throw new Error(`Trading ${callName} failed: ${errs.length ? errs.join(" | ") : xmlText}`);
  }

  return xmlText;
}

function getXmlTag(xml: string, tag: string): string | null {
  const m = xml.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`));
  return m ? m[1].trim() : null;
}

function extractTradingErrors(xml: string): string[] {
  const blocks = [...xml.matchAll(/<Errors[\s\S]*?<\/Errors>/g)].map((m) => m[0]);
  const out: string[] = [];
  for (const b of blocks) {
    const code = getXmlTag(b, "ErrorCode") || "";
    const short = getXmlTag(b, "ShortMessage") || "";
    const longm = getXmlTag(b, "LongMessage") || "";
    const msg = `[${code}] ${short}` + (longm ? ` - ${longm}` : "");
    if (msg.trim() !== "[]") out.push(msg);
  }
  return out;
}

/**
 * ReviseFixedPriceItem quantity.
 */
async function setListingQuantity(env: Env, accountId: number, itemId: string, quantity: number) {
  const req = `<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="${XMLNS}">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>
    <ItemID>${escapeXml(itemId)}</ItemID>
    <Quantity>${Math.trunc(quantity)}</Quantity>
  </Item>
</ReviseFixedPriceItemRequest>`;

  await tradingCall(env, accountId, "ReviseFixedPriceItem", req);
}

/**
 * Compute desired listing qty from master inventory:
 * available_units = max(0, on_hand - reserved)
 * desired = floor(available_units / units_per_sale)
 *
 * Returns rows + a snapshot of qty_reserved used.
 */
async function computeDesired(env: Env, accountId: number, productId: number) {
  const sb = await env.DB_INVENTORY.prepare(
    `SELECT qty_on_hand, qty_reserved
     FROM stock_balance
     WHERE product_id = ?`
  )
    .bind(productId)
    .first<{ qty_on_hand: number; qty_reserved: number }>();

  const qtyOnHand = Number(sb?.qty_on_hand ?? 0);
  const qtyReserved = Number(sb?.qty_reserved ?? 0);

  const availableUnits = Math.max(0, qtyOnHand - qtyReserved);

  const listings = await env.DB_INVENTORY.prepare(
    `SELECT listing_id, ebay_item_number, units_per_sale, last_pushed_qty
     FROM listings
     WHERE account_id = ? AND product_id = ?`
  )
    .bind(accountId, productId)
    .all<{
      listing_id: number;
      ebay_item_number: string;
      units_per_sale: number;
      last_pushed_qty: number | null;
    }>();

  const rows = listings.results.map((l) => {
    const ups = Math.max(1, Number(l.units_per_sale || 1));
    const desired = Math.floor(availableUnits / ups);
    return { ...l, desired_qty: Math.max(0, desired) };
  });

  return { rows, qtyReservedSnapshot: qtyReserved };
}

/**
 * Resolve reserved into on_hand using the snapshot:
 * qty_on_hand -= reservedSnapshot
 * qty_reserved -= reservedSnapshot
 *
 * This avoids wiping out reservations that arrived after we took the snapshot.
 */
async function resolveReserved(env: Env, productId: number, reservedSnapshot: number) {
  const r = Math.max(0, Math.trunc(reservedSnapshot || 0));
  if (r <= 0) return;

  await env.DB_INVENTORY.prepare(
    `UPDATE stock_balance
     SET qty_on_hand = CASE WHEN qty_on_hand >= ? THEN qty_on_hand - ? ELSE 0 END,
         qty_reserved = CASE WHEN qty_reserved >= ? THEN qty_reserved - ? ELSE 0 END,
         updated_at = datetime('now')
     WHERE product_id = ?`
  )
    .bind(r, r, r, r, productId)
    .run();
}

async function updateLastPushed(env: Env, listingId: number, pushedQty: number) {
  await env.DB_INVENTORY.prepare(
    `UPDATE listings
     SET last_pushed_qty = ?,
         last_pushed_at  = datetime('now'),
         updated_at      = datetime('now')
     WHERE listing_id = ?`
  )
    .bind(Math.trunc(pushedQty), listingId)
    .run();
}

/**
 * Process exactly one work item (account_id, product_id).
 * Deletes recalc_queue row only if requested_at is unchanged (prevents wiping newer triggers).
 */
async function processWorkItem(env: Env, accountId: number, productId: number) {
  const rq = await env.DB_INVENTORY.prepare(
    `SELECT requested_at
     FROM recalc_queue
     WHERE account_id = ? AND product_id = ?`
  )
    .bind(accountId, productId)
    .first<{ requested_at: string }>();

  // Nothing queued anymore => nothing to do
  if (!rq?.requested_at) return { updated: 0, processed: 0 };

  const requestedAtSnapshot = rq.requested_at;

  const { rows, qtyReservedSnapshot } = await computeDesired(env, accountId, productId);
  const toPush = rows.filter((r) => (r.last_pushed_qty ?? -1) !== r.desired_qty);

  let updated = 0;

  for (const r of toPush) {
    await setListingQuantity(env, accountId, r.ebay_item_number, r.desired_qty);
    await updateLastPushed(env, r.listing_id, r.desired_qty);
    updated++;
  }

  await resolveReserved(env, productId, qtyReservedSnapshot);

  // Delete only if nobody refreshed requested_at while we were working.
  const del = await env.DB_INVENTORY.prepare(
    `DELETE FROM recalc_queue
     WHERE account_id = ? AND product_id = ? AND requested_at = ?`
  )
    .bind(accountId, productId, requestedAtSnapshot)
    .run();

  // If del.changes === 0, a newer trigger arrived; leave it queued for the next run.
  return { processed: 1, updated };
}

function retryDelaySeconds(attempts: number) {
  // attempts starts at 1. Backoff: 5s, 15s, 45s, 135s... capped at 15 minutes
  const base = 5 * Math.pow(3, Math.max(0, attempts - 1));
  return Math.min(900, Math.trunc(base));
}

export default {
  /**
   * Queue consumer entrypoint.
   * Uses per-message ack()/retry() so one failure doesn't fail the entire batch. :contentReference[oaicite:4]{index=4}
   */
  async queue(batch: MessageBatch<RecalcMsg>, env: Env, ctx: ExecutionContext): Promise<void> {
    // Deduplicate within a batch: key = `${account_id}:${product_id}`
    const grouped = new Map<string, { account_id: number; product_id: number; messages: Message<RecalcMsg>[] }>();

    for (const m of batch.messages) {
      const b = m.body as any;
      const account_id = Number(b?.account_id);
      const product_id = Number(b?.product_id);
      if (!Number.isFinite(account_id) || !Number.isFinite(product_id)) {
        // Bad payload: ack so it doesn't poison the queue forever.
        m.ack();
        continue;
      }
      const key = `${account_id}:${product_id}`;
      const entry = grouped.get(key) || { account_id, product_id, messages: [] };
      entry.messages.push(m);
      grouped.set(key, entry);
    }

    for (const entry of grouped.values()) {
      try {
        await processWorkItem(env, entry.account_id, entry.product_id);
        for (const m of entry.messages) m.ack();
      } catch (err: any) {
        const msg = String(err?.message ?? err);

        // Record error on the D1 queue row (so you can see it without digging into DLQ first).
        ctx.waitUntil(
          env.DB_INVENTORY.prepare(
            `UPDATE recalc_queue
             SET last_error = ?
             WHERE account_id = ? AND product_id = ?`
          )
            .bind(msg, entry.account_id, entry.product_id)
            .run()
        );

        // Retry these messages with backoff.
        for (const m of entry.messages) {
          m.retry({ delaySeconds: retryDelaySeconds(m.attempts) });
        }
      }
    }
  },

  /**
   * Admin endpoint: enqueue work (still “queue only”).
   * - POST /admin/process-queue  (enqueues the oldest 10 recalc_queue items)
   */
  async fetch(req: Request, env: Env) {
    const url = new URL(req.url);

    if (url.pathname === "/admin/process-queue") {
      requireAdmin(req, env);

      // Pick oldest queued items and enqueue them.
      const queued = await env.DB_INVENTORY.prepare(
        `SELECT account_id, product_id
         FROM recalc_queue
         ORDER BY requested_at ASC
         LIMIT 10`
      ).all<{ account_id: number; product_id: number }>();

      const batchMsgs = queued.results.map((r) => ({
        body: { account_id: r.account_id, product_id: r.product_id } satisfies RecalcMsg,
      }));

      if (batchMsgs.length) {
        await env.RECALC_QUEUE.sendBatch(batchMsgs);
      }

      return new Response(
        JSON.stringify({ ok: true, enqueued: batchMsgs.length }, null, 2),
        { headers: { "content-type": "application/json" } }
      );
    }

    return new Response("Not found", { status: 404 });
  },
};