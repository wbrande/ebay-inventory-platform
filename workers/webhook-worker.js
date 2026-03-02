export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // -----------------------------
    // Private admin endpoints
    // -----------------------------
    if (url.pathname === "/ebay/notifications/history") {
      return handleHistoryRead(request, env);
    }
    if (url.pathname === "/ebay/notifications/mark-processed") {
      return handleMarkProcessed(request, env);
    }

    // Only handle your webhook path for eBay
    if (url.pathname !== "/ebay/notifications") {
      return new Response("Not found", { status: 404 });
    }

    // ------------------------------------------------------------
    // 1) Challenge validation (GET)
    // eBay expects: SHA256(challengeCode + verificationToken + endpoint)
    // ------------------------------------------------------------
    const challengeCode = url.searchParams.get("challenge_code");
    if (request.method === "GET" && challengeCode) {
      const verificationToken = env.VERIFICATION_TOKEN;
      const endpoint = env.ENDPOINT;
      if (!verificationToken || !endpoint) {
        return new Response("Missing VERIFICATION_TOKEN or ENDPOINT in env", {
          status: 500,
        });
      }

      const textToHash = `${challengeCode}${verificationToken}${endpoint}`;
      const hashHex = await sha256Hex(textToHash);

      return new Response(JSON.stringify({ challengeResponse: hashHex }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // ------------------------------------------------------------
    // 2) Notification delivery (POST)
    // Verify X-EBAY-SIGNATURE using getPublicKey(kid) + ECDSA(SHA1)
    // ------------------------------------------------------------
    if (request.method === "POST") {
      const sigB64 = request.headers.get("x-ebay-signature");
      if (!sigB64) {
        // Ignore noise.
        return new Response("OK", { status: 200 });
      }

      // Read body as bytes once (needed for signature verification)
      const bodyBuf = await request.arrayBuffer();

      // Verify signature. If verification fails, return 412 (as eBay suggests).
      const ok = await verifyEbaySignature({
        env,
        ctx,
        signatureHeaderBase64: sigB64,
        payload: bodyBuf,
      });

      if (!ok) {
        return new Response("Invalid signature", { status: 412 });
      }

      // Verified message; store raw body to D1 asynchronously.
      const bodyText = new TextDecoder().decode(bodyBuf);
      console.log("[EBAY_NOTIFICATION_VERIFIED] body:\n" + bodyText);

      // Store raw event can stay async (non-critical)
      ctx.waitUntil(storeEvent({ env, source: "ebay", bodyText }));

      // Inventory reservation IS critical -> await so it can't be canceled
      await processOrderConfirmation({ env, bodyText });

      return new Response("OK", { status: 200 });
    }

    return new Response("Method not allowed", { status: 405 });
  },
};

// -------------------------
// Store to D1: events table
// -------------------------
async function storeEvent({ env, source, bodyText }) {
  try {
    if (!env.DB_SALES_HISTORY) {
      console.log("DB_SALES_HISTORY binding is missing");
      return;
    }

    // Optional: ensure body is JSON
    try {
      JSON.parse(bodyText);
    } catch {
      console.log("Body was not valid JSON; storing anyway");
    }

    const id = crypto.randomUUID();
    const receivedAt = new Date().toISOString();

    await env.DB_SALES_HISTORY.prepare(
      `INSERT INTO events (id, received_at, source, body, status, processed_at)
       VALUES (?, ?, ?, ?, 'new', NULL)`
    )
      .bind(id, receivedAt, source, bodyText)
      .run();
  } catch (e) {
    console.log("Failed to store event:", e);
  }
}

// -------------------------
// Admin: list stored events
// GET /ebay/notifications/history?status=new&limit=50
// Requires header: x-api-key = ADMIN_API_KEY
// -------------------------
async function handleHistoryRead(request, env) {
  const apiKey = request.headers.get("x-api-key");
  if (!env.ADMIN_API_KEY || apiKey !== env.ADMIN_API_KEY) {
    return new Response("Unauthorized", { status: 401 });
  }
  if (!env.DB_SALES_HISTORY) {
    return new Response("DB_SALES_HISTORY binding is missing", { status: 500 });
  }

  const url = new URL(request.url);
  const status = url.searchParams.get("status") || "new";
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 200);

  const { results } = await env.DB_SALES_HISTORY.prepare(
    `SELECT id, received_at, source, status, processed_at, body
     FROM events
     WHERE status = ?
     ORDER BY received_at ASC
     LIMIT ?`
  )
    .bind(status, limit)
    .all();

  return Response.json(results, { headers: { "Cache-Control": "no-store" } });
}

