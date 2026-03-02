#!/usr/bin/env python3
from __future__ import annotations

"""
Cloudflare D1 interactive TUI (Windows + macOS + Linux) using Textual.

Install:
  pip install textual requests python-dotenv

Env vars:
  CLOUDFLARE_API_TOKEN
  CLOUDFLARE_ACCOUNT_ID
  CLOUDFLARE_D1_DATABASE_ID

Keys:
  q                 quit
  r                 refresh tables
  n / p             next / previous page (table viewer)
  F5        run SQL from the SQL box
  tab / shift+tab   focus cycle

SQL console supports dot-commands:
  .help
  .tables
  .schema [table]
  .mode table|json
  .params
  .params set [JSON_LIST]
  .params clear
  .limit N
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, ListView, ListItem, Label, DataTable, TextArea, Static, Button, Input
from textual.coordinate import Coordinate
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.containers import Vertical, Horizontal
from textual.worker import get_current_worker

from rich.text import Text




# -----------------------------
# D1 REST client
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


class D1Inspector:
    def __init__(self, client: D1Client) -> None:
        self.client = client

    def list_tables(self) -> List[str]:
        sql = """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """
        res = self.client.query(sql)
        rows = res[0].get("results") or []
        return [r["name"] for r in rows if isinstance(r, dict) and "name" in r]

    def _safe_ident(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def table_info(self, table: str) -> List[Dict[str, Any]]:
        sql = f"PRAGMA table_info({self._safe_ident(table)})"
        res = self.client.query(sql)
        return res[0].get("results") or []

    # NORMAL fetch (no rowid)
    def fetch_rows(self, table: str, limit: int, offset: int) -> Tuple[List[str], List[Dict[str, Any]]]:
        info = self.table_info(table)
        cols = [r.get("name", "") for r in info if isinstance(r, dict) and r.get("name")]

        sql = f"SELECT * FROM {self._safe_ident(table)} LIMIT ? OFFSET ?"
        res = self.client.query(sql, params=[int(limit), int(offset)])
        rows = res[0].get("results") or []
        if cols and rows and isinstance(rows, list) and isinstance(rows[0], dict):
            rows = [{c: row.get(c) for c in cols} for row in rows]  # stabilize order
        return cols, rows if isinstance(rows, list) else []

    # Fetch with identity (rowid fallback if no PK)
    def fetch_rows_with_identity(
        self, table: str, limit: int, offset: int
    ) -> Tuple[List[str], List[Dict[str, Any]], bool]:
        info = self.table_info(table)
        pk_cols = [r.get("name") for r in info if isinstance(r, dict) and int(r.get("pk", 0)) > 0]
        pk_cols = [c for c in pk_cols if c]

        if not pk_cols:
            sql = f"SELECT rowid AS _rowid_, * FROM {self._safe_ident(table)} LIMIT ? OFFSET ?"
            res = self.client.query(sql, params=[int(limit), int(offset)])
            rows = res[0].get("results") or []
            cols = ["_rowid_"] + [r.get("name", "") for r in info if isinstance(r, dict) and r.get("name")]
            cols = [c for c in cols if c]
            if rows and isinstance(rows, list) and isinstance(rows[0], dict):
                rows = [{c: row.get(c) for c in cols} for row in rows]
            return cols, rows if isinstance(rows, list) else [], True

        cols, rows = self.fetch_rows(table, limit, offset)
        return cols, rows, False


    def schema_sql(self, table: Optional[str] = None) -> str:
        if table:
            sql = """
                SELECT type, name, sql
                FROM sqlite_master
                WHERE name = ?
                ORDER BY type, name
            """
            res = self.client.query(sql, params=[table])
        else:
            sql = """
                SELECT type, name, sql
                FROM sqlite_master
                WHERE type IN ('table','index','trigger','view')
                ORDER BY type, name
            """
            res = self.client.query(sql)

        rows = res[0].get("results") or []
        parts: List[str] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            t, n, s = r.get("type"), r.get("name"), r.get("sql")
            if s:
                parts.append(f"-- {t}: {n}\n{s.strip()};")
        return "\n\n".join(parts) if parts else "-- (no schema found)"

class CellEditScreen(ModalScreen[Optional[str]]):
    def __init__(self, title: str, initial: str) -> None:
        super().__init__()
        self._title = title
        self._initial = initial

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._title),
            Input(value=self._initial, id="val"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save"),
            ),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            val = self.query_one("#val", Input).value
            self.dismiss(val)

class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self):
        yield Vertical(
            Label(self._title),
            Label(self._message),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Confirm", id="confirm"),
            ),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

class AddRowScreen(ModalScreen[Optional[dict]]):
    """
    Returns:
      - dict of {column: raw_text_value} on Save
      - None on Cancel
    """
    def __init__(self, table: str, columns: list[dict]) -> None:
        super().__init__()
        self._table = table
        self._columns = columns  # output of PRAGMA table_info

    def compose(self):
        items = [Label(f"Add row: {self._table}")]
        # Create an Input per column (skip generated identity columns if you want)
        for col in self._columns:
            name = col.get("name", "")
            if not name:
                continue
            # Use ids like col__<name>
            items.append(Label(name))
            items.append(Input(placeholder="(blank = DEFAULT/NULL)", id=f"col__{name}"))

        items.append(
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save"),
            )
        )

        yield Vertical(*items)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return

        out: dict[str, str] = {}
        for col in self._columns:
            name = col.get("name", "")
            if not name:
                continue
            inp = self.query_one(f"#col__{name}", Input)
            out[name] = inp.value
        self.dismiss(out)

class RowEditScreen(ModalScreen[Optional[dict]]):
    """
    Edit a full row.
    Returns:
      dict {col_name: new_text_value} on Save
      None on Cancel
    Behavior:
      - Inputs prefilled with current values (NULL shown as blank)
      - User may type NULL (case-insensitive) to set SQL NULL
    """

    def __init__(self, title: str, cols: list[str], row: dict, skip_cols: set[str]) -> None:
        super().__init__()
        self._title = title
        self._cols = cols
        self._row = row
        self._skip = skip_cols

    def compose(self):
        widgets = [Label(self._title)]

        for c in self._cols:
            if c in self._skip:
                continue
            cur = self._row.get(c)
            cur_s = "" if cur is None else str(cur)
            widgets.append(Label(c))
            widgets.append(Input(value=cur_s, id=f"col__{c}"))

        widgets.append(
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save"),
            )
        )
        yield Vertical(*widgets)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return

        out: dict[str, str] = {}
        for c in self._cols:
            if c in self._skip:
                continue
            out[c] = self.query_one(f"#col__{c}", Input).value
        self.dismiss(out)


# -----------------------------
# Helpers for console output
# -----------------------------
def render_table_text(rows: List[Dict[str, Any]], max_rows: int = 200) -> str:
    if not rows:
        return "(no rows)"
    cols: List[str] = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)

    def s(v: Any) -> str:
        if v is None:
            return "NULL"
        out = str(v)
        return out if len(out) <= 80 else out[:79] + "…"

    widths = [len(c) for c in cols]
    sample = rows[:max_rows]
    str_rows: List[List[str]] = []
    for r in sample:
        line = []
        for i, c in enumerate(cols):
            val = s(r.get(c))
            widths[i] = max(widths[i], len(val))
            line.append(val)
        str_rows.append(line)

    header = " | ".join(cols[i].ljust(widths[i]) for i in range(len(cols)))
    sep = "-+-".join("-" * widths[i] for i in range(len(cols)))
    out_lines = [header, sep]
    for line in str_rows:
        out_lines.append(" | ".join(line[i].ljust(widths[i]) for i in range(len(cols))))
    if len(rows) > max_rows:
        out_lines.append(f"... ({len(rows) - max_rows} more rows)")
    return "\n".join(out_lines)


# -----------------------------
# Textual App
# -----------------------------

_INSERT_RE = re.compile(
    r"""^\s*INSERT\s+INTO\s+"?([A-Za-z_][A-Za-z0-9_]*)"?\s*
         \(\s*([^)]+?)\s*\)\s*
         VALUES\s*\(\s*([?]\s*(?:,\s*[?]\s*)*)\)\s*;?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

