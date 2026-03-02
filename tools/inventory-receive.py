#!/usr/bin/env python3
"""
Textual TUI for receiving shipments / adjusting inventory via Cloudflare Worker:
POST /admin/inventory/move

Install:
  pip install textual requests python-dotenv

Env (.env supported):
  INVENTORY_WORKER_URL=...
  INVENTORY_API_KEY=...

  # optional for product search + reason dropdown + stock display
  CLOUDFLARE_API_TOKEN=...
  CLOUDFLARE_ACCOUNT_ID=...
  CLOUDFLARE_D1_DATABASE_ID=...

Run:
  python inventory_tui.py
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from textual import on, work, events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Select,
    Static,
    TextArea,
)

# Optional .env
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass


# -----------------------------
# Cloudflare D1 REST client
# -----------------------------
class CloudflareD1Error(RuntimeError):
    pass


@dataclass(frozen=True)
class D1Config:
    account_id: str
    database_id: str
    api_token: str
    base_url: str = "https://api.cloudflare.com/client/v4"


class D1Client:
    """POST /accounts/{account_id}/d1/database/{database_id}/query"""

    def __init__(self, cfg: D1Config, timeout_s: int = 30) -> None:
        self.cfg = cfg
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {cfg.api_token}", "Content-Type": "application/json"}
        )

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        url = (
            f"{self.cfg.base_url}/accounts/{self.cfg.account_id}"
            f"/d1/database/{self.cfg.database_id}/query"
        )
        payload: Dict[str, Any] = {"sql": sql}
        if params is not None:
            payload["params"] = list(params)

        r = self.session.post(url, json=payload, timeout=self.timeout_s)
        if not r.ok:
            raise CloudflareD1Error(f"HTTP {r.status_code}: {r.text}")

        data = r.json()
        if not data.get("success", False):
            raise CloudflareD1Error(f"API error: {data.get('errors') or data}")

        result = data.get("result")
        if result is None:
            raise CloudflareD1Error(f"Unexpected response: {data}")
        return result


def make_d1_client_from_env() -> Optional[D1Client]:
    tok = os.getenv("CLOUDFLARE_API_TOKEN")
    acc = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    dbid = os.getenv("CLOUDFLARE_D1_DATABASE_ID")
    if not (tok and acc and dbid):
        return None
    return D1Client(D1Config(account_id=acc, database_id=dbid, api_token=tok))


# -----------------------------
# Worker client
# -----------------------------
class InventoryWorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    base_url: str
    api_key: Optional[str] = None
    timeout_s: int = 30


class InventoryWorkerClient:
    def __init__(self, cfg: WorkerConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()

    def move(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.cfg.base_url.rstrip("/") + "/admin/inventory/move"
        headers = {"content-type": "application/json"}
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key

        r = self.session.post(url, headers=headers, json=payload, timeout=self.cfg.timeout_s)

        if r.status_code == 401:
            raise InventoryWorkerError("Unauthorized (check INVENTORY_API_KEY / ADMIN_API_KEY).")

        try:
            data = r.json()
        except Exception:
            raise InventoryWorkerError(f"HTTP {r.status_code}: {r.text}")

        if not r.ok or not data.get("ok", False):
            raise InventoryWorkerError(f"HTTP {r.status_code}: {json.dumps(data, indent=2)}")

        return data


# -----------------------------
# D1 helper queries
# -----------------------------
def d1_fetch_reasons(d1: D1Client) -> List[Tuple[str, str]]:
    sql = """
        SELECT reason_code, COALESCE(description,'') AS description
        FROM stock_ledger_reason_codes
        WHERE is_active = 1
        ORDER BY reason_code
    """
    res = d1.query(sql)
    rows = res[0].get("results") or []
    out: List[Tuple[str, str]] = []
    for r in rows:
        if isinstance(r, dict) and "reason_code" in r:
            out.append((str(r["reason_code"]), str(r.get("description", "") or "")))
    return out


def d1_find_products(d1: D1Client, q: str, limit: int = 50) -> List[Dict[str, Any]]:
    q = (q or "").strip()
    if not q:
        return []
    try:
        pid = int(q)
    except ValueError:
        pid = None

    sql = """
        SELECT p.product_id, p.upc, p.model, p.brand,
               COALESCE(sb.qty_on_hand, 0) AS qty_on_hand,
               COALESCE(sb.qty_reserved, 0) AS qty_reserved
        FROM products p
        LEFT JOIN stock_balance sb ON sb.product_id = p.product_id
        WHERE (? IS NOT NULL AND p.product_id = ?)
           OR (p.upc IS NOT NULL AND p.upc LIKE '%' || ? || '%')
           OR (p.model IS NOT NULL AND p.model LIKE '%' || ? || '%')
           OR (p.brand IS NOT NULL AND p.brand LIKE '%' || ? || '%')
        ORDER BY p.product_id
        LIMIT ?
    """
    params = [pid, pid, q, q, q, int(limit)]
    res = d1.query(sql, params=params)
    rows = res[0].get("results") or []
    return [r for r in rows if isinstance(r, dict)]


def d1_get_stock(d1: D1Client, product_id: int) -> Optional[Dict[str, Any]]:
    sql = """
        SELECT product_id, qty_on_hand, qty_reserved, updated_at
        FROM stock_balance
        WHERE product_id = ?
    """
    res = d1.query(sql, params=[int(product_id)])
    rows = res[0].get("results") or []
    if rows and isinstance(rows[0], dict):
        return rows[0]
    return None


# -----------------------------
# Utility
# -----------------------------
def guess_entered_by() -> str:
    return os.getenv("USERNAME") or os.getenv("USER") or os.getenv("LOGNAME") or "unknown"


def log_action(entry: Dict[str, Any]) -> None:
    path = os.path.join(os.path.dirname(__file__), "inventory_receive_log.jsonl")
    entry = dict(entry)
    entry["logged_at_epoch"] = int(time.time())
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def new_ref_id(prefix: str) -> str:
    return f"{prefix.lower()}-{int(time.time())}"


# -----------------------------
# Textual UI
# -----------------------------
CSS = """
Screen {
    layout: vertical;
}
#body {
    height: 1fr;
}
#left, #right {
    width: 1fr;
    height: 1fr;
    border: round $panel;
    padding: 1;
}
#left { min-width: 54; }
#right { min-width: 54; }