// -------------------------
// Admin: mark processed
// POST /ebay/notifications/mark-processed
// Body: {"id":"..."} OR {"ids":["...","..."]}
// Requires header: x-api-key = ADMIN_API_KEY
// -------------------------
async function handleMarkProcessed(request, env) {
  const apiKey = request.headers.get("x-api-key");
  if (!env.ADMIN_API_KEY || apiKey !== env.ADMIN_API_KEY) {
    return new Response("Unauthorized", { status: 401 });
  }
  if (!env.DB_SALES_HISTORY) {
    return new Response("DB_SALES_HISTORY binding is missing", { status: 500 });
  }
  if (request.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return new Response("Invalid JSON body", { status: 400 });
  }

  const ids = Array.isArray(payload?.ids)
    ? payload.ids
    : payload?.id
      ? [payload.id]
      : null;

  if (!ids || ids.length === 0) {
    return new Response("Missing id or ids", { status: 400 });
  }

  const now = new Date().toISOString();

  const stmt = env.DB_SALES_HISTORY.prepare(
    `UPDATE events
     SET status = 'processed', processed_at = ?
     WHERE id = ?`
  );

  for (const id of ids) {
    await stmt.bind(now, id).run();
  }

  return Response.json({ updated: ids.length }, { headers: { "Cache-Control": "no-store" } });
}

// -------------------------
// eBay signature verification
// -------------------------
async function verifyEbaySignature({ env, ctx, signatureHeaderBase64, payload }) {
  let sigHeader;
  try {
    const jsonStr = b64ToString(signatureHeaderBase64);
    sigHeader = JSON.parse(jsonStr);
  } catch (e) {
    console.log("Failed to decode/parse X-EBAY-SIGNATURE:", e);
    return false;
  }

  const { kid, signature, alg, digest } = sigHeader || {};
  if (!kid || !signature) {
    console.log("X-EBAY-SIGNATURE missing kid/signature:", sigHeader);
    return false;
  }

  if (alg && String(alg).toUpperCase() !== "ECDSA") {
    console.log("Unexpected alg:", alg);
  }
  if (digest && String(digest).toUpperCase() !== "SHA1") {
    console.log("Unexpected digest:", digest);
  }

  const pub = await getEbayPublicKeyCached({ env, ctx, kid });
  if (!pub?.key) {
    console.log("Could not retrieve public key for kid:", kid);
    return false;
  }

  const sigBytes = b64ToBytes(signature);

  const imported = await importEcdsaPublicKey(pub.key);
  if (!imported?.key) return false;

  const partLen = curvePartLen(imported.namedCurve);

  let sigForVerify;
  try {
    if (sigBytes[0] === 0x30) {
      sigForVerify = derEcdsaSigToP1363(sigBytes, partLen);
    } else {
      sigForVerify = sigBytes;
    }
  } catch (e) {
    console.log("Signature normalization failed:", e);
    return false;
  }

  return crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-1" },
    imported.key,
    sigForVerify,
    payload
  );
}

// ---------------------------------
// getPublicKey(kid) with caching
// ---------------------------------
async function getEbayPublicKeyCached({ env, ctx, kid }) {
  const cache = caches.default;
  const cacheUrl = new URL("https://cache.example/ebay/public_key");
  cacheUrl.searchParams.set("kid", kid);
  cacheUrl.searchParams.set("env", env.EBAY_ENV || "production");

  const cacheReq = new Request(cacheUrl.toString(), { method: "GET" });
  const cached = await cache.match(cacheReq);
  if (cached) return cached.json();

  const token = await getEbayApplicationToken({ env });
  if (!token) return null;

  const base = "https://api.ebay.com";

  const resp = await fetch(
    `${base}/commerce/notification/v1/public_key/${encodeURIComponent(kid)}`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
      },
    }
  );

  if (!resp.ok) {
    console.log("getPublicKey failed:", resp.status, await resp.text());
    return null;
  }

  const json = await resp.json();

  const cacheResp = new Response(JSON.stringify(json), {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "public, max-age=3600",
    },
  });
  ctx.waitUntil(cache.put(cacheReq, cacheResp));

  return json;
}

// ---------------------------------
// eBay Application token (client_credentials)
// ---------------------------------
let _tokenMemo = { token: null, expMs: 0 };

