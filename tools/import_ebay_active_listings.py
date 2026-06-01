#!/usr/bin/env python3

# Import eBay "All active listings" CSV into Cloudflare D1 via generated SQL.
#
# Goal:
# - Process ALL listings where Condition = "New" (single items and multi-packs).
# - Insert ONLY missing products(upc) (NOT EXISTS; avoids AUTOINCREMENT "burn" on ignored inserts)
# - Insert ONLY missing listings (NOT EXISTS; avoids AUTOINCREMENT "burn")
# - Update existing listings (plain UPDATE; never touches sqlite_sequence)
# - Does NOT touch stock_balance or stock_ledger.
#
# Usage:
# python import_ebay_active_listings.py --csv "C:\Users\PC\Downloads\eBay-all-active-listings-report.csv" --out import.sql --db ebay_information --account-id 1
# npx wrangler d1 execute ebay_information --remote --file import.sql
#
# If you omit --apply, it only generates the SQL file.
#
# Notes:
# - eBay headers vary; this script is pinned to the headers you provided.
# - It writes ONE INSERT per statement to avoid D1 statement-length limits.
# - Intentionally avoids PRAGMA / BEGIN / TEMP / DDL in generated SQL to prevent SQLITE_AUTH on D1 remote.


from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from typing import Dict, Optional, Set, Tuple


# -------------------------
# Exact headers (as you provided)
# -------------------------
H_ITEM_NUMBER = "Item number"
H_TITLE = "Title"
H_VARIATION_DETAILS = "Variation details"
H_SKU = "Custom label (SKU)"
H_AVAILABLE_QTY = "Available quantity"
H_FORMAT = "Format"
H_CURRENCY = "Currency"
H_START_PRICE = "Start price"
H_AUCTION_BIN = "Auction Buy It Now price"
H_RESERVE_PRICE = "Reserve price"
H_CURRENT_PRICE = "Current price"
H_SOLD_QTY = "Sold quantity"
H_WATCHERS = "Watchers"
H_BIDS = "Bids"
H_START_DATE = "Start date"
H_END_DATE = "End date"
H_CAT1_NAME = "eBay category 1 name"
H_CAT1_NUM = "eBay category 1 number"
H_CAT2_NAME = "eBay category 2 name"
H_CAT2_NUM = "eBay category 2 number"
H_CONDITION = "Condition"
H_PRO_GRADER = "CD:Professional Grader - (ID: 27501)"
H_GRADE = "CD:Grade - (ID: 27502)"
H_CERT_NUM = "CDA:Certification Number - (ID: 27503)"
H_CARD_COND = "CD:Card Condition - (ID: 40001)"
H_EPID = "eBay Product ID(ePID)"
H_LISTING_SITE = "Listing site"
H_UPC = "P:UPC"
H_EAN = "P:EAN"
H_ISBN = "P:ISBN"


# -------------------------
# Pack-size parser
# -------------------------
PACK_PATTERNS = [
    # "2 PK", "2PK", "2-PK", "2 PACK", "2-PACK"
    re.compile(r"\b(\d{1,3})\s*[- ]?\s*(PK|PACK)\b", re.IGNORECASE),
]


def parse_units_per_sale(title: str) -> int:
    if not title:
        return 1
    best = 1
    for rx in PACK_PATTERNS:
        for m in rx.finditer(title):
            # In pattern 1, number is group(1). In pattern 2, number is group(2).
            g1 = m.group(1)
            g2 = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            n_str = g1 if (g1 and g1.isdigit()) else (g2 if (g2 and g2.isdigit()) else "")
            if not n_str:
                if m.lastindex and m.lastindex >= 2 and m.group(2).isdigit():
                    n_str = m.group(2)
            try:
                n = int(n_str) if n_str else 0
            except Exception:
                continue
            if 1 <= n <= 999 and n > best:
                best = n
    return best