.section_title {
    text-style: bold;
    margin-bottom: 1;
}
.hint {
    color: $text-muted;
}
#status {
    height: auto;
    border: round $panel;
    padding: 1;
}
Input.-invalid {
    border: heavy $error;
}
#tpl_row {
    layout: grid;
    grid-size: 2 2;      /* 2 columns x 2 rows */
    grid-gutter: 1 0;    /* column gap, row gap */
}
#tpl_row Button {
    height: 1;
    min-height: 1;
    padding: 0 1;
    border: none;
    margin-right: 1;
}
#qty_delta, #reference_id, #entered_by, #reference_type, #notes, #reason_text {
    height: 1;
    min-height: 1;
    border: none;
    padding: 0 1;
    color: white;
}
Input:focus {
    color: white;
    background: $boost;
}
.field_label {
    color: #999999;
}

"""


class InventoryTUI(App):
    CSS = CSS
    TITLE = "Inventory Receiver"
    SUB_TITLE = "Templates + Scan Mode"

    d1_enabled: bool = reactive(False)
    selected_product_id: Optional[int] = reactive(None)

    scan_mode: bool = reactive(True)

    def __init__(self) -> None:
        super().__init__()
        worker_url = os.getenv("INVENTORY_WORKER_URL")
        if not worker_url:
            print("Missing INVENTORY_WORKER_URL. Set env var or .env file.")
            raise SystemExit(2)

        api_key = os.getenv("INVENTORY_API_KEY")
        self.worker = InventoryWorkerClient(WorkerConfig(base_url=worker_url, api_key=api_key))
        self.d1 = make_d1_client_from_env()
        self.d1_enabled = self.d1 is not None

        self._reasons: List[Tuple[str, str]] = []
        self._products: List[Dict[str, Any]] = []

        # Used for scan-enter flow
        self._scan_submit_pending: bool = False

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="body"):
            # LEFT: product search + results
            with Vertical(id="left"):
                yield Static("Product", classes="section_title")
                yield Label(
                    "Scan mode: scan UPC into search box and press Enter (scanner usually does Enter).",
                    classes="hint",
                )
                with Horizontal():
                    yield Input(placeholder="Search… (UPC/model/brand/product_id)", id="product_search")
                    yield Button("Scan: ON", id="toggle_scan")

                yield LoadingIndicator(id="product_loading")

                table = DataTable(id="product_table")
                table.add_columns("product_id", "upc", "model", "brand", "on_hand", "reserved")
                yield table
                yield Static("", id="product_note")

            # RIGHT: move form
            with VerticalScroll(id="right"):
                yield Static("Move / Receive", classes="section_title")

                # Action templates
                yield Label("Action templates", classes="field_label")
                with Horizontal(id="tpl_row"):
                    yield Button("Receive Shipment", id="tpl_receive")
                    yield Button("Customer Return", id="tpl_return")
                    yield Button("Damage / Write-off", id="tpl_damage")
                    yield Button("Adjust Count", id="tpl_adjust")

                yield Static("")

                # If no D1, allow manual product_id entry
                yield Label("Selected product_id", classes="field_label")
                yield Input(placeholder="(select from left, or type product_id)", id="product_id")

                yield Label("Current stock (if available)", classes="field_label")
                yield Static("—", id="stock_line")

                yield Label("qty_delta (Enter submits)", classes="field_label")
                yield Input(placeholder="e.g. 5", id="qty_delta")

                yield Label("reason_code", classes="field_label")
                yield Select(options=[("RECEIVE", "RECEIVE")], id="reason_code")

                yield Label("reference_type", classes="field_label")
                yield Input(value="MANUAL", id="reference_type")

                yield Label("reference_id (unique per shipment/return)", classes="field_label")
                yield Input(value=new_ref_id("manual"), id="reference_id")

                yield Label("notes (optional)", classes="field_label")
                yield TextArea(id="notes")

                yield Label("reason text (optional)", classes="field_label")
                yield Input(placeholder="", id="reason_text")

                yield Label("entered_by", classes="field_label")
                yield Input(value=guess_entered_by(), id="entered_by")

                with Horizontal():
                    yield Button("Submit", id="submit")
                    yield Button("Clear", id="clear")
                    yield Button("Refresh reasons", id="refresh_reasons")

        yield Static(self._initial_status_text(), id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#product_loading", LoadingIndicator).display = False
        self.load_reasons()
        self.query_one("#product_search", Input).focus()

        if not self.d1_enabled:
            self.set_status(
                "D1 lookup is disabled (missing CLOUDFLARE_* env vars). "
                "You can still type product_id manually."
            )

        self._update_scan_button()

    def _initial_status_text(self) -> str:
        bits = [
            f"Worker: {os.getenv('INVENTORY_WORKER_URL','(missing)')}",
            "D1: enabled" if self.d1_enabled else "D1: disabled (optional)",
            f"Scan mode: {'ON' if self.scan_mode else 'OFF'}",
        ]
        return " | ".join(bits)

    def set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _update_scan_button(self) -> None:
        btn = self.query_one("#toggle_scan", Button)
        btn.label = "Scan: ON" if self.scan_mode else "Scan: OFF"

    # -------------------------
    # Helpers
    # -------------------------
    def _ensure_reason_option(self, code: str) -> None:
        """If D1 doesn't include a code (or reasons not loaded yet), add it as an option."""
        sel = self.query_one("#reason_code", Select)
        existing = {value for _, value in sel._options}  # internal but stable enough
        if code not in existing:
            # add "CODE" as a label too
            sel.set_options(list(sel._options) + [(code, code)])

    def apply_template(self, *, reason_code: str, reference_type: str, ref_prefix: str) -> None:
        self._ensure_reason_option(reason_code)

        self.query_one("#reason_code", Select).value = reason_code
        self.query_one("#reference_type", Input).value = reference_type
        self.query_one("#reference_id", Input).value = new_ref_id(ref_prefix)

        # Keep notes/reason_text as-is (staff might have started typing)
        self.set_status(f"Template applied: {reference_type} / {reason_code}")

        # In scan workflows, keep focus on product search unless a product is already selected
        if self.selected_product_id is None:
            self.query_one("#product_search", Input).focus()
        else:
            self.query_one("#qty_delta", Input).focus()

    def select_product(self, prod: Dict[str, Any]) -> None:
        pid = int(prod["product_id"])
        self.selected_product_id = pid
        self.query_one("#product_id", Input).value = str(pid)

        # Update stock line immediately from search row + fetch full stock row
        line = self.query_one("#stock_line", Static)
        line.update(
            f"product {pid}: on_hand={prod.get('qty_on_hand', 0)} reserved={prod.get('qty_reserved', 0)} (from search)"
        )
        if self.d1:
            self.fetch_stock(pid)

        # Scan flow: clear search and go straight to qty_delta
        if self.scan_mode:
            self.query_one("#product_search", Input).value = ""
        self.query_one("#qty_delta", Input).focus()

    # -------------------------
    # Background workers
    # -------------------------
    @work(exclusive=True, thread=True)
    def load_reasons(self) -> None:
        if not self.d1:
            return
        try:
            reasons = d1_fetch_reasons(self.d1)
            self.call_from_thread(self._apply_reasons, reasons)
        except Exception as e:
            self.call_from_thread(self.set_status, f"Failed to load reason codes from D1: {e}")

    def _apply_reasons(self, reasons: List[Tuple[str, str]]) -> None:
        self._reasons = reasons
        select = self.query_one("#reason_code", Select)

        if not reasons:
            select.set_options([("RECEIPT", "RECEIPT")])
            self.set_status("No active reason codes returned from D1; using default list.")
            return

        options = []
        for code, desc in reasons:
            label = f"{code} — {desc}" if desc else code
            options.append((label, code))
        select.set_options(options)

        # Keep current if still valid, else choose first
        current = select.value
        valid = {c for _, c in reasons}
        if current not in valid:
            select.value = reasons[0][0]

    @work(exclusive=True, thread=True)
    def search_products(self, q: str) -> None:
        if not self.d1:
            return
        try:
            rows = d1_find_products(self.d1, q, limit=100)
            self.call_from_thread(self._apply_products, rows)
        except Exception as e:
            self.call_from_thread(self.set_status, f"Product search failed: {e}")
            self.call_from_thread(self._apply_products, [])

    def _apply_products(self, rows: List[Dict[str, Any]]) -> None:
        self._products = rows
        table = self.query_one("#product_table", DataTable)
        table.clear()

        for r in rows:
            table.add_row(
                str(r.get("product_id", "")),
                str(r.get("upc") or ""),
                str(r.get("model") or ""),
                str(r.get("brand") or ""),
                str(r.get("qty_on_hand", 0)),
                str(r.get("qty_reserved", 0)),
            )

        note = self.query_one("#product_note", Static)
        if not rows:
            note.update("No matches." if self.query_one("#product_search", Input).value.strip() else "")
        else:
            note.update(f"{len(rows)} match(es).")

        self.query_one("#product_loading", LoadingIndicator).display = False

        # If this was a scan-enter search, auto-select if exactly 1 match
        if self._scan_submit_pending:
            self._scan_submit_pending = False
            if len(rows) == 1:
                self.select_product(rows[0])
            elif len(rows) > 1:
                table.focus()
                self.set_status("Multiple matches — select the correct row.")
            else:
                self.set_status("No matches for scanned value.")

    @work(exclusive=True, thread=True)
    def fetch_stock(self, product_id: int) -> None:
        if not self.d1:
            return
        try:
            sb = d1_get_stock(self.d1, product_id)
            self.call_from_thread(self._apply_stock, product_id, sb)
        except Exception as e:
            self.call_from_thread(self.set_status, f"Stock lookup failed: {e}")

    def _apply_stock(self, product_id: int, sb: Optional[Dict[str, Any]]) -> None:
        line = self.query_one("#stock_line", Static)
        if not sb:
            line.update(f"product {product_id}: (no stock_balance row)")
            return
        line.update(
            f"product {product_id}: on_hand={sb.get('qty_on_hand')} reserved={sb.get('qty_reserved')} "
            f"(updated {sb.get('updated_at')})"
        )

    @work(exclusive=True, thread=True)
    def submit_move(self, payload: Dict[str, Any]) -> None:
        try:
            resp = self.worker.move(payload)
            self.call_from_thread(self._on_submit_success, payload, resp)
        except Exception as e:
            self.call_from_thread(self._on_submit_error, str(e))

    def _on_submit_success(self, payload: Dict[str, Any], resp: Dict[str, Any]) -> None:
        applied = bool(resp.get("applied", True))
        receipt_lines = [
            "✅ Success",
            f"applied: {applied}",
            f"product_id: {resp.get('product_id')}",
            f"qty_delta: {resp.get('qty_delta')}",
            f"reason_code: {resp.get('reason_code')}",
            f"reference: {resp.get('reference_type')} / {resp.get('reference_id')}",
            f"new_qty_on_hand: {resp.get('new_qty_on_hand')}",
        ]
        if "affected_accounts" in resp:
            receipt_lines.append(
                f"affected_accounts: {resp.get('affected_accounts')}  enqueued: {resp.get('enqueued')}"
            )
        if resp.get("ledger"):
            led = resp["ledger"]
            receipt_lines.append(f"ledger_id: {led.get('stock_ledger_id')} at {led.get('occurred_at')}")

        self.set_status("\n".join(receipt_lines))
        log_action({"request": payload, "response": resp})

        # Refresh stock line if D1 enabled
        try:
            pid = int(resp.get("product_id"))
            if self.d1:
                self.fetch_stock(pid)
        except Exception:
            pass

        # Scan workflow: ready for next scan immediately
        if self.scan_mode:
            self.query_one("#qty_delta", Input).value = ""
            self.query_one("#product_search", Input).value = ""
            self.selected_product_id = None
            self.query_one("#product_id", Input).value = ""
            self.query_one("#product_search", Input).focus()

    def _on_submit_error(self, msg: str) -> None:
        self.set_status(f"❌ Submit failed: {msg}")

    # -------------------------
    # Events
    # -------------------------
    @on(Button.Pressed, "#toggle_scan")
    def _toggle_scan(self) -> None:
        self.scan_mode = not self.scan_mode
        self._update_scan_button()
        self.set_status(f"Scan mode is now {'ON' if self.scan_mode else 'OFF'}.")

    @on(Input.Changed, "#product_search")
    def _on_product_search_changed(self, event: Input.Changed) -> None:
        if not self.d1_enabled:
            return
        q = event.value.strip()

        # In scan mode, we still support "type-ahead" searching as they type,
        # but we only auto-select on Enter (Input.Submitted).
        if len(q) < 2 and not q.isdigit():
            self._apply_products([])
            return

        self.query_one("#product_loading", LoadingIndicator).display = True
        self.search_products(q)

    @on(Input.Submitted, "#product_search")
    def _on_product_search_submitted(self, event: Input.Submitted) -> None:
        """Scan mode: scanner sends Enter. We'll auto-select if exactly one match."""
        if not self.d1_enabled:
            self.set_status("D1 disabled; scan search is unavailable.")
            return
        q = event.value.strip()
        if not q:
            return

        self._scan_submit_pending = True
        self.query_one("#product_loading", LoadingIndicator).display = True
        self.search_products(q)

    @on(events.Key)
    def _on_product_table_enter(self, event: events.Key) -> None:
        """When the product table has focus, Enter selects the highlighted row."""
        if event.key != "enter":
            return

        table = self.query_one("#product_table", DataTable)
        if self.focused is not table:
            return

        idx = table.cursor_row
        if idx < 0 or idx >= len(self._products):
            return

        self.select_product(self._products[idx])
        event.stop()

    # Action templates
    @on(Button.Pressed, "#tpl_receive")
    def _tpl_receive(self) -> None:
        self.apply_template(reason_code="RECEIVE", reference_type="PO", ref_prefix="po")

    @on(Button.Pressed, "#tpl_return")
    def _tpl_return(self) -> None:
        self.apply_template(reason_code="RETURN", reference_type="RMA", ref_prefix="rma")

    @on(Button.Pressed, "#tpl_damage")
    def _tpl_damage(self) -> None:
        self.apply_template(reason_code="DAMAGE", reference_type="MANUAL", ref_prefix="damage")

    @on(Button.Pressed, "#tpl_adjust")
    def _tpl_adjust(self) -> None:
        self.apply_template(reason_code="ADJUST", reference_type="MANUAL", ref_prefix="adjust")

    @on(Button.Pressed, "#refresh_reasons")
    def _on_refresh_reasons(self) -> None:
        if not self.d1:
            self.set_status("D1 is disabled; cannot refresh reasons.")
            return
        self.set_status("Refreshing reason codes…")
        self.load_reasons()

    @on(Button.Pressed, "#clear")
    def _on_clear(self) -> None:
        self.query_one("#product_search", Input).value = ""
        self._apply_products([])
        self.selected_product_id = None

        self.query_one("#product_id", Input).value = ""
        self.query_one("#qty_delta", Input).value = ""
        self.query_one("#reference_type", Input).value = "MANUAL"
        self.query_one("#reference_id", Input).value = new_ref_id("manual")
        self.query_one("#notes", TextArea).text = ""
        self.query_one("#reason_text", Input).value = ""
        self.query_one("#entered_by", Input).value = guess_entered_by()
        self.query_one("#stock_line", Static).update("—")
        self.set_status("Cleared.")
        self.query_one("#product_search", Input).focus()

    def _validate_int(self, inp: Input, field: str, allow_zero: bool = False) -> Optional[int]:
        s = inp.value.strip()
        if not s:
            inp.add_class("-invalid")
            self.set_status(f"❌ {field} is required.")
            return None
        try:
            v = int(s)
        except ValueError:
            inp.add_class("-invalid")
            self.set_status(f"❌ {field} must be a whole number.")
            return None
        if not allow_zero and v == 0:
            inp.add_class("-invalid")
            self.set_status(f"❌ {field} must be non-zero.")
            return None
        inp.remove_class("-invalid")
        return v

    @on(Input.Submitted, "#qty_delta")
    def _qty_submitted(self) -> None:
        """Enter on qty_delta submits (great for scan workflows)."""
        self._on_submit()

    @on(Button.Pressed, "#submit")
    def _submit_pressed(self) -> None:
        self._on_submit()

    def _on_submit(self) -> None:
        pid_inp = self.query_one("#product_id", Input)
        qty_inp = self.query_one("#qty_delta", Input)

        pid = self._validate_int(pid_inp, "product_id", allow_zero=False)
        if pid is None:
            return
        qty_delta = self._validate_int(qty_inp, "qty_delta", allow_zero=False)
        if qty_delta is None:
            return

        reason_sel = self.query_one("#reason_code", Select)
        reason_code = (reason_sel.value or "").strip()
        if not reason_code:
            self.set_status("❌ reason_code is required.")
            return

        ref_type = self.query_one("#reference_type", Input).value.strip() or "MANUAL"
        ref_id = self.query_one("#reference_id", Input).value.strip() or new_ref_id(ref_type)
        notes = self.query_one("#notes", TextArea).text.strip() or None
        reason_text = self.query_one("#reason_text", Input).value.strip() or None
        entered_by = self.query_one("#entered_by", Input).value.strip() or guess_entered_by()

        payload: Dict[str, Any] = {
            "product_id": int(pid),
            "qty_delta": int(qty_delta),
            "reason_code": reason_code,
            "reference_type": ref_type,
            "reference_id": ref_id,
            "entered_by": entered_by,
        }
        if notes is not None:
            payload["notes"] = notes
        if reason_text is not None:
            payload["reason"] = reason_text

        self.set_status("Submitting…")
        self.submit_move(payload)


if __name__ == "__main__":
    # Best-effort terminal resize (xterm-compatible terminals).
    # rows;cols — tweak to what you want.
    try:
        rows, cols = 45, 140
        print(f"\x1b[8;{rows};{cols}t", end="", flush=True)
        # Some terminals need a moment to apply; harmless if ignored.
        time.sleep(0.05)
    except Exception:
        pass

    InventoryTUI().run()