var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// src/index.ts
function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json" }
  });
}
__name(json, "json");
function requireAdmin(req, env) {
  if (!env.ADMIN_API_KEY) return;
  const key = req.headers.get("x-api-key");
  if (key !== env.ADMIN_API_KEY) throw new Response("Unauthorized", { status: 401 });
}
__name(requireAdmin, "requireAdmin");
function asInt(n) {
  const x = typeof n === "string" ? Number(n) : n;
  if (!Number.isFinite(x)) return null;
  return Math.trunc(x);
}
__name(asInt, "asInt");
function nonEmptyString(v) {
  if (typeof v !== "string") return null;
  const s = v.trim();
  return s.length ? s : null;
}
__name(nonEmptyString, "nonEmptyString");
async function getDistinctAccountsForProduct(env, productId) {
  const r = await env.DB_INVENTORY.prepare(
    `SELECT DISTINCT account_id
     FROM listings
     WHERE product_id = ?
       AND account_id IS NOT NULL`
  ).bind(productId).all();
  return r.results.map((x) => Number(x.account_id)).filter((x) => Number.isFinite(x));
}
__name(getDistinctAccountsForProduct, "getDistinctAccountsForProduct");
var index_default = {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/admin/inventory/move" && req.method === "POST") {
      requireAdmin(req, env);
      let body;
      try {
        body = await req.json();
      } catch {
        return json({ ok: false, error: "Invalid JSON" }, 400);
      }
      const productId = asInt(body.product_id);
      const qtyDelta = asInt(body.qty_delta);
      const reasonCode = nonEmptyString(body.reason_code);
      if (!productId || productId <= 0) return json({ ok: false, error: "product_id required" }, 400);
      if (qtyDelta === null || qtyDelta === 0) return json({ ok: false, error: "qty_delta must be non-zero integer" }, 400);
      if (!reasonCode) return json({ ok: false, error: "reason_code required" }, 400);
      const referenceType = nonEmptyString(body.reference_type) ?? "MANUAL";
      const referenceId = nonEmptyString(body.reference_id) ?? `manual-${crypto.randomUUID()}`;
      const notes = nonEmptyString(body.notes);
      const reason = nonEmptyString(body.reason);
      const enteredBy = nonEmptyString(body.entered_by);
      // Merge product validation + current stock balance into one query
      const prod = await env.DB_INVENTORY.prepare(
        `SELECT p.product_id, p.auto_recalc, COALESCE(sb.qty_on_hand,0) AS qty_on_hand, COALESCE(sb.qty_reserved,0) AS qty_reserved
         FROM products p
         LEFT JOIN stock_balance sb ON p.product_id = sb.product_id
         WHERE p.product_id = ?`
      ).bind(productId).first();
      if (!prod) return json({ ok: false, error: `Unknown product_id ${productId}` }, 404);
      const rc = await env.DB_INVENTORY.prepare(
        `SELECT reason_code, is_active
         FROM stock_ledger_reason_codes
         WHERE reason_code = ?`
      ).bind(reasonCode).first();
      if (!rc) return json({ ok: false, error: `Invalid reason_code ${reasonCode}` }, 400);
      if (Number(rc.is_active) !== 1) return json({ ok: false, error: `Inactive reason_code ${reasonCode}` }, 400);
      const onHand = Number(prod.qty_on_hand ?? 0);
      const newOnHand = onHand + qtyDelta;
      if (newOnHand < 0) {
        return json(
          {
            ok: false,
            error: "qty_on_hand would go negative",
            current_qty_on_hand: onHand,
            attempted_delta: qtyDelta,
            attempted_new_qty_on_hand: newOnHand
          },
          409
        );
      }
      const ledgerRun = await env.DB_INVENTORY.prepare(
        `INSERT OR IGNORE INTO stock_ledger
		(product_id, qty_delta, reason_code, reference_type, reference_id, reason, notes, entered_by)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
      ).bind(productId, qtyDelta, reasonCode, referenceType, referenceId, reason, notes, enteredBy).run();
      const applied = Number(ledgerRun?.meta?.changes ?? 0) > 0;
      const ledgerRow = await env.DB_INVENTORY.prepare(
        `SELECT stock_ledger_id, occurred_at, product_id, qty_delta, reason_code,
					reference_type, reference_id, reason, notes, entered_by
			FROM stock_ledger
			WHERE product_id = ? AND reference_type = ? AND reference_id = ?`
      ).bind(productId, referenceType, referenceId).first();
      if (applied) {
        // UPSERT instead of UPDATE — no need for a separate ensure-row-exists call
        await env.DB_INVENTORY.prepare(
          `INSERT INTO stock_balance (product_id, qty_on_hand, qty_reserved, updated_at)
           VALUES (?, ?, 0, datetime('now'))
           ON CONFLICT(product_id) DO UPDATE SET qty_on_hand = ?, updated_at = datetime('now')`
        ).bind(productId, newOnHand, newOnHand).run();
      }
      // Only enqueue recalc jobs if the product requires auto-recalculation
      let accounts = [];
      if (applied && prod.auto_recalc === 1) {
        accounts = await getDistinctAccountsForProduct(env, productId);
        if (accounts.length) {
          const stmts = accounts.map(
            (accountId) => env.DB_INVENTORY.prepare(
              `INSERT INTO recalc_queue (account_id, product_id, requested_at, reason, last_error)
              VALUES (?, ?, datetime('now'), ?, NULL)
              ON CONFLICT(account_id, product_id)
              DO UPDATE SET requested_at = excluded.requested_at,
                            reason = excluded.reason,
                            last_error = NULL`
            ).bind(accountId, productId, `INVENTORY_${reasonCode}`)
          );
          await env.DB_INVENTORY.batch(stmts);
          await env.RECALC_QUEUE.sendBatch(
            accounts.map((accountId) => ({
              body: { account_id: accountId, product_id: productId, reason: `INVENTORY_${reasonCode}` }
            }))
          );
        }
      }
      return json({
        ok: true,
        applied,
        // false means duplicate reference_type/reference_id/product_id
        ledger: ledgerRow,
        product_id: productId,
        qty_delta: qtyDelta,
        reason_code: reasonCode,
        reference_type: referenceType,
        reference_id: referenceId,
        affected_accounts: accounts.length,
        enqueued: applied ? accounts.length : 0,
        new_qty_on_hand: applied ? newOnHand : onHand
      });
    }
    if (url.pathname === "/admin/recalc/enqueue" && req.method === "POST") {
      requireAdmin(req, env);
      let body;
      try {
        body = await req.json();
      } catch {
        return json({ ok: false, error: "Invalid JSON" }, 400);
      }
      const accountId = asInt(body.account_id);
      const productId = asInt(body.product_id);
      const reason = nonEmptyString(body.reason) ?? "MANUAL_RECALC";
      if (!accountId || accountId <= 0) {
        return json({ ok: false, error: "account_id required" }, 400);
      }
      if (!productId || productId <= 0) {
        return json({ ok: false, error: "product_id required" }, 400);
      }
      await env.RECALC_QUEUE.send({
        account_id: accountId,
        product_id: productId,
        reason
      });
      return json({
        ok: true,
        enqueued: 1,
        account_id: accountId,
        product_id: productId,
        reason
      });
    }
    return new Response("Not found", { status: 404 });
  }
};
export {
  index_default as default
};
//# sourceMappingURL=index.js.map