async function getEbayApplicationToken({ env }) {
  const now = Date.now();
  if (_tokenMemo.token && now < _tokenMemo.expMs - 30_000) {
    return _tokenMemo.token;
  }

  const clientId = env.EBAY_CLIENT_ID;
  const clientSecret = env.EBAY_CLIENT_SECRET;
  if (!clientId || !clientSecret) {
    console.log("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET");
    return null;
  }

  const base = "https://api.ebay.com";
  const tokenUrl = `${base}/identity/v1/oauth2/token`;

  const basic = btoa(`${clientId}:${clientSecret}`);
  const scope = env.EBAY_OAUTH_SCOPE || "https://api.ebay.com/oauth/api_scope";

  const body = new URLSearchParams();
  body.set("grant_type", "client_credentials");
  body.set("scope", scope);

  const resp = await fetch(tokenUrl, {
    method: "POST",
    headers: {
      Authorization: `Basic ${basic}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });

  if (!resp.ok) {
    console.log("OAuth token request failed:", resp.status, await resp.text());
    return null;
  }

  const json = await resp.json();
  const token = json.access_token;
  const expiresIn = Number(json.expires_in || 0);

  if (!token || !expiresIn) {
    console.log("Unexpected token response:", json);
    return null;
  }

  _tokenMemo.token = token;
  _tokenMemo.expMs = Date.now() + expiresIn * 1000;

  return token;
}

//----------------------------------
// Queue listing edits
//----------------------------------
async function enqueueRecalc(env, accountId, productId, reason) {
  await env.DB_INVENTORY.prepare(
    `INSERT INTO recalc_queue (account_id, product_id, requested_at, reason, last_error)
     VALUES (?, ?, datetime('now'), ?, NULL)
     ON CONFLICT(account_id, product_id)
     DO UPDATE SET requested_at = excluded.requested_at,
                   reason       = excluded.reason,
                   last_error   = NULL`
  ).bind(accountId, productId, reason).run();

  await env.RECALC_QUEUE.send({
    account_id: Number(accountId),
    product_id: Number(productId),
    reason: reason ?? null,
  });
}

/**
 * Reserve stock + update balances + enqueue recalc,
 * using already-resolved listing mapping (no second DB lookup).
 */
async function reserveFromResolvedListing(env, { orderId, orderLineItemId, listingId, qtySold, listing }) {
  const q = Math.max(1, Number(qtySold || 1));
  const listingIdStr = String(listingId);
  const refId = `${String(orderId)}:${String(orderLineItemId)}`;
  const notesBase = `listingId=${listingIdStr}, qtySold=${q}`;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isRetryableD1 = (e) => {
    const msg = String(e?.message || e || "");
    return (
      msg.includes("D1_ERROR") &&
      (msg.includes("exceeded timeout") ||
        msg.includes("object to be reset") ||
        msg.includes("Network connection lost") ||
        msg.includes("connection lost") ||
        msg.includes("reset") ||
        msg.includes("SQLITE_BUSY") ||
        msg.includes("database is locked"))
    );
  };

  const maxAttempts = 5;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      if (!listing?.product_id || !listing?.account_id) {
        console.log("Listing missing product/account mapping:", { listingIdStr, listing });
        return { inserted: 0, account_id: null, product_id: null };
      }

      const productId = Number(listing.product_id);
      const accountId = Number(listing.account_id);
      const ups = Math.max(1, Number(listing.ups || 1));
      const reservedInc = ups * q;

      // 2) Idempotent ledger insert
      const ins = await env.DB_INVENTORY.prepare(
        `INSERT OR IGNORE INTO stock_ledger
           (product_id, qty_delta, reason_code, reference_type, reference_id, reason, notes, entered_by)
         VALUES
           (?, ?, 'RESERVE', 'SALE', ?, 'Reserve on sale', ?, 'ebay-notification')`
      )
        .bind(productId, -reservedInc, refId, `${notesBase}, unitsPerSale=${ups}`)
        .run();

      const inserted = Number(ins?.meta?.changes ?? 0);

      // 3) Only increment reserved balance if we actually inserted the ledger row
      if (inserted > 0) {
        await env.DB_INVENTORY.prepare(
          `INSERT INTO stock_balance (product_id, qty_on_hand, qty_reserved, updated_at)
           VALUES (?, 0, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(product_id) DO UPDATE SET
             qty_reserved = qty_reserved + excluded.qty_reserved,
             updated_at = CURRENT_TIMESTAMP`
        ).bind(productId, reservedInc).run();
      }

      // 4) ALWAYS trigger recalc
      await enqueueRecalc(env, accountId, productId, "sale");

      return { inserted, account_id: accountId, product_id: productId };
    } catch (e) {
      if (attempt < maxAttempts && isRetryableD1(e)) {
        await sleep(150 * attempt + Math.floor(Math.random() * 150));
        continue;
      }
      throw e;
    }
  }
}

/**
 * Process an ORDER_CONFIRMATION:
 * - resolve listing once (product_id/account_id/ups)
 * - optional TEST_MODE gate
 * - reserve + enqueue using resolved listing (no second lookup)
 */
async function processOrderConfirmation({ env, bodyText }) {
  try {
    if (!env.DB_INVENTORY) {
      console.log("DB_INVENTORY binding is missing");
      return;
    }

    let msg;
    try {
      msg = JSON.parse(bodyText);
    } catch (e) {
      console.log("Notification body is not valid JSON:", e);
      return;
    }

    const topic = msg?.metadata?.topic;
    if (topic !== "ORDER_CONFIRMATION") return;

    const order = msg?.notification?.data?.order;
    const orderId = order?.orderId;
    const lineItems = order?.orderLineItems || [];
    if (!orderId || !Array.isArray(lineItems) || lineItems.length === 0) return;

    // ---- TEST MODE (Change #1): config + log once ----
    const TEST_PRODUCT_IDS = new Set([
      // 123,
      // 456,
    ]);
    const TEST_MODE = TEST_PRODUCT_IDS.size > 0;
    console.log("TEST_MODE:", TEST_MODE, "TEST_PRODUCT_IDS:", [...TEST_PRODUCT_IDS]);

    for (const li of lineItems) {
      const orderLineItemId = li?.orderLineItemId;
      const listingId = li?.listingId;
      const qtySold = Number(li?.quantity ?? 1);

      if (!orderLineItemId || !listingId || !Number.isFinite(qtySold) || qtySold <= 0) continue;

      // ---- Change #2: single lookup for mapping + ups ----
      const listing = await env.DB_INVENTORY.prepare(
        `SELECT product_id, account_id, COALESCE(units_per_sale, 1) AS ups
         FROM listings
         WHERE ebay_item_number = ?
         LIMIT 1`
      ).bind(String(listingId)).first();

      if (!listing) {
        console.log("No matching listing for listingId:", listingId);
        continue;
      }

      const productId = Number(listing.product_id);

      if (TEST_MODE && !TEST_PRODUCT_IDS.has(productId)) {
        console.log(
          `[TEST_MODE] Ignoring sale for product_id=${productId}; only processing ${[...TEST_PRODUCT_IDS].join(", ")}`
        );
        continue;
      }

      await reserveFromResolvedListing(env, {
        orderId,
        orderLineItemId,
        listingId,
        qtySold,
        listing,
      });
    }
  } catch (e) {
    console.log("processOrderConfirmation failed:", e);
  }
}

// ---------------------------------
// Crypto helpers
// ---------------------------------
async function sha256Hex(input) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function b64ToString(b64) {
  return atob(b64);
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function importEcdsaPublicKey(pem) {
  const der = pemToSpkiDer(pem);
  const curves = ["P-256", "P-384", "P-521"];
  for (const namedCurve of curves) {
    try {
      const key = await crypto.subtle.importKey("spki", der, { name: "ECDSA", namedCurve }, true, ["verify"]);
      return { key, namedCurve };
    } catch {}
  }
  return null;
}

function curvePartLen(namedCurve) {
  if (namedCurve === "P-256") return 32;
  if (namedCurve === "P-384") return 48;
  if (namedCurve === "P-521") return 66;
  throw new Error("Unknown curve: " + namedCurve);
}

function pemToSpkiDer(pem) {
  const cleaned = pem
    .replace("-----BEGIN PUBLIC KEY-----", "")
    .replace("-----END PUBLIC KEY-----", "")
    .replace(/\s+/g, "");
  const bytes = b64ToBytes(cleaned);
  return bytes.buffer;
}

function derEcdsaSigToP1363(derSig, partLen) {
  const bytes = derSig instanceof Uint8Array ? derSig : new Uint8Array(derSig);
  let i = 0;

  if (bytes[i++] !== 0x30) throw new Error("Not a DER sequence");
  let seqLen = bytes[i++];
  if (seqLen & 0x80) {
    const n = seqLen & 0x7f;
    seqLen = 0;
    for (let k = 0; k < n; k++) seqLen = (seqLen << 8) | bytes[i++];
  }

  if (bytes[i++] !== 0x02) throw new Error("Expected INTEGER (r)");
  let rLen = bytes[i++];
  let r = bytes.slice(i, i + rLen);
  i += rLen;

  if (bytes[i++] !== 0x02) throw new Error("Expected INTEGER (s)");
  let sLen = bytes[i++];
  let s = bytes.slice(i, i + sLen);

  r = stripDerIntLeadingZeros(r);
  s = stripDerIntLeadingZeros(s);

  const rOut = leftPad(r, partLen);
  const sOut = leftPad(s, partLen);

  const out = new Uint8Array(partLen * 2);
  out.set(rOut, 0);
  out.set(sOut, partLen);
  return out;
}

function stripDerIntLeadingZeros(u8) {
  let i = 0;
  while (i < u8.length - 1 && u8[i] === 0x00) i++;
  return u8.slice(i);
}

function leftPad(u8, len) {
  if (u8.length > len) return u8.slice(u8.length - len);
  if (u8.length === len) return u8;
  const out = new Uint8Array(len);
  out.set(u8, len - u8.length);
  return out;
}