def _parse_simple_insert(sql: str) -> Tuple[str, Tuple[str, ...], int] | None:
    """
    Accept only very simple, parameterized inserts of the form:
      INSERT INTO "table" ("a","b") VALUES (?, ?);
    Returns: (table, cols_tuple, n_params)
    """
    m = _INSERT_RE.match(sql)
    if not m:
        return None
    table = m.group(1)
    cols_raw = m.group(2)
    qmarks = m.group(3)

    cols = []
    for c in cols_raw.split(","):
        c = c.strip()
        if c.startswith('"') and c.endswith('"'):
            c = c[1:-1]
        cols.append(c)

    n_params = qmarks.count("?")
    if n_params != len(cols):
        return None
    return table, tuple(cols), n_params

class D1TextualApp(App):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_tables", "Refresh tables"),
        Binding("n", "next_page", "Next page"),
        Binding("p", "prev_page", "Prev page"),
        Binding("f5", "run_sql", "Run SQL"),
        Binding("e", "edit_cell", "Edit cell"),
        Binding("E", "edit_row", "Edit row"),
        Binding("c", "commit_edits", "Commit edits"),
        Binding("x", "discard_edits", "Discard edits"),
        Binding("v", "view_edits", "View queued"),
        Binding("a", "add_row", "Add row"),
        Binding("d", "delete_row", "Delete row"),
    ]

    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #sidebar { width: 32; }
    #status { height: 3; }
    #sql_box { height: 7; }
    #out_box { height: 12; }
    DataTable { height: 1fr; }
    """

    def __init__(self, client: D1Client, insp: D1Inspector, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.client = client
        self.insp = insp

        self.tables: List[str] = []
        self.hidden_tables =  {
            "_cf_KV",
            "demo",
            "ebay_oauth",
            "locations",
            "recalc_queue",
            "stage_ebay_active_listings",
            "stock_ledger_reason_codes",
        }
        self.current_table: Optional[str] = None
        self.limit = 100
        self.offset = 0

        # Console state
        self.mode = "table"   # table|json
        self.params: Optional[List[Any]] = None
        self.console_limit_default = 200

        self.current_cols: List[str] = []
        self.current_rows: List[Dict[str, Any]] = []   # each row dict matches current_cols
        self.pk_cols: List[str] = []                   # primary key columns for current_table
        self.rowid_enabled: bool = False               # whether we loaded _rowid_
        self._soft_row: int | None = None


        self.pending_edits: List[tuple[str, list[Any]]] = []  # [(sql, params), ...]
        self.autocommit = False  # if True, keep old behavior


    def _get_pk_cols(self, table: str) -> List[str]:
        info = self.insp.table_info(table)
        # In PRAGMA table_info: pk is 0 (not PK) or >0 (PK order)
        pk = [(r.get("pk", 0), r.get("name")) for r in info if isinstance(r, dict)]
        pk = [(int(o), n) for o, n in pk if n and int(o) > 0]
        pk.sort(key=lambda x: x[0])
        return [n for _, n in pk]

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Label("Tables")
                yield ListView(id="tables_list")

            with Vertical():
                yield Label("Rows (selected table)")
                yield DataTable(id="rows_table")

        yield Static("", id="status")

        yield Label("SQL (F5 to run). Dot-commands: .help, .tables, .schema, .mode, .params, .limit")
        yield TextArea(id="sql_box")

        yield Label("Output")
        out = TextArea(id="out_box")
        out.read_only = True
        yield out

        yield Footer()

    def on_mount(self) -> None:
        self.refresh_tables()

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _set_output(self, msg: str) -> None:
        out = self.query_one("#out_box", TextArea)
        out.read_only = False
        out.text = msg
        out.read_only = True

    def refresh_tables(self) -> None:
        try:
            all_tables = self.insp.list_tables()
            self.tables = [t for t in all_tables if t not in self.hidden_tables]

            lv = self.query_one("#tables_list", ListView)
            lv.clear()
            for t in self.tables:
                item = ListItem(Label(t))
                item.data = t  # <-- store table name here (Textual supports arbitrary attrs)
                lv.append(item)
            self._set_status(f"Loaded {len(self.tables)} tables.")
            if self.tables and self.current_table is None:
                self.current_table = self.tables[0]
                self.offset = 0
                self.load_table_rows()
        except Exception as e:
            self._set_status(f"ERROR loading tables: {e}")

    def _safe_ident(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def _row_identity_where(self, row: Dict[str, Any]) -> Tuple[str, List[Any]]:
        # Prefer PK columns
        if self.pk_cols:
            where = " AND ".join(f"{self._safe_ident(c)}=?" for c in self.pk_cols)
            params = [row.get(c) for c in self.pk_cols]
            return where, params

        # Fallback: rowid
        if self.rowid_enabled and "_rowid_" in row:
            return f"{self._safe_ident('_rowid_')}=?", [row.get("_rowid_")]

        raise CloudflareD1Error("Cannot edit: table has no PK and rowid not available (WITHOUT ROWID?).")

    async def _delete_row_flow(self) -> None:
        if not self.current_table or not self.current_rows:
            return

        dt = self.query_one("#rows_table", DataTable)
        row_idx = getattr(dt, "cursor_row", 0)
        row_idx = max(0, min(int(row_idx), len(self.current_rows) - 1))
        row = self.current_rows[row_idx]

        # Build WHERE using PK or _rowid_
        try:
            where_sql, where_params = self._row_identity_where(row)
        except Exception as e:
            self._set_output(f"Cannot delete row: {e}")
            return

        ok = await self.push_screen_wait(
            ConfirmScreen(
                "Delete row",
                f"Delete row {row_idx + 1} from {self.current_table}?\n"
                f"(This will be queued until you commit.)",
            )
        )
        if not ok:
            return

        sql = f"DELETE FROM {self._safe_ident(self.current_table)} WHERE {where_sql};"
        params = list(where_params)

        self.pending_edits.append((sql, params))

        # Optimistic UI: remove from local list + refresh view
        try:
            self.current_rows.pop(row_idx)
        except Exception:
            pass
        self.load_table_rows()

        self._set_output(
            "Queued delete (not committed).\n"
            f"{sql}\nparams={json.dumps(params, default=str)}\n\n"
            f"Pending edits: {len(self.pending_edits)}"
        )

    def _coerce_input(self, raw: str) -> Any:
        """
        Basic coercion:
        - blank -> special marker meaning "omit column" (let DEFAULT apply if possible)
        - 'NULL' -> None
        - otherwise -> string (SQLite can coerce)
        """
        s = (raw or "").strip()
        if s == "":
            return "__OMIT__"
        if s.upper() == "NULL":
            return None
        return s

    async def _add_row_flow(self) -> None:
        if not self.current_table:
            return

        # Use PRAGMA table_info to know columns
        try:
            cols_info = self.insp.table_info(self.current_table)
            cols_info = [c for c in cols_info if isinstance(c, dict) and c.get("name")]
        except Exception as e:
            self._set_output(f"ERROR reading table info: {e}")
            return

        # Optional: skip obvious auto identity columns if you want (INTEGER PRIMARY KEY often auto-generates)
        # We'll still show them; user can leave blank to omit and let DB fill.
        payload = await self.push_screen_wait(AddRowScreen(self.current_table, cols_info))
        if payload is None:
            return

        # Build INSERT with only provided columns (omitting blanks)
        insert_cols: list[str] = []
        params: list[Any] = []

        for col in cols_info:
            name = col["name"]
            raw = payload.get(name, "")
            value = self._coerce_input(raw)

            if value == "__OMIT__":
                # omit column to allow DEFAULT (or NULL if no default and nullable)
                continue

            insert_cols.append(name)
            params.append(value)

        if not insert_cols:
            # If user omitted everything, insert default row
            sql = f"INSERT INTO {self._safe_ident(self.current_table)} DEFAULT VALUES;"
            self.pending_edits.append((sql, []))
            self.load_table_rows()
            self._set_output(
                "Queued insert (DEFAULT VALUES) (not committed).\n"
                f"{sql}\n\nPending edits: {len(self.pending_edits)}"
            )
            return

        cols_sql = ", ".join(self._safe_ident(c) for c in insert_cols)
        qmarks = ", ".join(["?"] * len(insert_cols))
        sql = f"INSERT INTO {self._safe_ident(self.current_table)} ({cols_sql}) VALUES ({qmarks});"

        self.pending_edits.append((sql, params))

        # Refresh table view (you won't see it in DB until commit, unless you do optimistic local append)
        self.load_table_rows()

        self._set_output(
            "Queued insert (not committed).\n"
            f"{sql}\nparams={json.dumps(params, default=str)}\n\n"
            f"Pending edits: {len(self.pending_edits)}"
        )

    def action_edit_cell(self) -> None:
        # Run the async edit flow in a worker so push_screen_wait is allowed.
        self.run_worker(self._edit_cell_flow(), exclusive=True)

    async def _edit_cell_flow(self) -> None:
        """Edit a single cell and QUEUE the update (no write to D1 until you commit)."""
        if not self.current_table or not self.current_rows or not self.current_cols:
            return

        dt = self.query_one("#rows_table", DataTable)

        # Cursor APIs vary across Textual versions; these are the common attributes.
        row_idx = getattr(dt, "cursor_row", 0)
        col_idx = getattr(dt, "cursor_column", 0)

        row_idx = max(0, min(int(row_idx), len(self.current_rows) - 1))
        col_idx = max(0, min(int(col_idx), len(self.current_cols) - 1))

        col = self.current_cols[col_idx]

        # Avoid editing identity columns; it complicates targeting.
        if col in getattr(self, "pk_cols", []) or col == "_rowid_":
            self._set_output("Refusing to edit identity column directly. Edit non-PK columns.")
            return

        row = self.current_rows[row_idx]
        old = row.get(col)
        initial = "" if old is None else str(old)

        new_val = await self.push_screen_wait(
            CellEditScreen(f"Edit {self.current_table}.{col}", initial)
        )
        if new_val is None:
            return

        # Decide how to interpret the user's input.
        # - If user types NULL (case-insensitive), treat as SQL NULL
        # - Otherwise keep as string; SQLite will coerce types as needed
        if new_val.strip().upper() == "NULL":
            value = None
        else:
            value = new_val

        # Build parameterized UPDATE
        where_sql, where_params = self._row_identity_where(row)
        sql = (
            f"UPDATE {self._safe_ident(self.current_table)} "
            f"SET {self._safe_ident(col)}=? "
            f"WHERE {where_sql};"
        )
        params = [value] + list(where_params)

        # Queue instead of executing
        if not hasattr(self, "pending_edits"):
            self.pending_edits = []  # type: ignore[attr-defined]
        self.pending_edits.append((sql, params))  # type: ignore[attr-defined]

        # Optimistically update the in-memory row and the visible DataTable cell
        row[col] = value

        # Update the DataTable cell if possible; otherwise refresh the table view.
        try:
            # DataTable uses row/column indices; update_cell exists in many Textual versions.
            if hasattr(dt, "update_cell"):
                dt.update_cell(row_idx, col_idx, self._cell(value))
            else:
                # fallback: full refresh
                self.load_table_rows()
        except Exception:
            self.load_table_rows()

        self._set_output(
            "Queued edit (not committed).\n"
            f"{sql}\n"
            f"params={json.dumps(params, default=str)}\n\n"
            f"Pending edits: {len(self.pending_edits)}"
        )

    def action_edit_row(self) -> None:
        self.run_worker(self._edit_row_flow(), exclusive=True)

    async def _edit_row_flow(self) -> None:
        """Edit all editable columns for the current row; queue ONE UPDATE statement."""
        if not self.current_table or not self.current_rows or not self.current_cols:
            self._set_output("No table/rows loaded.")
            return

        dt = self.query_one("#rows_table", DataTable)
        row_idx = getattr(dt, "cursor_row", 0)
        row_idx = max(0, min(int(row_idx), len(self.current_rows) - 1))
        row = self.current_rows[row_idx]

        # Determine identity columns we should not edit
        skip_cols = set(getattr(self, "pk_cols", []))
        if getattr(self, "rowid_enabled", False):
            skip_cols.add("_rowid_")

        # Must be able to uniquely identify the row to update
        try:
            where_sql, where_params = self._row_identity_where(row)
        except Exception as e:
            self._set_output(f"Cannot edit row (no reliable identity): {e}")
            return

        edited = await self.push_screen_wait(
            RowEditScreen(
                title=f"Edit row {row_idx + 1} in {self.current_table}",
                cols=list(self.current_cols),
                row=row,
                skip_cols=skip_cols,
            )
        )
        if edited is None:
            return

        # Build SET clause for changed columns only
        set_cols: list[str] = []
        set_vals: list[Any] = []

        def normalize_input(s: str) -> Any:
            t = (s or "").strip()
            if t.upper() == "NULL":
                return None
            return t  # keep string; SQLite will coerce types as needed

        for c, raw in edited.items():
            new_val = normalize_input(raw)
            old_val = row.get(c)
            # Compare using string normalization for non-None, else None
            # This avoids spurious updates when the UI re-serializes values.
            if old_val is None and new_val is None:
                continue
            if old_val is not None and new_val is not None and str(old_val) == str(new_val):
                continue

            set_cols.append(c)
            set_vals.append(new_val)

        if not set_cols:
            self._set_output("No changes detected; nothing queued.")
            return

        set_sql = ", ".join(f"{self._safe_ident(c)}=?" for c in set_cols)
        sql = (
            f"UPDATE {self._safe_ident(self.current_table)} "
            f"SET {set_sql} "
            f"WHERE {where_sql};"
        )
        params = list(set_vals) + list(where_params)

        # Queue (do not execute)
        self.pending_edits.append((sql, params))

        # Optimistically update in-memory row and refresh UI
        for c, v in zip(set_cols, set_vals):
            row[c] = v

        # Light UI update if possible; else reload page
        try:
            if hasattr(dt, "update_cell"):
                # update visible cells for changed columns
                col_index = {name: i for i, name in enumerate(self.current_cols)}
                for c, v in zip(set_cols, set_vals):
                    if c in col_index:
                        dt.update_cell(row_idx, col_index[c], self._cell(v))
            else:
                self.load_table_rows()
        except Exception:
            self.load_table_rows()

        self._set_output(
            "Queued row update (not committed).\n"
            f"{sql}\n"
            f"params={json.dumps(params, default=str)}\n\n"
            f"Pending edits: {len(self.pending_edits)}"
        )

    def action_commit_edits(self) -> None:
        """
        Commit pending edits using ONLY single-statement calls (so params are allowed).
        Best-effort optimization:
        - batches identical-shape INSERTs into one multi-values INSERT
        - executes everything else one-by-one
        """
        if not self.pending_edits:
            self._set_output("No pending edits.")
            return

        # Group simple inserts by (table, cols, n_params)
        insert_groups: Dict[Tuple[str, Tuple[str, ...], int], List[Tuple[str, List[Any]]]] = {}
        passthrough: List[Tuple[str, List[Any]]] = []

        for (sql, params) in self.pending_edits:
            parsed = _parse_simple_insert(sql)
            if parsed is None:
                passthrough.append((sql, params))
                continue
            key = parsed
            insert_groups.setdefault(key, []).append((sql, params))

        executed_results = []
        errors = []

        # 1) Execute batched inserts
        for (table, cols, n_params), items in insert_groups.items():
            if len(items) == 1:
                # No benefit batching a single row
                sql, params = items[0]
                try:
                    res = self.client.query(sql, params=params)
                    executed_results.append({"sql": sql, "params_count": len(params), "result": res})
                except Exception as e:
                    errors.append({"sql": sql, "params": params, "error": str(e)})
                continue

            # Build one INSERT with multiple VALUES groups
            cols_sql = ", ".join(self._safe_ident(c) for c in cols)
            row_tpl = "(" + ", ".join(["?"] * n_params) + ")"
            sql_batched = (
                f"INSERT INTO {self._safe_ident(table)} ({cols_sql}) VALUES "
                + ", ".join([row_tpl] * len(items))
                + ";"
            )
            all_params: List[Any] = []
            for _, p in items:
                all_params.extend(p)

            try:
                res = self.client.query(sql_batched, params=all_params)
                executed_results.append(
                    {"sql": sql_batched, "params_count": len(all_params), "batched_rows": len(items), "result": res}
                )
            except Exception as e:
                errors.append({"sql": sql_batched, "params_count": len(all_params), "error": str(e)})

        # 2) Execute everything else one-by-one (single statement each)
        for (sql, params) in passthrough:
            # Ensure it's single statement; we won't attempt to split it here.
            try:
                res = self.client.query(sql, params=params)
                executed_results.append({"sql": sql, "params_count": len(params), "result": res})
            except Exception as e:
                errors.append({"sql": sql, "params": params, "error": str(e)})

        # Clear only if no errors (safer)
        if errors:
            self._set_output(
                "COMMIT completed with errors.\n\n"
                f"Executed batches: {len(executed_results)}\n"
                f"Errors: {len(errors)}\n\n"
                "Errors:\n"
                + json.dumps(errors, indent=2, default=str)
            )
            return

        self.pending_edits.clear()

        # Refresh view
        if self.current_table:
            self.load_table_rows()

        self._set_output(
            "Committed edits.\n\n"
            f"HTTP calls: {len(executed_results)}\n"
            f"Original queued statements: {len(insert_groups) + len(passthrough)} (groups + passthrough)\n\n"
            + json.dumps(executed_results, indent=2, default=str)
        )

    def action_discard_edits(self) -> None:
        n = len(self.pending_edits)
        self.pending_edits.clear()
        self._set_output(f"Discarded {n} pending edits.")

    def action_view_edits(self) -> None:
        if not self.pending_edits:
            self._set_output("(no pending edits)")
            return
        lines = []
        for i, (sql, params) in enumerate(self.pending_edits, start=1):
            lines.append(f"-- {i} --\n{sql}\nparams={json.dumps(params, default=str)}\n")
        self._set_output("\n".join(lines))

    def action_add_row(self) -> None:
        self.run_worker(self._add_row_flow(), exclusive=True)

    def action_delete_row(self) -> None:
        self.run_worker(self._delete_row_flow(), exclusive=True)

    def action_refresh_tables(self) -> None:
        self.refresh_tables()

    def action_next_page(self) -> None:
        if self.current_table:
            self.offset += self.limit
            self.load_table_rows()

    def action_prev_page(self) -> None:
        if self.current_table:
            self.offset = max(0, self.offset - self.limit)
            self.load_table_rows()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        table = getattr(event.item, "data", None)
        if not table:
            # fallback: try label text if data wasn't set
            try:
                table = event.item.query_one(Label).text
            except Exception:
                return

        self.current_table = str(table)
        self.offset = 0
        self.load_table_rows()

    def load_table_rows(self) -> None:
        if not self.current_table:
            return
        try:
            # identity-aware fetch first
            self.pk_cols = self._get_pk_cols(self.current_table)
            cols, rows, rowid_enabled = self.insp.fetch_rows_with_identity(
                self.current_table, self.limit, self.offset
            )
            self.rowid_enabled = rowid_enabled
            self.current_cols = cols
            self.current_rows = rows

            dt = self.query_one("#rows_table", DataTable)
            dt.clear(columns=True)
            self._soft_row = None

            if not cols:
                cols = ["(no columns)"]
            for c in cols:
                dt.add_column(c)

            for r in rows:
                if isinstance(r, dict):
                    dt.add_row(*[self._cell(r.get(c)) for c in cols])
                else:
                    dt.add_row(self._cell(r))

            self._set_status(
                f"Table={self.current_table} | offset={self.offset} limit={self.limit} | rows={len(rows)}"
            )
        except Exception as e:
            self._set_status(f"ERROR loading rows: {e}")

    def _cell_renderable(self, v: Any, *, dim: bool) -> Any:
        """Return either plain text or a Rich Text renderable."""
        s = self._cell(v)
        return Text(s, style="on #3B3B8B") if dim else s

    def _apply_soft_row(self, dt: DataTable, row_idx: int, dim: bool, skip_col: int | None = None) -> None:
        """Dim/undim every cell in a row, optionally skipping one column (the focused cell)."""
        if not self.current_rows or not self.current_cols:
            return
        if row_idx < 0 or row_idx >= len(self.current_rows):
            return

        row = self.current_rows[row_idx]
        for col_idx, col_name in enumerate(self.current_cols):
            if skip_col is not None and col_idx == skip_col:
                continue
            val = row.get(col_name) if isinstance(row, dict) else None
            coord = Coordinate(row_idx, col_idx)

            if hasattr(dt, "coordinate_to_cell_key"):
                cell_key = dt.coordinate_to_cell_key(coord)
                dt.update_cell(cell_key.row_key, cell_key.column_key, self._cell_renderable(val, dim=dim))
            elif hasattr(dt, "update_cell_at"):
                # Some Textual versions provide update_cell_at(Coordinate, value)
                dt.update_cell_at(coord, self._cell_renderable(val, dim=dim))
            else:
                # Last resort: can't safely update cells on this version
                return

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        dt = event.data_table
        if dt.id != "rows_table":
            return

        new_row = int(event.coordinate.row)
        new_col = int(event.coordinate.column)

        # Clear old soft highlight
        if self._soft_row is not None and self._soft_row != new_row:
            self._apply_soft_row(dt, self._soft_row, dim=False)

        # Apply new soft highlight, but DON'T dim the focused cell
        self._apply_soft_row(dt, new_row, dim=True, skip_col=new_col)
        self._soft_row = new_row

    def on_key(self, event) -> None:
        # temporary: show what Textual *actually* receives
        self._set_status(f"key={event.key!r} character={event.character!r}")



    @staticmethod
    def _cell(v: Any) -> str:
        if v is None:
            return "NULL"
        s = str(v)
        return s if len(s) <= 200 else s[:199] + "…"

    # -------------------------
    # SQL console
    # -------------------------
    def action_run_sql(self) -> None:
        sql = self.query_one("#sql_box", TextArea).text.strip()
        if not sql:
            return

        if sql.startswith("."):
            out = self._run_dot(sql)
            if out == "__EXIT__":
                self.exit()
                return
            self._set_output(out)
            return

        # convenience: enforce semicolon
        if not sql.endswith(";"):
            sql = sql + ";"

        # optional default limit for simple SELECTs without LIMIT
        lowered = sql.lstrip().lower()
        if lowered.startswith("select") and " limit " not in lowered and self.console_limit_default:
            sql = f"SELECT * FROM ({sql.rstrip(';')}) LIMIT {int(self.console_limit_default)};"

        try:
            res = self.client.query(sql, params=self.params)
            self._set_output(self._format_results(res))
            # Refresh table view if user modified data
            if self.current_table:
                self.load_table_rows()
        except Exception as e:
            self._set_output(f"ERROR: {e}")

    def _run_dot(self, cmdline: str) -> str:
        parts = cmdline.strip().split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in (".quit", ".exit"):
            return "__EXIT__"

        if cmd == ".help":
            return (
                "Dot-commands:\n"
                "  .help\n"
                "  .tables\n"
                "  .schema [table]\n"
                "  .mode table|json\n"
                "  .params\n"
                "  .params set [JSON_LIST]\n"
                "  .params clear\n"
                "  .limit N\n"
                "  .quit / .exit\n\n"
                "Notes:\n"
                "- Use '?' placeholders with .params set [...]\n"
                "- Identifiers (table names) cannot be parameterized.\n"
                "- F5 runs the SQL box.\n"
            )

        if cmd == ".tables":
            try:
                return "\n".join(self.insp.list_tables()) or "(no tables)"
            except Exception as e:
                return f"ERROR: {e}"

        if cmd == ".schema":
            table = parts[1] if len(parts) >= 2 else None
            try:
                return self.insp.schema_sql(table)
            except Exception as e:
                return f"ERROR: {e}"

        if cmd == ".mode":
            if len(parts) < 2 or parts[1].lower() not in ("table", "json"):
                return "Usage: .mode table|json"
            self.mode = parts[1].lower()
            return f"mode = {self.mode}"

        if cmd == ".params":
            if len(parts) == 1:
                return json.dumps(self.params) if self.params is not None else "(no params)"
            sub = parts[1].lower()
            if sub == "clear":
                self.params = None
                return "(params cleared)"
            if sub == "set":
                if len(parts) < 3:
                    return 'Usage: .params set [JSON_LIST]  e.g. .params set [123,"x"]'
                try:
                    parsed = json.loads(parts[2])
                    if not isinstance(parsed, list):
                        return "Params must be a JSON list, e.g. [1,\"a\"]"
                    self.params = parsed
                    return f"params = {json.dumps(self.params)}"
                except json.JSONDecodeError as e:
                    return f"Invalid JSON: {e}"
            return "Usage: .params | .params set [JSON_LIST] | .params clear"

        if cmd == ".limit":
            if len(parts) < 2:
                return "Usage: .limit N"
            try:
                n = int(parts[1])
                if n <= 0:
                    return "N must be > 0"
                self.console_limit_default = n
                return f"limit = {self.console_limit_default}"
            except ValueError:
                return "N must be an integer"

        return f"Unknown command: {cmdline}"

    def _format_results(self, results: List[Dict[str, Any]]) -> str:
        if self.mode == "json":
            return json.dumps(results, indent=2, default=str)

        out_parts: List[str] = []
        multi = len(results) > 1
        for i, stmt in enumerate(results, start=1):
            if multi:
                out_parts.append(f"-- statement {i} --")

            rows = stmt.get("results")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                out_parts.append(render_table_text(rows))
            elif isinstance(rows, list):
                out_parts.append(str(rows))
            else:
                meta = stmt.get("meta") or {}
                out_parts.append(f"(ok) meta={json.dumps(meta, default=str)}")

        return "\n\n".join(out_parts).strip() or "(no output)"


def main() -> None:
    token = os.getenv("CLOUDFLARE_API_TOKEN", "")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    database_id = os.getenv("CLOUDFLARE_D1_DATABASE_ID", "")
    if not token or not account_id or not database_id:
        raise SystemExit(
            "Missing env vars. Required:\n"
            "  CLOUDFLARE_API_TOKEN\n"
            "  CLOUDFLARE_ACCOUNT_ID\n"
            "  CLOUDFLARE_D1_DATABASE_ID\n"
        )

    timeout_s = int(os.getenv("D1_TUI_TIMEOUT_S", "30"))
    cfg = D1Config(account_id=account_id, database_id=database_id, api_token=token)
    client = D1Client(cfg, timeout_s=timeout_s)
    insp = D1Inspector(client)

    app = D1TextualApp(client=client, insp=insp)
    app.run()


if __name__ == "__main__":
    main()