# -------------------------
# Parsing + normalization helpers
# -------------------------
def normalize_upc(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    digits = re.sub(r"\D+", "", s)
    if not digits:
        return ""

    # Canonicalize EAN-13 that is just UPC-A with leading 0
    if len(digits) == 13 and digits.startswith("0"):
        digits = digits[1:]

    return digits


def parse_int(raw: str) -> Optional[int]:
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"[^\d\-]", "", s)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_float(raw: str) -> Optional[float]:
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"[^0-9\.\-]", "", s)
    if not s or s in {".", "-", "-."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# -------------------------
# SQL escaping helpers
# -------------------------
def sql_quote(s: str) -> str:
    return "'" + (s or "").replace("'", "''") + "'"


def sql_text_or_null(s: str) -> str:
    return "NULL" if not (s or "").strip() else sql_quote(s.strip())


def sql_int_or_null(v: Optional[int]) -> str:
    return "NULL" if v is None else str(v)


def sql_num_or_null(v: Optional[float]) -> str:
    return "NULL" if v is None else str(v)


# -------------------------
# Core two-pass logic
# -------------------------
def is_condition_new(cond: str) -> bool:
    return (cond or "").strip().lower() == "new"


def scan_relevant_upcs(csv_path: str, encoding: str) -> Dict[str, int]:
    """
    Pass 1:
    Collect every UPC from Condition="New" listings and count how many distinct
    ebay_item_numbers share each UPC.
    - count >= 2 → multipack product (auto_recalc = 1)
    - count == 1 → single-listing product (auto_recalc = 0)
    """
    upc_counts: Dict[str, int] = {}
    seen_pairs: Set[Tuple[str, str]] = set()
    with open(csv_path, "r", encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cond = (row.get(H_CONDITION) or "").strip()
            if not is_condition_new(cond):
                continue

            upc = normalize_upc(row.get(H_UPC) or "")
            item_number = (row.get(H_ITEM_NUMBER) or "").strip()
            if not upc or not item_number:
                continue

            pair = (upc, item_number)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                upc_counts[upc] = upc_counts.get(upc, 0) + 1
    return upc_counts


def _listing_value_literals(account_id: int, data: dict) -> str:
    """Return the SELECT literal columns for one listing in a batched UPSERT."""
    return (
        f"{account_id}, p.product_id, {data['units']},\n"
        f"  {sql_quote(data['item_number'])}, {sql_text_or_null(data['title'])}, "
        f"{sql_text_or_null(data['variation_details'])}, {sql_text_or_null(data['sku'])},\n"
        f"  {sql_int_or_null(data['available_qty'])}, {sql_text_or_null(data['fmt'])}, "
        f"{sql_text_or_null(data['currency'])},\n"
        f"  {sql_num_or_null(data['start_price'])}, {sql_num_or_null(data['auction_bin'])}, "
        f"{sql_num_or_null(data['reserve_price'])}, {sql_num_or_null(data['current_price'])},\n"
        f"  {sql_int_or_null(data['sold_qty'])}, {sql_int_or_null(data['watchers'])}, "
        f"{sql_int_or_null(data['bids'])},\n"
        f"  {sql_text_or_null(data['start_date'])}, {sql_text_or_null(data['end_date'])},\n"
        f"  {sql_text_or_null(data['cat1_name'])}, {sql_int_or_null(data['cat1_num'])},\n"
        f"  {sql_text_or_null(data['cat2_name'])}, {sql_int_or_null(data['cat2_num'])},\n"
        f"  {sql_text_or_null(data['condition'])}, {sql_text_or_null(data['professional_grader'])}, "
        f"{sql_text_or_null(data['grade'])}, {sql_text_or_null(data['certification_number'])}, "
        f"{sql_text_or_null(data['card_condition'])},\n"
        f"  {sql_text_or_null(data['ebay_product_id'])}, {sql_text_or_null(data['listing_site'])},\n"
        f"  {sql_text_or_null(data['upc'])}, {sql_text_or_null(data['ean'])}, "
        f"{sql_text_or_null(data['isbn'])},\n"
        f"  CURRENT_TIMESTAMP"
    )


def _cte_value_row(data: dict) -> str:
    """Return the VALUES row for one listing inside the CTE (no account_id, product_id, or CURRENT_TIMESTAMP)."""
    return (
        f"{sql_quote(data['upc'])}, {data['units']},\n"
        f"  {sql_text_or_null(data['item_number'])}, {sql_text_or_null(data['title'])}, "
        f"{sql_text_or_null(data['variation_details'])}, {sql_text_or_null(data['sku'])},\n"
        f"  {sql_int_or_null(data['available_qty'])}, {sql_text_or_null(data['fmt'])}, "
        f"{sql_text_or_null(data['currency'])},\n"
        f"  {sql_num_or_null(data['start_price'])}, {sql_num_or_null(data['auction_bin'])}, "
        f"{sql_num_or_null(data['reserve_price'])}, {sql_num_or_null(data['current_price'])},\n"
        f"  {sql_int_or_null(data['sold_qty'])}, {sql_int_or_null(data['watchers'])}, "
        f"{sql_int_or_null(data['bids'])},\n"
        f"  {sql_text_or_null(data['start_date'])}, {sql_text_or_null(data['end_date'])},\n"
        f"  {sql_text_or_null(data['cat1_name'])}, {sql_int_or_null(data['cat1_num'])},\n"
        f"  {sql_text_or_null(data['cat2_name'])}, {sql_int_or_null(data['cat2_num'])},\n"
        f"  {sql_text_or_null(data['condition'])}, {sql_text_or_null(data['professional_grader'])}, "
        f"{sql_text_or_null(data['grade'])}, {sql_text_or_null(data['certification_number'])}, "
        f"{sql_text_or_null(data['card_condition'])},\n"
        f"  {sql_text_or_null(data['ebay_product_id'])}, {sql_text_or_null(data['listing_site'])},\n"
        f"  {sql_text_or_null(data['ean'])}, {sql_text_or_null(data['isbn'])}"
    )


_CTE_COLUMNS = (
    "upc, units, item_number, title, variation_details, sku,\n"
    "  available_qty, fmt, currency,\n"
    "  start_price, auction_bin, reserve_price, current_price,\n"
    "  sold_qty, watchers, bids,\n"
    "  start_date, end_date,\n"
    "  cat1_name, cat1_num,\n"
    "  cat2_name, cat2_num,\n"
    "  condition, professional_grader, grade, certification_number, card_condition,\n"
    "  ebay_product_id, listing_site,\n"
    "  ean, isbn"
)


_LISTING_COLUMNS = (
    "account_id, product_id, units_per_sale,\n"
    "  ebay_item_number, title, variation_details, sku,\n"
    "  available_quantity, format, currency,\n"
    "  start_price, auction_buy_it_now_price, reserve_price, current_price,\n"
    "  sold_quantity, watchers, bids,\n"
    "  start_date, end_date,\n"
    "  ebay_category_1_name, ebay_category_1_number,\n"
    "  ebay_category_2_name, ebay_category_2_number,\n"
    "  condition, professional_grader, grade, certification_number, card_condition,\n"
    "  ebay_product_id, listing_site,\n"
    "  upc, ean, isbn,\n"
    "  updated_at"
)

_LISTING_DO_UPDATE = (
    "  product_id = excluded.product_id,\n"
    "  units_per_sale = excluded.units_per_sale,\n"
    "  title = excluded.title,\n"
    "  variation_details = excluded.variation_details,\n"
    "  sku = excluded.sku,\n"
    "  available_quantity = excluded.available_quantity,\n"
    "  format = excluded.format,\n"
    "  currency = excluded.currency,\n"
    "  start_price = excluded.start_price,\n"
    "  auction_buy_it_now_price = excluded.auction_buy_it_now_price,\n"
    "  reserve_price = excluded.reserve_price,\n"
    "  current_price = excluded.current_price,\n"
    "  sold_quantity = excluded.sold_quantity,\n"
    "  watchers = excluded.watchers,\n"
    "  bids = excluded.bids,\n"
    "  start_date = excluded.start_date,\n"
    "  end_date = excluded.end_date,\n"
    "  ebay_category_1_name = excluded.ebay_category_1_name,\n"
    "  ebay_category_1_number = excluded.ebay_category_1_number,\n"
    "  ebay_category_2_name = excluded.ebay_category_2_name,\n"
    "  ebay_category_2_number = excluded.ebay_category_2_number,\n"
    "  condition = excluded.condition,\n"
    "  professional_grader = excluded.professional_grader,\n"
    "  grade = excluded.grade,\n"
    "  certification_number = excluded.certification_number,\n"
    "  card_condition = excluded.card_condition,\n"
    "  ebay_product_id = excluded.ebay_product_id,\n"
    "  listing_site = excluded.listing_site,\n"
    "  upc = excluded.upc,\n"
    "  ean = excluded.ean,\n"
    "  isbn = excluded.isbn,\n"
    "  updated_at = CURRENT_TIMESTAMP"
)

_BATCH = 50  # listings per UPSERT statement


def iter_import_statements(
    csv_path: str,
    account_id: int,
    upc_counts: Dict[str, int],
    encoding: str,
):
    """
    Generator that yields (category, stmt_type, sql, row_count) tuples.

    Batches products and stock_balance into single INSERT / CASE-UPDATE
    statements, and listings into UPSERT batches of {_BATCH}, to minimise
    HTTP round-trips when executed via the D1 REST API.

    Yields:
      ("products", "INSERT", sql, upc_count)
      ("products", "UPDATE", sql, upc_count)
      ("listings", "UPSERT", sql, listing_count_in_batch)
      ("stock_balance", "INSERT", sql, upc_count)
      ("stock_balance", "UPDATE", sql, single_upc_count)
    """
    sorted_upcs = sorted(upc_counts)

    # ---------- Phase 1: products (1 INSERT batch + 1 UPDATE batch) ----------
    insert_vals = []
    update_whens = []
    upc_ins = []
    for upc in sorted_upcs:
        ar = 1 if upc_counts[upc] >= 2 else 0
        insert_vals.append(f"({sql_quote(upc)}, {ar})")
        update_whens.append(f"WHEN {sql_quote(upc)} THEN {ar}")
        upc_ins.append(sql_quote(upc))

    if insert_vals:
        yield ("products", "INSERT",
               "INSERT OR IGNORE INTO products (upc, auto_recalc) VALUES\n"
               + ",\n".join(insert_vals) + ";",
               len(insert_vals))

    if update_whens:
        case = "CASE upc " + " ".join(update_whens) + " END"
        in_list = ", ".join(upc_ins)
        yield ("products", "UPDATE",
               "UPDATE products SET auto_recalc = " + case
               + ", updated_at = CURRENT_TIMESTAMP\n"
               + "WHERE upc IN (" + in_list + ")\n"
               + "  AND auto_recalc != " + case + ";",
               len(update_whens))

    # ---------- Phase 2: listings (UPSERT batches) ----------
    seen_item_numbers: Set[str] = set()
    single_qty: Dict[str, int] = {}  # UPC -> available_qty (single-listing only)
    batch: list = []                 # accumulated listing dicts

    def _flush_listing_batch():
        if not batch:
            return
        values_rows = []
        for d in batch:
            values_rows.append(
                "(" + _cte_value_row(d) + ")"
            )
        sql = ("WITH cte(" + _CTE_COLUMNS + ") AS (\n"
               "  VALUES\n    "
               + ",\n    ".join(values_rows) + "\n"
               + ")\n"
               + "INSERT INTO listings (\n  " + _LISTING_COLUMNS + "\n)\n"
               + "SELECT " + str(account_id) + ", p.product_id, cte.units,\n"
               + "  cte.item_number, cte.title, cte.variation_details, cte.sku,\n"
               + "  cte.available_qty, cte.fmt, cte.currency,\n"
               + "  cte.start_price, cte.auction_bin, cte.reserve_price, cte.current_price,\n"
               + "  cte.sold_qty, cte.watchers, cte.bids,\n"
               + "  cte.start_date, cte.end_date,\n"
               + "  cte.cat1_name, cte.cat1_num,\n"
               + "  cte.cat2_name, cte.cat2_num,\n"
               + "  cte.condition, cte.professional_grader, cte.grade, cte.certification_number, cte.card_condition,\n"
               + "  cte.ebay_product_id, cte.listing_site,\n"
               + "  cte.upc, cte.ean, cte.isbn,\n"
               + "  CURRENT_TIMESTAMP\n"
               + "FROM cte\n"
               + "JOIN products p ON p.upc = cte.upc\n"
               + "ON CONFLICT(account_id, ebay_item_number) DO UPDATE SET\n"
               + _LISTING_DO_UPDATE + ";")
        yield ("listings", "UPSERT", sql, len(batch))
        batch.clear()

    with open(csv_path, "r", encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        required = {H_ITEM_NUMBER, H_TITLE, H_UPC}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"CSV missing required headers: {sorted(missing)}")

        for row in reader:
            item_number = (row.get(H_ITEM_NUMBER) or "").strip()
            if not item_number:
                continue
            cond = (row.get(H_CONDITION) or "").strip()
            if not is_condition_new(cond):
                continue
            title = (row.get(H_TITLE) or "").strip()
            upc = normalize_upc(row.get(H_UPC) or "")
            if not upc or upc not in upc_counts:
                continue
            if item_number in seen_item_numbers:
                continue
            seen_item_numbers.add(item_number)

            available_qty = parse_int(row.get(H_AVAILABLE_QTY) or "")
            if upc_counts.get(upc, 0) == 1:
                single_qty[upc] = available_qty if available_qty is not None else 0

            data = {
                "units": parse_units_per_sale(title),
                "item_number": item_number,
                "title": title,
                "variation_details": (row.get(H_VARIATION_DETAILS) or "").strip(),
                "sku": (row.get(H_SKU) or "").strip(),
                "available_qty": available_qty,
                "fmt": (row.get(H_FORMAT) or "").strip(),
                "currency": (row.get(H_CURRENCY) or "").strip(),
                "start_price": parse_float(row.get(H_START_PRICE) or ""),
                "auction_bin": parse_float(row.get(H_AUCTION_BIN) or ""),
                "reserve_price": parse_float(row.get(H_RESERVE_PRICE) or ""),
                "current_price": parse_float(row.get(H_CURRENT_PRICE) or ""),
                "sold_qty": parse_int(row.get(H_SOLD_QTY) or ""),
                "watchers": parse_int(row.get(H_WATCHERS) or ""),
                "bids": parse_int(row.get(H_BIDS) or ""),
                "start_date": (row.get(H_START_DATE) or "").strip(),
                "end_date": (row.get(H_END_DATE) or "").strip(),
                "cat1_name": (row.get(H_CAT1_NAME) or "").strip(),
                "cat1_num": parse_int(row.get(H_CAT1_NUM) or ""),
                "cat2_name": (row.get(H_CAT2_NAME) or "").strip(),
                "cat2_num": parse_int(row.get(H_CAT2_NUM) or ""),
                "condition": (row.get(H_CONDITION) or "").strip(),
                "professional_grader": (row.get(H_PRO_GRADER) or "").strip(),
                "grade": (row.get(H_GRADE) or "").strip(),
                "certification_number": (row.get(H_CERT_NUM) or "").strip(),
                "card_condition": (row.get(H_CARD_COND) or "").strip(),
                "ebay_product_id": (row.get(H_EPID) or "").strip(),
                "listing_site": (row.get(H_LISTING_SITE) or "").strip(),
                "upc": upc,
                "ean": (row.get(H_EAN) or "").strip(),
                "isbn": (row.get(H_ISBN) or "").strip(),
            }
            batch.append(data)
            if len(batch) >= _BATCH:
                yield from _flush_listing_batch()

    yield from _flush_listing_batch()  # flush remaining

    # ---------- Phase 3: stock_balance (1 INSERT batch + 1 UPDATE batch) ----------
    sb_insert_cols = []   # (upc, qty) for the CASE expression
    sb_update_whens = []  # single-listing UPCs only
    sb_update_ins = []
    for upc in sorted_upcs:
        is_single = upc_counts[upc] == 1 and upc in single_qty
        qty = single_qty.get(upc, 0) if is_single else 0
        sb_insert_cols.append((upc, qty))
        if is_single:
            sb_update_whens.append(f"WHEN {sql_quote(upc)} THEN {qty}")
            sb_update_ins.append(sql_quote(upc))

    if sb_insert_cols:
        whens = " ".join(f"WHEN {sql_quote(u)} THEN {q}" for u, q in sb_insert_cols)
        in_list = ", ".join(sql_quote(u) for u, _ in sb_insert_cols)
        yield ("stock_balance", "INSERT",
               "INSERT OR IGNORE INTO stock_balance (product_id, qty_on_hand, qty_reserved)\n"
               + "SELECT p.product_id, CASE p.upc " + whens + " END, 0\n"
               + "FROM products p\n"
               + "WHERE p.upc IN (" + in_list + ");",
               len(sb_insert_cols))

    if sb_update_whens:
        case_p2 = "CASE p2.upc " + " ".join(sb_update_whens) + " END"
        case_p3 = "CASE p3.upc " + " ".join(sb_update_whens) + " END"
        in_list = ", ".join(sb_update_ins)
        yield ("stock_balance", "UPDATE",
               "UPDATE stock_balance SET qty_on_hand = (\n"
               + "  SELECT " + case_p2 + "\n"
               + "  FROM products p2 WHERE p2.product_id = stock_balance.product_id\n"
               + "), updated_at = CURRENT_TIMESTAMP\n"
               + "WHERE product_id IN (SELECT product_id FROM products WHERE upc IN ("
               + in_list + "))\n"
               + "  AND qty_on_hand != (\n"
               + "    SELECT " + case_p3 + "\n"
               + "    FROM products p3 WHERE p3.product_id = stock_balance.product_id\n"
               + "  );",
               len(sb_update_whens))


def generate_sql(
    csv_path: str,
    out_path: str,
    account_id: int,
    upc_counts: Dict[str, int],
    encoding: str,
) -> Tuple[int, int, int]:
    """Write batched SQL to file.  Returns (rows_scanned, listings_emitted, distinct_upcs_used)."""
    listings_emitted = 0

    with open(out_path, "w", encoding="utf-8") as out:
        out.write("-- Generated by import_ebay_active_listings.py (batched)\n")
        out.write("-- NOTE: Intentionally avoids PRAGMA/BEGIN/TEMP/DDL to prevent SQLITE_AUTH on D1 remote.\n\n")

        out.write("-- Products: insert missing + update auto_recalc\n")
        for category, stmt_type, sql, _row_count in iter_import_statements(
            csv_path, account_id, upc_counts, encoding
        ):
            if category == "products":
                out.write(sql + "\n\n")
            elif category == "listings":
                out.write(sql + "\n\n")
                listings_emitted += _row_count
            elif category == "stock_balance":
                out.write(sql + "\n\n")

    with open(csv_path, "r", encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        rows_scanned = sum(1 for _ in reader)

    return rows_scanned, listings_emitted, len(upc_counts)


def run_wrangler_execute(db_name: str, sql_path: str, remote: bool = True) -> int:
    cmd = ["npx", "wrangler", "d1", "execute", db_name]
    if remote:
        cmd.append("--remote")
    cmd.extend(["--file", sql_path])
    print("\nRunning:", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Path to eBay Active Listings CSV")
    ap.add_argument("--out", default="import.sql", help="Output SQL file path")
    ap.add_argument("--account-id", type=int, default=1, help="account_id to use in listings")
    ap.add_argument("--encoding", default="utf-8-sig", help="CSV encoding (utf-8-sig handles BOM)")
    ap.add_argument("--apply", action="store_true", help="Run wrangler d1 execute after generating SQL")
    ap.add_argument("--db", help="D1 database name (required for --apply)")
    ap.add_argument("--local", action="store_true", help="Apply to local D1 (omit --remote)")

    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"CSV not found: {args.csv}", file=sys.stderr)
        return 2

    # Pass 1: find all UPCs with Condition = "New" and count listings per UPC
    upc_counts = scan_relevant_upcs(args.csv, args.encoding)
    multipack_count = sum(1 for c in upc_counts.values() if c >= 2)
    print(f"Relevant UPCs found (Condition=New): {len(upc_counts)} ({multipack_count} multipack)", file=sys.stderr)

    # Pass 2: generate SQL for those UPCs
    rows_scanned, listings_emitted, upcs_used = generate_sql(
        csv_path=args.csv,
        out_path=args.out,
        account_id=args.account_id,
        upc_counts=upc_counts,
        encoding=args.encoding,
    )

    print(f"Rows scanned:          {rows_scanned}", file=sys.stderr)
    print(f"Distinct UPCs emitted: {upcs_used}", file=sys.stderr)
    print(f"Listings SQL emitted:  {listings_emitted}", file=sys.stderr)
    print(f"SQL written to:        {args.out}", file=sys.stderr)

    if args.apply:
        if not args.db:
            print("--apply requires --db YOUR_D1_DB_NAME", file=sys.stderr)
            return 2
        return run_wrangler_execute(args.db, args.out, remote=(not args.local))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())