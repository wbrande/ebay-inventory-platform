import sys
import json
import os
import time
import requests
import sqlite3
import pandas as pd
import re
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableView, QPushButton, QLabel, QMessageBox, QTextEdit,
    QHeaderView, QAbstractItemView, QProgressDialog,
    QTabWidget, QComboBox, QLineEdit, QCheckBox, QDialog,
    QDialogButtonBox, QFormLayout, QMenuBar, QMenu, QListWidget,
    QListWidgetItem, QGroupBox, QSplitter, QTreeWidget,
    QTreeWidgetItem, QInputDialog, QColorDialog, QTableWidget,
    QTableWidgetItem, QFileDialog, QSpinBox, QProgressBar, QPlainTextEdit
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QThread, Signal, QSettings, QTimer
from PySide6.QtGui import QFont, QAction, QColor


def clean_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Sanitize column names for SQLite compatibility."""
    new_columns = []
    for i, col in enumerate(df.columns):
        # Handle None or empty column names
        if col is None or str(col).strip() == "":
            new_name = f"col_{i}"
        else:
            # Replace any non-alphanumeric/underscore with underscore
            new_name = re.sub(r'\W+', '_', str(col))
            # Ensure name doesn't start with a digit
            if new_name and new_name[0].isdigit():
                new_name = f"col_{new_name}"
            # Handle case where result is empty after cleaning
            if not new_name:
                new_name = f"col_{i}"
        new_columns.append(new_name)
    df.columns = new_columns
    return df

# ------------------- Cloudflare D1 Client -------------------
class CloudflareD1Client:
    def __init__(self, account_id: str, database_id: str, api_token: str):
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"

    def query(self, sql: str, params: list = None) -> dict:
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        payload = {"sql": sql}
        if params:
            payload["params"] = [self._to_native(v) for v in params]
        response = requests.post(self.base_url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    def query_rows(self, sql: str, params: list = None) -> list:
        """Return rows list from a query result, or empty list on failure."""
        result = self.query(sql, params)
        if result.get("success") and result.get("result"):
            if isinstance(result["result"], list):
                return result["result"][0].get("results", [])
        return []

    def get_table_names(self):
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' AND name NOT GLOB '_*'"
        result = self.query(sql)
        if not result.get("success"):
            return []
        if result["result"] and isinstance(result["result"], list):
            rows = result["result"][0].get("results", [])
        else:
            rows = result.get("result", [])
        return [row["name"] for row in rows]
        
    def execute(self, sql: str, params: list = None) -> dict:
        """Execute a parameterized DML statement (UPDATE/INSERT/DELETE) on the remote D1 database."""
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        payload = {"sql": sql}
        if params:
            payload["params"] = [self._to_native(v) for v in params]
        response = requests.post(self.base_url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _to_native(value):
        """Convert numpy / pandas types to native Python for JSON serialization."""
        if value is None:
            return None
        try:
            import numpy as np
            if isinstance(value, (np.integer,)):
                return int(value)
            if isinstance(value, (np.floating,)):
                if np.isnan(value):
                    return None
                return float(value)
            if isinstance(value, np.bool_):
                return bool(value)
            if isinstance(value, np.ndarray):
                return value.tolist()
        except ImportError:
            pass
        if isinstance(value, float) and value != value:  # NaN sentinel
            return None
        return value

    def get_table_info(self, table_name: str) -> list:
        """Return PRAGMA table_info rows for a remote table (includes pk flag)."""
        result = self.query(f"PRAGMA table_info({table_name})")
        if result.get("success") and result.get("result"):
            if isinstance(result["result"], list):
                return result["result"][0].get("results", [])
        return []

    def debug_response(self, response, context=""):
        import json
        with open("d1_debug.log", "a") as f:
            f.write(f"\n=== {context} ===\n")
            f.write(json.dumps(response, indent=2))
            f.write("\n")

# ------------------- Inventory Worker Client -------------------
class InventoryWorkerClient:
    """POST /admin/inventory/move with x-api-key header."""

    def __init__(self, base_url: str, api_key: str = None, timeout_s: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.session = requests.Session()

    def move(self, payload: dict) -> dict:
        url = f"{self.base_url}/admin/inventory/move"
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        r = self.session.post(url, headers=headers, json=payload, timeout=self.timeout_s)
        if r.status_code == 401:
            raise RuntimeError("Unauthorized (check INVENTORY_API_KEY).")
        data = r.json()
        if not r.ok or not data.get("ok", False):
            raise RuntimeError(f"HTTP {r.status_code}: {json.dumps(data, indent=2)}")
        return data

    def recalc_enqueue(self, account_id: int, product_id: int, reason: str = "MANUAL_RECALC") -> dict:
        url = f"{self.base_url}/admin/recalc/enqueue"
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        payload = {"account_id": account_id, "product_id": product_id, "reason": reason}
        r = self.session.post(url, headers=headers, json=payload, timeout=self.timeout_s)
        if r.status_code == 401:
            raise RuntimeError("Unauthorized (check INVENTORY_API_KEY).")
        data = r.json()
        if not r.ok or not data.get("ok", False):
            raise RuntimeError(f"HTTP {r.status_code}: {json.dumps(data, indent=2)}")
        return data


# ------------------- Worker to Load All Data (dynamic tables) -------------------
class LoadDataWorker(QThread):
    finished = Signal(object, str)  # (table_data_dict, error_message)

    def __init__(self, client: CloudflareD1Client):
        super().__init__()
        self.client = client

    def run(self):
        try:
            table_names = self.client.get_table_names()
            # Exclude internal Cloudflare tables (start with underscore)
            table_names = [t for t in table_names if not t.startswith('_')]
            
            if not table_names:
                self.finished.emit(None, "No user tables found in database.")
                return

            table_data = {}
            for table in table_names:
                try:
                    result_json = self.client.query(f"SELECT * FROM {table}")
                    if not result_json.get("success"):
                        errors = result_json.get("errors", [])
                        error_msg = errors[0]["message"] if errors else f"Failed to fetch {table}"
                        print(f"Skipping {table}: {error_msg}")
                        continue
                    
                    # Extract rows
                    if result_json.get("result") and isinstance(result_json["result"], list):
                        rows = result_json["result"][0].get("results", [])
                    else:
                        rows = result_json.get("result", [])
                    
                    if rows:
                        df = pd.DataFrame(rows)
                    else:
                        # Empty table – get column names from PRAGMA
                        pragma = self.client.query(f"PRAGMA table_info({table})")
                        if pragma.get("success") and pragma.get("result"):
                            cols = pragma["result"][0].get("results", [])
                            if cols:
                                column_names = [c["name"] for c in cols]
                                df = pd.DataFrame(columns=column_names)
                            else:
                                df = pd.DataFrame()
                        else:
                            df = pd.DataFrame()
                    table_data[table] = df
                except Exception as e:
                    print(f"Error processing {table}: {e}")
                    continue
            
            if not table_data:
                self.finished.emit(None, "No data could be loaded from any table.")
            else:
                self.finished.emit(table_data, "")
        except Exception as e:
            self.finished.emit(None, str(e))

# ------------------- Pandas Model for QTableView -------------------
class PandasModel(QAbstractTableModel):
    cellEdited = Signal(object, str, object)  # (row_label, col_name, new_value)

    def __init__(self, data: pd.DataFrame, format_rules=None):
        super().__init__()
        self._data = data
        self._format_rules = format_rules or []  # list of {column, operator, value, color}
        self._dirty_labels = set()  # set of (row_label, col_name) tuples

    def rowCount(self, parent=QModelIndex()):
        return 0 if self._data is None else self._data.shape[0]

    def columnCount(self, parent=QModelIndex()):
        return 0 if self._data is None else self._data.shape[1]

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or self._data is None:
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            value = self._data.iloc[index.row(), index.column()]
            return str(value) if value is not None else ""
        if role == Qt.BackgroundRole:
            # Dirty cells get highest priority (light yellow)
            if self._dirty_labels:
                row_label = self._data.index[index.row()]
                col_name = self._data.columns[index.column()]
                if (row_label, col_name) in self._dirty_labels:
                    return QColor("#FFFACD")
            # Then check conditional format rules
            if self._format_rules:
                col_name = self._data.columns[index.column()]
                cell_value = self._data.iloc[index.row(), index.column()]
                for rule in self._format_rules:
                    if rule["column"] != col_name:
                        continue
                    if self._evaluate_rule(cell_value, rule["operator"], rule["value"]):
                        return QColor(rule["color"])
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid() or role != Qt.EditRole:
            return False
        row_label = self._data.index[index.row()]
        col_name = self._data.columns[index.column()]
        value = self._coerce_for_column(col_name, value)
        self._data.at[row_label, col_name] = value
        self.dataChanged.emit(index, index, [role])
        self.cellEdited.emit(row_label, col_name, value)
        return True

    def _coerce_for_column(self, col_name, value):
        """Cast value to match the column's dtype to avoid pandas deprecation warnings."""
        try:
            import numpy as np
            col_dtype = self._data[col_name].dtype
            if np.issubdtype(col_dtype, np.integer):
                return int(value)
            if np.issubdtype(col_dtype, np.floating):
                return float(value)
            if np.issubdtype(col_dtype, np.bool_):
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes", "y")
                return bool(value)
        except (ValueError, TypeError, AttributeError):
            pass
        return value

    def set_data(self, data: pd.DataFrame, format_rules=None):
        """Replace the backing DataFrame without destroying the model (preserves signals)."""
        self.beginResetModel()
        self._data = data
        if format_rules is not None:
            self._format_rules = format_rules
        self.endResetModel()

    def set_dirty_labels(self, dirty_set: set):
        """Update which cells are marked dirty (for background highlighting)."""
        self._dirty_labels = dirty_set
        if self.rowCount() > 0 and self.columnCount() > 0:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(self.rowCount() - 1, self.columnCount() - 1),
                [Qt.BackgroundRole]
            )

    def _evaluate_rule(self, cell_value, op, threshold):
        try:
            val = float(cell_value) if cell_value is not None else None
            if val is None:
                return False
            thresh = float(threshold)
            if op == '>': return val > thresh
            if op == '<': return val < thresh
            if op == '>=': return val >= thresh
            if op == '<=': return val <= thresh
            if op == '==': return val == thresh
            if op == '!=': return val != thresh
            return False
        except (ValueError, TypeError):
            return False

    def headerData(self, section, orientation, role):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return str(self._data.columns[section])
        return None

    def sort(self, column, order):
        if self._data is not None:
            col_name = self._data.columns[column]
            self.layoutAboutToBeChanged.emit()
            self._data = self._data.sort_values(col_name, ascending=(order == Qt.AscendingOrder))
            self.layoutChanged.emit()

# ------------------- Conditional Format Dialog -------------------
OPERATORS = [
    ("greater than", ">"),
    ("less than", "<"),
    ("greater than or equal", ">="),
    ("less than or equal", "<="),
    ("equals", "=="),
    ("not equals", "!="),
]

class ConditionalFormatDialog(QDialog):
    def __init__(self, parent, table_name, columns, existing_rules):
        super().__init__(parent)
        self.table_name = table_name
        self.columns = columns
        self.rules = list(existing_rules)  # copy
        self._editing_index = -1
        self.setWindowTitle(f"Conditional Formatting — {table_name}")
        self.setMinimumWidth(500)
        self.setModal(True)
        self._build_ui()
        self._refresh_rule_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Rule list
        layout.addWidget(QLabel("Rules (first match wins):"))
        self.rule_list = QListWidget()
        self.rule_list.currentRowChanged.connect(self._on_rule_selected)
        layout.addWidget(self.rule_list)

        # Buttons for rule list
        btn_row = QHBoxLayout()
        self.edit_btn = QPushButton("Edit Selected")
        self.edit_btn.clicked.connect(self._edit_rule)
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.clicked.connect(self._delete_rule)
        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.clicked.connect(self._clear_all)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Add/Edit form
        form_group = QGroupBox("Add / Edit Rule")
        form_layout = QFormLayout()

        self.col_combo = QComboBox()
        self.col_combo.addItems(self.columns)
        form_layout.addRow("Column:", self.col_combo)

        self.op_combo = QComboBox()
        for label, _ in OPERATORS:
            self.op_combo.addItem(label)
        form_layout.addRow("Operator:", self.op_combo)

        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("e.g. 10")
        form_layout.addRow("Value:", self.value_edit)

        color_layout = QHBoxLayout()
        self.color_button = QPushButton()
        self.color_button.setFixedSize(32, 24)
        self.color_button.clicked.connect(self._pick_color)
        self.color_label = QLabel()
        self._current_color = "#FF0000"
        self._update_color_button()
        color_layout.addWidget(self.color_button)
        color_layout.addWidget(self.color_label)
        color_layout.addStretch()
        form_layout.addRow("Color:", color_layout)

        form_group.setLayout(form_layout)
        layout.addWidget(form_group)

        add_btn_row = QHBoxLayout()
        self.add_btn = QPushButton("Add Rule")
        self.add_btn.clicked.connect(self._add_rule)
        self.cancel_edit_btn = QPushButton("Cancel Edit")
        self.cancel_edit_btn.clicked.connect(self._cancel_edit)
        self.cancel_edit_btn.setVisible(False)
        add_btn_row.addWidget(self.add_btn)
        add_btn_row.addWidget(self.cancel_edit_btn)
        add_btn_row.addStretch()
        layout.addLayout(add_btn_row)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self._current_color), self, "Choose Cell Color")
        if color.isValid():
            self._current_color = color.name()
            self._update_color_button()

    def _update_color_button(self):
        self.color_button.setStyleSheet(
            f"background-color: {self._current_color}; border: 1px solid #999;"
        )
        self.color_label.setText(self._current_color)

    def _add_rule(self):
        col = self.col_combo.currentText()
        op_idx = self.op_combo.currentIndex()
        op = OPERATORS[op_idx][1]
        value = self.value_edit.text().strip()
        if not value:
            QMessageBox.warning(self, "Missing Value", "Please enter a threshold value.")
            return
        rule = {"column": col, "operator": op, "value": value, "color": self._current_color}
        if self._editing_index >= 0:
            self.rules[self._editing_index] = rule
            self._cancel_edit()
        else:
            self.rules.append(rule)
        self._refresh_rule_list()

    def _edit_rule(self):
        idx = self.rule_list.currentRow()
        if idx < 0:
            return
        self._editing_index = idx
        rule = self.rules[idx]
        self.col_combo.setCurrentText(rule["column"])
        for i, (_, op_sym) in enumerate(OPERATORS):
            if op_sym == rule["operator"]:
                self.op_combo.setCurrentIndex(i)
                break
        self.value_edit.setText(rule["value"])
        self._current_color = rule["color"]
        self._update_color_button()
        self.add_btn.setText("Update Rule")
        self.cancel_edit_btn.setVisible(True)

    def _cancel_edit(self):
        self._editing_index = -1
        self.value_edit.clear()
        self.add_btn.setText("Add Rule")
        self.cancel_edit_btn.setVisible(False)

    def _delete_rule(self):
        idx = self.rule_list.currentRow()
        if idx >= 0:
            del self.rules[idx]
            if self._editing_index == idx:
                self._cancel_edit()
            elif self._editing_index > idx:
                self._editing_index -= 1
            self._refresh_rule_list()

    def _clear_all(self):
        self.rules.clear()
        self._cancel_edit()
        self._refresh_rule_list()

    def _on_rule_selected(self, idx):
        has_sel = idx >= 0
        self.edit_btn.setEnabled(has_sel)
        self.delete_btn.setEnabled(has_sel)

    def _refresh_rule_list(self):
        self.rule_list.clear()
        for rule in self.rules:
            op_label = next((l for l, s in OPERATORS if s == rule["operator"]), rule["operator"])
            item_text = f"{rule['column']} {op_label} {rule['value']}"
            item = QListWidgetItem(item_text)
            item.setBackground(QColor(rule["color"]))
            # Use white text on dark backgrounds
            c = QColor(rule["color"])
            if c.red() * 0.299 + c.green() * 0.587 + c.blue() * 0.114 < 128:
                item.setForeground(QColor("#FFFFFF"))
            self.rule_list.addItem(item)

    def get_rules(self):
        return self.rules

# ------------------- Spreadsheet Browser Widget -------------------
class SpreadsheetBrowser(QWidget):
    def __init__(self, local_conn_getter, get_hidden_tables_func, client_getter=None):
        super().__init__()
        self.local_conn_getter = local_conn_getter
        self.get_hidden_tables_func = get_hidden_tables_func  # returns set of hidden table names
        self.client_getter = client_getter  # returns CloudflareD1Client or None
        self.current_table = None
        self.current_df = pd.DataFrame()
        self.full_df = pd.DataFrame()
        self.format_rules = {}   # {table_name: [rule_dict, ...]}
        self._dirty_cells = {}   # {(row_label, col_name): new_value}
        self._model = None       # persistent PandasModel
        self._pk_cache = {}      # {table_name: [pk_col, ...]}
        self._load_format_rules()
        self.setup_ui()

    def _format_rules_key(self, table_name):
        return f"format_rules/{table_name}"

    def _load_format_rules(self):
        settings = QSettings("EbayBrowser", "D1Browser")
        self.format_rules = {}
        # QSettings doesn't support listing child groups, so we load on demand per table
        # We'll store all rules in a single JSON key to simplify
        raw = settings.value("format_rules_all", "{}")
        try:
            self.format_rules = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            self.format_rules = {}

    def _save_format_rules(self):
        settings = QSettings("EbayBrowser", "D1Browser")
        settings.setValue("format_rules_all", json.dumps(self.format_rules))

    def _get_columns_for_table(self, table_name):
        conn = self.local_conn_getter()
        if not conn:
            return []
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info([{table_name}])")
        return [row[1] for row in cursor.fetchall()]

    def _open_conditional_format_dialog(self):
        if not self.current_table:
            return
        columns = self._get_columns_for_table(self.current_table)
        if not columns:
            return
        existing = self.format_rules.get(self.current_table, [])
        dlg = ConditionalFormatDialog(self, self.current_table, columns, existing)
        if dlg.exec():
            self.format_rules[self.current_table] = dlg.get_rules()
            self._save_format_rules()
            self.update_table_view()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Table:"))
        self.table_combo = QComboBox()
        self.table_combo.currentTextChanged.connect(self.on_table_selected)
        controls.addWidget(self.table_combo)

        controls.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Type to filter rows (case-insensitive)...")
        self.filter_edit.textChanged.connect(self.apply_filter)
        controls.addWidget(self.filter_edit)

        self.case_sensitive_cb = QCheckBox("Case sensitive")
        self.case_sensitive_cb.stateChanged.connect(self.apply_filter)
        controls.addWidget(self.case_sensitive_cb)

        controls.addStretch()

        self.push_btn = QPushButton("Push Changes to D1")
        self.push_btn.setEnabled(False)
        self.push_btn.setStyleSheet("QPushButton:enabled { background-color: #FFD700; font-weight: bold; }")
        self.push_btn.clicked.connect(self.push_changes_to_d1)
        controls.addWidget(self.push_btn)

        self.discard_btn = QPushButton("Discard Changes")
        self.discard_btn.setEnabled(False)
        self.discard_btn.clicked.connect(self.discard_changes)
        controls.addWidget(self.discard_btn)

        self.dirty_label = QLabel("")
        controls.addWidget(self.dirty_label)

        layout.addLayout(controls)

        self.table_view = QTableView()
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table_view.setSortingEnabled(True)
        self.table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.table_view)

    def refresh_table_list(self):
        """Populate table dropdown, excluding hidden tables."""
        conn = self.local_conn_getter()
        if not conn:
            return
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name")
        all_tables = [row[0] for row in cursor.fetchall()]
        hidden = self.get_hidden_tables_func()
        visible_tables = [t for t in all_tables if t not in hidden]
        self.table_combo.clear()
        self.table_combo.addItems(visible_tables)

    def on_table_selected(self, table_name):
        if not table_name:
            return
        self.current_table = table_name
        self.load_full_table()

    def load_full_table(self):
        conn = self.local_conn_getter()
        if not conn or not self.current_table:
            return
        try:
            self.full_df = pd.read_sql_query(f"SELECT * FROM [{self.current_table}]", conn)
            self.current_df = self.full_df.copy()
            self._dirty_cells.clear()
            self._update_dirty_ui()
            self._model = None  # force fresh model for new table
            self.apply_filter()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load table {self.current_table}:\n{e}")

    def apply_filter(self):
        if not hasattr(self, 'full_df') or self.full_df is None or self.full_df.empty:
            self.current_df = pd.DataFrame()
            self.update_table_view()
            return
        filter_text = self.filter_edit.text()
        if not filter_text:
            self.current_df = self.full_df.copy()
        else:
            if not self.case_sensitive_cb.isChecked():
                filter_text = filter_text.lower()
                mask = self.full_df.apply(lambda row: row.astype(str).str.lower().str.contains(filter_text).any(), axis=1)
            else:
                mask = self.full_df.apply(lambda row: row.astype(str).str.contains(filter_text).any(), axis=1)
            self.current_df = self.full_df[mask]
        self.update_table_view()

    def update_table_view(self):
        rules = self.format_rules.get(self.current_table, [])
        if self._model is None:
            self._model = PandasModel(self.current_df, rules)
            self._model.cellEdited.connect(self._on_cell_edited)
            self.table_view.setModel(self._model)
        else:
            self._model.set_data(self.current_df, rules)
        self._model.set_dirty_labels(set(self._dirty_cells.keys()))
        self.table_view.setSortingEnabled(True)

    def _on_context_menu(self, pos):
        if not self.current_table:
            return
        menu = QMenu(self)
        format_action = menu.addAction("Conditional Formatting...")
        menu.addSeparator()
        delete_action = menu.addAction("Delete Selected Row(s)")
        action = menu.exec(self.table_view.viewport().mapToGlobal(pos))
        if action == format_action:
            self._open_conditional_format_dialog()
        elif action == delete_action:
            self._delete_selected_rows()

    def _on_cell_edited(self, row_label, col_name, new_value):
        """Called when the user edits a cell in the model."""
        # Coerce to column dtype to avoid pandas deprecation warnings
        try:
            import numpy as np
            col_dtype = self.full_df[col_name].dtype
            if np.issubdtype(col_dtype, np.integer):
                new_value = int(new_value)
            elif np.issubdtype(col_dtype, np.floating):
                new_value = float(new_value)
            elif np.issubdtype(col_dtype, np.bool_):
                if isinstance(new_value, str):
                    new_value = new_value.lower() in ("true", "1", "yes", "y")
                else:
                    new_value = bool(new_value)
        except (ValueError, TypeError, AttributeError):
            pass
        self.full_df.at[row_label, col_name] = new_value
        self._dirty_cells[(row_label, col_name)] = new_value
        self._update_dirty_ui()

    def _update_dirty_ui(self):
        count = len(self._dirty_cells)
        if count > 0:
            self.push_btn.setEnabled(True)
            self.discard_btn.setEnabled(True)
            self.dirty_label.setText(f"{count} unsaved change(s)")
        else:
            self.push_btn.setEnabled(False)
            self.discard_btn.setEnabled(False)
            self.dirty_label.setText("")

    def _get_primary_keys(self, table_name):
        """Return list of primary-key column names for a table, using cached remote info."""
        if table_name in self._pk_cache:
            return self._pk_cache[table_name]
        client = self.client_getter() if self.client_getter else None
        if client:
            info = client.get_table_info(table_name)
            pks = [row["name"] for row in info if row.get("pk", 0) > 0]
            if pks:
                self._pk_cache[table_name] = pks
                return pks
        # Fallback: heuristic — columns named like <table>_id or just 'id'
        conn = self.local_conn_getter()
        if conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info([{table_name}])")
            cols = [row[1] for row in cursor.fetchall()]
            candidates = [f"{table_name}_id", "id"]
            if table_name.endswith('s'):
                candidates.insert(1, f"{table_name[:-1]}_id")
            for c in candidates:
                if c in cols:
                    self._pk_cache[table_name] = [c]
                    return [c]
        self._pk_cache[table_name] = []
        return []

    def push_changes_to_d1(self):
        """Send all dirty cell edits to Cloudflare D1 as UPDATE statements."""
        if not self._dirty_cells:
            return
        client = self.client_getter() if self.client_getter else None
        if not client:
            QMessageBox.warning(self, "Not Connected", "No D1 client available. Check credentials.")
            return
        table = self.current_table
        pk_cols = self._get_primary_keys(table)
        if not pk_cols:
            QMessageBox.warning(self, "No Primary Key",
                f"Table '{table}' has no detectable primary key. Cannot safely update rows.\n"
                "Edits will remain local only. Re-sync to discard them.")
            return

        errors = []
        pushed = 0
        # Group by row_label to build one UPDATE per row with multiple column changes
        row_updates = {}  # {row_label: {col_name: new_value}}
        for (row_label, col_name), new_value in self._dirty_cells.items():
            row_updates.setdefault(row_label, {})[col_name] = new_value

        for row_label, col_changes in row_updates.items():
            try:
                # Get primary key values from full_df
                pk_vals = [self.full_df.at[row_label, pk] for pk in pk_cols]
                set_parts = [f"[{col}] = ?" for col in col_changes]
                where_parts = [f"[{pk}] = ?" for pk in pk_cols]
                sql = f"UPDATE [{table}] SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}"
                params = list(col_changes.values()) + pk_vals
                result = client.execute(sql, params)
                if not result.get("success"):
                    errors.append(f"Row {row_label}: {result.get('errors', ['unknown'])[0]}")
                else:
                    pushed += 1
            except Exception as e:
                errors.append(f"Row {row_label}: {e}")

        if errors:
            QMessageBox.warning(self, "Partial Success",
                f"Pushed {pushed} row(s).\n{len(errors)} error(s):\n" + "\n".join(errors[:10]))
        else:
            QMessageBox.information(self, "Saved", f"Successfully pushed {pushed} row(s) to D1.")
            self._dirty_cells.clear()
            self._update_dirty_ui()
            self._model.set_dirty_labels(set())

    def discard_changes(self):
        """Revert all unsaved edits by reloading the current table from the in-memory database."""
        self._dirty_cells.clear()
        self._update_dirty_ui()
        if self._model:
            self._model.set_dirty_labels(set())
        self.load_full_table()

    def _delete_selected_rows(self):
        """Delete selected rows from D1 and local data."""
        if not self.current_table:
            return
        table = self.current_table
        client = self.client_getter() if self.client_getter else None
        if not client:
            QMessageBox.warning(self, "Not Connected", "No D1 client available. Check credentials.")
            return
        pk_cols = self._get_primary_keys(table)
        if not pk_cols:
            QMessageBox.warning(self, "No Primary Key",
                f"Table '{table}' has no detectable primary key. Cannot safely delete rows.")
            return

        selection_model = self.table_view.selectionModel()
        if not selection_model or not selection_model.hasSelection():
            QMessageBox.information(self, "No Selection", "Select one or more rows to delete.")
            return

        rows = sorted(set(idx.row() for idx in selection_model.selectedIndexes()), reverse=True)
        labels = []
        for row in rows:
            if row < len(self._model._data.index):
                labels.append(self._model._data.index[row])

        if not labels:
            return

        reply = QMessageBox.question(self, "Confirm Delete",
            f"Delete {len(labels)} row(s) from '{table}' on Cloudflare D1?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        errors = []
        deleted = 0
        for row_label in labels:
            try:
                pk_vals = [self.full_df.at[row_label, pk] for pk in pk_cols]
                where_parts = [f"[{pk}] = ?" for pk in pk_cols]
                sql = f"DELETE FROM [{table}] WHERE {' AND '.join(where_parts)}"
                result = client.execute(sql, pk_vals)
                if not result.get("success"):
                    errors.append(f"Row {row_label}: {result.get('errors', ['unknown'])[0]}")
                else:
                    deleted += 1
                    # Remove from local DataFrames
                    self.full_df.drop(row_label, inplace=True)
                    self._dirty_cells = {k: v for k, v in self._dirty_cells.items() if k[0] != row_label}
            except Exception as e:
                errors.append(f"Row {row_label}: {e}")

        self._update_dirty_ui()
        self.apply_filter()
        if errors:
            QMessageBox.warning(self, "Partial Success",
                f"Deleted {deleted} row(s).\n{len(errors)} error(s):\n" + "\n".join(errors[:10]))


# ------------------- SQL Query Tab -------------------
class SQLQueryTab(QWidget):
    def __init__(self, local_conn_getter):
        super().__init__()
        self.local_conn_getter = local_conn_getter
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        self.query_edit = QTextEdit()
        self.query_edit.setFont(QFont("Courier New", 10))
        self.query_edit.setMaximumHeight(120)
        self.query_edit.setText("SELECT * FROM listings LIMIT 100")
        layout.addWidget(self.query_edit)

        btn_layout = QHBoxLayout()
        self.execute_btn = QPushButton("▶ Execute Local Query")
        self.execute_btn.clicked.connect(self.execute_query)
        self.reset_btn = QPushButton("📋 Reset to Full Listings")
        self.reset_btn.clicked.connect(self.reset_to_full)
        btn_layout.addWidget(self.execute_btn)
        btn_layout.addWidget(self.reset_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.table_view = QTableView()
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table_view)

    def execute_query(self):
        conn = self.local_conn_getter()
        if not conn:
            QMessageBox.warning(self, "No Data", "Data not loaded yet.")
            return
        sql = self.query_edit.toPlainText().strip()
        if not sql:
            return
        try:
            df = pd.read_sql_query(sql, conn)
            model = PandasModel(df)
            self.table_view.setModel(model)
            self.table_view.setSortingEnabled(True)
        except Exception as e:
            QMessageBox.critical(self, "Query Error", str(e))

    def reset_to_full(self):
        self.query_edit.setText("SELECT * FROM listings")
        self.execute_query()


# ------------------- Inventory Tab Workers -------------------
class LoadReasonsWorker(QThread):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, d1_client):
        super().__init__()
        self.d1_client = d1_client

    def run(self):
        try:
            sql = """SELECT reason_code, COALESCE(description,'') AS description
                     FROM stock_ledger_reason_codes WHERE is_active = 1 ORDER BY reason_code"""
            rows = self.d1_client.query_rows(sql)
            reasons = [(str(r["reason_code"]), str(r.get("description", "") or ""))
                       for r in rows if isinstance(r, dict)]
            self.finished.emit(reasons)
        except Exception as e:
            self.error.emit(str(e))


class SearchProductsWorker(QThread):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, d1_client, query_text):
        super().__init__()
        self.d1_client = d1_client
        self.query_text = query_text

    def run(self):
        try:
            q = self.query_text.strip()
            try:
                pid = int(q)
            except ValueError:
                pid = None
            sql = """SELECT p.product_id, p.upc, p.model, p.brand,
                            COALESCE(sb.qty_on_hand, 0) AS qty_on_hand,
                            COALESCE(sb.qty_reserved, 0) AS qty_reserved
                     FROM products p
                     LEFT JOIN stock_balance sb ON sb.product_id = p.product_id
                     WHERE (? IS NOT NULL AND p.product_id = ?)
                        OR (p.upc IS NOT NULL AND p.upc LIKE '%' || ? || '%')
                        OR (p.model IS NOT NULL AND p.model LIKE '%' || ? || '%')
                        OR (p.brand IS NOT NULL AND p.brand LIKE '%' || ? || '%')
                     ORDER BY p.product_id
                     LIMIT ?"""
            params = [pid, pid, q, q, q, 100]
            rows = self.d1_client.query_rows(sql, params)
            self.finished.emit([r for r in rows if isinstance(r, dict)])
        except Exception as e:
            self.error.emit(str(e))


class FetchQueueErrorsWorker(QThread):
    finished = Signal(int, list)  # (product_id, [recalc_queue rows...])
    error = Signal(str)

    def __init__(self, d1_client, product_id):
        super().__init__()
        self.d1_client = d1_client
        self.product_id = product_id

    def run(self):
        try:
            sql = """SELECT account_id, requested_at, reason, last_error
                     FROM recalc_queue
                     WHERE product_id = ? AND last_error IS NOT NULL
                     ORDER BY requested_at DESC"""
            rows = self.d1_client.query_rows(sql, [int(self.product_id)])
            self.finished.emit(self.product_id, rows)
        except Exception as e:
            self.error.emit(str(e))


class FetchStockWorker(QThread):
    finished = Signal(int, object)
    error = Signal(str)

    def __init__(self, d1_client, product_id):
        super().__init__()
        self.d1_client = d1_client
        self.product_id = product_id

    def run(self):
        try:
            sql = """SELECT product_id, qty_on_hand, qty_reserved, updated_at
                     FROM stock_balance WHERE product_id = ?"""
            rows = self.d1_client.query_rows(sql, [int(self.product_id)])
            sb = rows[0] if rows else None
            self.finished.emit(self.product_id, sb)
        except Exception as e:
            self.error.emit(str(e))


class SubmitMoveWorker(QThread):
    finished = Signal(dict, dict)
    error = Signal(str)

    def __init__(self, worker_client, payload):
        super().__init__()
        self.worker_client = worker_client
        self.payload = payload

    def run(self):
        try:
            resp = self.worker_client.move(self.payload)
            self.finished.emit(self.payload, resp)
        except Exception as e:
            self.error.emit(str(e))


class SubmitRecalcWorker(QThread):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, worker_client, account_id, product_id, reason="MANUAL_RECALC"):
        super().__init__()
        self.worker_client = worker_client
        self.account_id = account_id
        self.product_id = product_id
        self.reason = reason

    def run(self):
        try:
            resp = self.worker_client.recalc_enqueue(self.account_id, self.product_id, self.reason)
            self.finished.emit(resp)
        except Exception as e:
            self.error.emit(str(e))


# ------------------- eBay Import Workers -------------------
class PreviewImportWorker(QThread):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, csv_path, encoding="utf-8-sig"):
        super().__init__()
        self.csv_path = csv_path
        self.encoding = encoding

    def run(self):
        try:
            from import_ebay_active_listings import scan_relevant_upcs
            upc_counts = scan_relevant_upcs(self.csv_path, self.encoding)
            multipack = sum(1 for c in upc_counts.values() if c >= 2)
            total_listings = sum(upc_counts.values())
            self.finished.emit({
                "distinct_upcs": len(upc_counts),
                "multipack_upcs": multipack,
                "total_listings": total_listings,
            })
        except Exception as e:
            self.error.emit(str(e))


class ImportEbayListingsWorker(QThread):
    progress = Signal(int, int, str)
    phase = Signal(str)
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, csv_path, account_id, encoding, d1_client):
        super().__init__()
        self.csv_path = csv_path
        self.account_id = account_id
        self.encoding = encoding
        self.d1_client = d1_client
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            from import_ebay_active_listings import scan_relevant_upcs, iter_import_statements

            self.phase.emit("scanning")
            upc_counts = scan_relevant_upcs(self.csv_path, self.encoding)
            total_upcs = len(upc_counts)
            total_listings = sum(upc_counts.values())
            single_upcs = sum(1 for c in upc_counts.values() if c == 1)

            # total_logical = rows touched across all batches (for progress bar scale)
            total_logical = (total_upcs * 2 + total_listings * 2
                             + total_upcs + single_upcs)
            batch_count = (2 + 2  # products INSERT+UPDATE, stock INSERT+UPDATE
                           + -(-total_listings // 50))  # listing UPSERT batches

            self.phase.emit(f"importing ({total_upcs} UPCs, {total_listings} listings, "
                            f"~{batch_count} batched requests)")

            stats = {
                "products_inserted": 0,
                "products_updated": 0,
                "listings_upserted": 0,
                "stock_balance_inserted": 0,
                "stock_balance_updated": 0,
                "errors": 0,
                "errors_list": [],
                "cancelled": False,
            }

            current = 0
            batch_num = 0
            for item in iter_import_statements(
                self.csv_path, self.account_id, upc_counts, self.encoding
            ):
                if len(item) == 4:
                    category, stmt_type, sql, row_count = item
                else:
                    category, stmt_type, sql = item
                    row_count = 1
                if self._cancelled:
                    stats["cancelled"] = True
                    break

                batch_num += 1
                sql_preview = sql[:400] + "..." if len(sql) > 400 else sql
                try:
                    result = self.d1_client.execute(sql)
                    current += row_count
                    tag = f"[{batch_num}/{batch_count}]"

                    if result.get("success"):
                        if category == "listings":
                            stats["listings_upserted"] += row_count
                        else:
                            suffix = "ed" if stmt_type == "INSERT" else "d"
                            key = f"{category}_{stmt_type.lower()}{suffix}"
                            stats[key] = stats.get(key, 0) + row_count
                        self.progress.emit(current, total_logical,
                                           f"{tag} OK: {category} {stmt_type} x{row_count}")
                    else:
                        stats["errors"] += 1
                        errs = result.get("errors", [])
                        msg = errs[0].get("message", "Unknown error") if errs else str(result)
                        full = f"SQL: {sql_preview}\nError: {msg}"
                        stats["errors_list"].append(full)
                        self.progress.emit(current, total_logical,
                                           f"{tag} FAIL ({category} {stmt_type}): {msg[:100]} | SQL: {sql_preview[:100]}")
                except Exception as e:
                    stats["errors"] += 1
                    current += row_count
                    msg = str(e)
                    if hasattr(e, 'response') and e.response is not None:
                        try:
                            cf_body = e.response.text
                            if cf_body:
                                msg = f"{msg}\nCF response: {cf_body[:600]}"
                        except Exception:
                            pass
                    full = f"SQL: {sql_preview}\nError: {msg}"
                    stats["errors_list"].append(full)
                    self.progress.emit(current, total_logical,
                                       f"{tag} ERROR ({category} {stmt_type}): {msg[:100]} | SQL: {sql_preview[:100]}")

            stats["total_statements"] = batch_num
            self.finished.emit(stats)
        except Exception as e:
            self.error.emit(str(e))


# ------------------- Backup / Restore Workers -------------------
class BackupWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, d1_client, filepath):
        super().__init__()
        self.d1_client = d1_client
        self.filepath = filepath

    def run(self):
        try:
            schema_rows = self.d1_client.query_rows(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE '_cf_%' AND name != 'sqlite_sequence' ORDER BY name"
            )
            tables = []
            total = len(schema_rows)
            for i, row in enumerate(schema_rows):
                name = row["name"]
                sql = row.get("sql", "")
                self.progress.emit(i + 1, total, f"Backing up {name}...")
                data_rows = self.d1_client.query_rows(f"SELECT * FROM {name}")
                tables.append({"name": name, "sql": sql, "rows": data_rows})

            import json
            from datetime import datetime, timezone
            backup = {
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "tables": tables,
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(backup, f, indent=2, default=str)
            self.finished.emit(self.filepath)
        except Exception as e:
            self.error.emit(str(e))


class RestoreWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, d1_client, filepath):
        super().__init__()
        self.d1_client = d1_client
        self.filepath = filepath
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            import json
            with open(self.filepath, "r", encoding="utf-8") as f:
                backup = json.load(f)

            tables = backup.get("tables", [])
            if not tables:
                self.finished.emit({"dropped": 0, "created": 0, "rows_inserted": 0, "errors": 0})
                return

            ordered = self._topo_sort(tables)
            total = len(ordered) * 3  # drop + create + insert estimate
            step = 0
            stats = {"dropped": 0, "created": 0, "rows_inserted": 0, "errors": 0,
                     "errors_list": []}

            # Phase 1: drop in reverse dependency order
            for table in reversed(ordered):
                if self._cancelled:
                    break
                step += 1
                self.progress.emit(step, total, f"Dropping {table['name']}...")
                try:
                    self.d1_client.execute(f"DROP TABLE IF EXISTS {table['name']}")
                    stats["dropped"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    msg = f"Drop {table['name']}: {e}"
                    stats["errors_list"].append(msg)
                    self.progress.emit(step, total, f"ERROR: {msg}")

            # Phase 2: create in dependency order
            for table in ordered:
                if self._cancelled:
                    break
                step += 1
                self.progress.emit(step, total, f"Creating {table['name']}...")
                try:
                    self.d1_client.execute(table["sql"])
                    stats["created"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    msg = f"Create {table['name']}: {e}"
                    stats["errors_list"].append(msg)
                    self.progress.emit(step, total, f"ERROR: {msg}")

            # Phase 3: insert data
            for table in ordered:
                if self._cancelled:
                    break
                rows = table.get("rows", [])
                if not rows:
                    continue
                step += 1
                self.progress.emit(step, total,
                                   f"Inserting {len(rows)} rows into {table['name']}...")
                try:
                    inserted = self._insert_table(table["name"], rows)
                    stats["rows_inserted"] += inserted
                except Exception as e:
                    stats["errors"] += 1
                    msg = f"Insert {table['name']}: {e}"
                    stats["errors_list"].append(msg)
                    self.progress.emit(step, total, f"ERROR: {msg}")

            self.finished.emit(stats)
        except Exception as e:
            self.error.emit(str(e))

    def _topo_sort(self, tables):
        """Sort so referenced tables come before tables that reference them."""
        import re
        fk_re = re.compile(r'FOREIGN\s+KEY\s*\([^)]+\)\s*REFERENCES\s+["\[]?(\w+)',
                           re.IGNORECASE)
        name_set = {t["name"] for t in tables}
        deps = {t["name"]: set() for t in tables}

        for t in tables:
            for ref in fk_re.findall(t["sql"]):
                if ref in name_set and ref != t["name"]:
                    deps[t["name"]].add(ref)

        result = []
        remaining = set(deps.keys())
        idx = {t["name"]: i for i, t in enumerate(tables)}

        while remaining:
            ready = [n for n in remaining if not (deps[n] & remaining)]
            if not ready:
                for n in sorted(remaining):
                    result.append(tables[idx[n]])
                break
            for n in sorted(ready):
                result.append(tables[idx[n]])
                remaining.discard(n)
        return result

    def _insert_table(self, name, rows):
        """Insert rows using literal SQL batches of 50."""
        if not rows:
            return 0
        columns = list(rows[0].keys())
        col_list = ", ".join(columns)
        inserted = 0
        for i in range(0, len(rows), 50):
            batch = rows[i:i + 50]
            values_parts = []
            for row in batch:
                vals = [self._sql_literal(row.get(c)) for c in columns]
                values_parts.append("(" + ", ".join(vals) + ")")
            sql = f"INSERT INTO {name} ({col_list}) VALUES {','.join(values_parts)}"
            self.d1_client.execute(sql)
            inserted += len(batch)
        return inserted

    @staticmethod
    def _sql_literal(v):
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"


# ------------------- Import eBay Tab -------------------
class ImportEbayTab(QWidget):
    def __init__(self, d1_client_getter=None):
        super().__init__()
        self.d1_client_getter = d1_client_getter
        self._preview_data = None
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # File picker row
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("CSV File:"))
        self.csv_path_edit = QLineEdit()
        self.csv_path_edit.setPlaceholderText("Select eBay All Active Listings CSV...")
        file_layout.addWidget(self.csv_path_edit)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._on_browse)
        file_layout.addWidget(self.browse_btn)
        layout.addLayout(file_layout)

        # Account ID row
        acct_layout = QHBoxLayout()
        acct_layout.addWidget(QLabel("Account ID:"))
        self.account_spin = QSpinBox()
        self.account_spin.setMinimum(1)
        self.account_spin.setMaximum(9999)
        saved_acct = QSettings("EbayBrowser", "D1Browser").value("import_account_id", 1)
        try:
            self.account_spin.setValue(int(saved_acct))
        except (ValueError, TypeError):
            self.account_spin.setValue(1)
        self.account_spin.valueChanged.connect(self._on_account_changed)
        acct_layout.addWidget(self.account_spin)
        acct_layout.addStretch()
        layout.addLayout(acct_layout)

        # Button row
        btn_layout = QHBoxLayout()
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.clicked.connect(self._on_preview)
        self.import_btn = QPushButton("Import")
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._on_import)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self.preview_btn)
        btn_layout.addWidget(self.import_btn)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # Summary label
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("border: 1px solid #999; padding: 4px;")
        layout.addWidget(self.summary_label)

        # Log output
        layout.addWidget(QLabel("Log:"))
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Courier New", 9))
        self.log_output.setMaximumBlockCount(2000)
        layout.addWidget(self.log_output)

    def _d1_client(self):
        if self.d1_client_getter:
            return self.d1_client_getter()
        return None

    def _wait_worker(self, name):
        worker = getattr(self, name, None)
        if worker is not None and worker.isRunning():
            worker.wait(3000)

    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select eBay Active Listings CSV", "",
            "CSV Files (*.csv);;All Files (*)"
        )
        if path:
            self.csv_path_edit.setText(path)

    def _on_account_changed(self, value):
        QSettings("EbayBrowser", "D1Browser").setValue("import_account_id", value)

    def _on_preview(self):
        csv_path = self.csv_path_edit.text().strip()
        if not csv_path:
            self._log("Error: No CSV file selected.")
            return
        if not os.path.exists(csv_path):
            self._log(f"Error: File not found: {csv_path}")
            return
        d1 = self._d1_client()
        if not d1:
            self._log("Error: No D1 connection configured. Set credentials in Settings first.")
            return
        self.preview_btn.setEnabled(False)
        self._log(f"Scanning: {csv_path}")
        self._wait_worker("preview_worker")
        self.preview_worker = PreviewImportWorker(csv_path)
        self.preview_worker.finished.connect(self._on_preview_done)
        self.preview_worker.error.connect(self._on_preview_error)
        self.preview_worker.start()

    def _on_preview_done(self, data):
        self.preview_btn.setEnabled(True)
        self._preview_data = data
        multi = data["multipack_upcs"]
        total_upcs = data["distinct_upcs"]
        total_listings = data["total_listings"]
        self._log(f"Preview complete: {total_upcs} distinct UPCs, "
                  f"{multi} multipack, {total_listings} total listings (Condition=New)")
        self.summary_label.setText(
            f"Ready to import: {total_upcs} UPCs ({multi} multipack), "
            f"{total_listings} listings. "
            f"Estimated {2 * total_upcs + 2 * total_listings} SQL statements."
        )
        self.import_btn.setEnabled(True)

    def _on_preview_error(self, msg):
        self.preview_btn.setEnabled(True)
        self._log(f"Preview error: {msg}")

    def _on_import(self):
        csv_path = self.csv_path_edit.text().strip()
        account_id = self.account_spin.value()
        d1 = self._d1_client()
        if not d1:
            self._log("Error: No D1 connection configured.")
            return
        self.import_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self._log("Starting import...")
        self._wait_worker("import_worker")
        self.import_worker = ImportEbayListingsWorker(
            csv_path, account_id, "utf-8-sig", d1
        )
        self.import_worker.phase.connect(self._on_import_phase)
        self.import_worker.progress.connect(self._on_import_progress)
        self.import_worker.finished.connect(self._on_import_done)
        self.import_worker.error.connect(self._on_import_error)
        self.import_worker.start()

    def _on_cancel(self):
        if hasattr(self, "import_worker") and self.import_worker.isRunning():
            self.import_worker.cancel()
            self._log("Cancelling...")
        self.cancel_btn.setEnabled(False)

    def _on_import_phase(self, msg):
        self._log(f"Phase: {msg}")

    def _on_import_progress(self, current, total, message):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self._log(message)

    def _on_import_done(self, stats):
        self.import_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        lines = ["--- Import Complete ---"]
        if stats.get("cancelled"):
            lines.append("(cancelled by user)")
        lines.append(
            f"Products: {stats.get('products_inserted', 0)} inserted, "
            f"{stats.get('products_updated', 0)} updated"
        )
        lines.append(
            f"Listings: {stats.get('listings_upserted', 0)} upserted"
        )
        lines.append(
            f"Stock balance: {stats.get('stock_balance_inserted', 0)} inserted, "
            f"{stats.get('stock_balance_updated', 0)} updated"
        )
        lines.append(f"HTTP requests: {stats.get('total_statements', 0)}")
        lines.append(f"Errors: {stats.get('errors', 0)}")
        errors_list = stats.get("errors_list", [])
        if errors_list:
            lines.append("")
            lines.append("Error details:")
            for e in errors_list:
                lines.append(f"  - {e[:1000]}")
        summary = "\n".join(lines)
        self._log(summary)
        self.summary_label.setText(summary.replace("\n", " | "))

    def _on_import_error(self, msg):
        self.import_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self._log(f"Fatal import error: {msg}")

    def _log(self, msg):
        self.log_output.appendPlainText(msg)


# ------------------- Utility -------------------
def guess_entered_by():
    return os.getenv("USERNAME") or os.getenv("USER") or os.getenv("LOGNAME") or "unknown"


def new_ref_id(prefix):
    return f"{prefix.lower()}-{int(time.time())}"


# ------------------- Inventory Tab -------------------
class InventoryTab(QWidget):
    def __init__(self, d1_client_getter=None, worker_config_getter=None):
        super().__init__()
        self.d1_client_getter = d1_client_getter
        self.worker_config_getter = worker_config_getter
        self._products = []
        self._reasons = []
        self._selected_product_id = None
        self._selected_brand = ""
        self._selected_model = ""
        self._scan_mode = True
        self._reasons_loaded = False
        self._current_reason_code = "RECEIVE"
        self._current_reference_type = "PO"
        self._current_ref_prefix = "po"
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._do_search)
        self._pending_scan_submit = False
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # Top bar
        top = QHBoxLayout()
        self.scan_btn = QPushButton("Scan: ON")
        self.scan_btn.setCheckable(True)
        self.scan_btn.setChecked(True)
        self.scan_btn.clicked.connect(self._toggle_scan)
        top.addWidget(self.scan_btn)
        top.addWidget(QLabel("Product:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search... (UPC/model/brand/product_id)")
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.search_edit.returnPressed.connect(self._on_search_return)
        top.addWidget(self.search_edit)
        layout.addLayout(top)

        # Splitter: left = product results, right = move form
        splitter = QSplitter(Qt.Horizontal)

        # Left panel
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Product Results"))
        self.product_table = QTableWidget()
        self.product_table.setColumnCount(6)
        self.product_table.setHorizontalHeaderLabels(["product_id", "upc", "model", "brand", "on_hand", "reserved"])
        self.product_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.product_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.product_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.product_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.product_table.doubleClicked.connect(self._on_product_double_clicked)
        left_layout.addWidget(self.product_table)
        self.product_note = QLabel("")
        left_layout.addWidget(self.product_note)
        splitter.addWidget(left)

        # Right panel
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_layout.addWidget(QLabel("Move / Receive"))

        # Action templates
        right_layout.addWidget(QLabel("Action templates:"))
        tpl_layout = QHBoxLayout()
        self.tpl_receive_btn = QPushButton("Receive Shipment")
        self.tpl_receive_btn.clicked.connect(lambda: self._apply_template("RECEIVE", "PO", "po"))
        self.tpl_return_btn = QPushButton("Customer Return")
        self.tpl_return_btn.clicked.connect(lambda: self._apply_template("RETURN", "RMA", "rma"))
        self.tpl_damage_btn = QPushButton("Damage / Write-off")
        self.tpl_damage_btn.clicked.connect(lambda: self._apply_template("DAMAGE", "MANUAL", "damage"))
        self.tpl_adjust_btn = QPushButton("Adjust Count")
        self.tpl_adjust_btn.clicked.connect(lambda: self._apply_template("ADJUST", "MANUAL", "adjust"))
        self._tpl_buttons = {
            "RECEIVE": self.tpl_receive_btn,
            "RETURN": self.tpl_return_btn,
            "DAMAGE": self.tpl_damage_btn,
            "ADJUST": self.tpl_adjust_btn,
        }
        tpl_layout.addWidget(self.tpl_receive_btn)
        tpl_layout.addWidget(self.tpl_return_btn)
        tpl_layout.addWidget(self.tpl_damage_btn)
        tpl_layout.addWidget(self.tpl_adjust_btn)
        right_layout.addLayout(tpl_layout)

        # Second row: utility actions
        util_layout = QHBoxLayout()
        self.recheck_btn = QPushButton("Queue Recheck")
        self.recheck_btn.setToolTip("Enqueue a recalc for this product without touching inventory")
        self.recheck_btn.clicked.connect(self._on_recheck)
        util_layout.addWidget(self.recheck_btn)
        util_layout.addStretch()
        right_layout.addLayout(util_layout)

        # Form fields
        form_layout = QFormLayout()
        self.product_id_edit = QLineEdit()
        self.product_id_edit.setPlaceholderText("(select from left, or type product_id)")
        form_layout.addRow("Selected product_id:", self.product_id_edit)

        self.account_id_edit = QLineEdit("1")
        self.account_id_edit.setMaximumWidth(80)
        form_layout.addRow("account_id:", self.account_id_edit)

        self.stock_label = QLabel("--")
        form_layout.addRow("Current stock:", self.stock_label)

        self.queue_error_label = QLabel("")
        self.queue_error_label.setWordWrap(True)
        self.queue_error_label.setStyleSheet(
            "color: #cc0000; background-color: #fff0f0; border: 1px solid #cc0000; padding: 6px;"
        )
        self.queue_error_label.setVisible(False)
        form_layout.addRow("Queue errors:", self.queue_error_label)

        self.qty_delta_edit = QLineEdit()
        self.qty_delta_edit.setPlaceholderText("e.g. 5")
        self.qty_delta_edit.returnPressed.connect(self._on_submit)
        form_layout.addRow("qty_delta (Enter submits):", self.qty_delta_edit)

        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("optional")
        form_layout.addRow("notes:", self.notes_edit)

        self.reason_text_edit = QLineEdit()
        self.reason_text_edit.setPlaceholderText("optional")
        form_layout.addRow("reason text:", self.reason_text_edit)

        right_layout.addLayout(form_layout)

        # Action buttons
        btn_layout = QHBoxLayout()
        self.submit_btn = QPushButton("Submit")
        self.submit_btn.clicked.connect(self._on_submit)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear)
        self.refresh_reasons_btn = QPushButton("Refresh Reasons")
        self.refresh_reasons_btn.clicked.connect(self.load_reasons)
        btn_layout.addWidget(self.submit_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.refresh_reasons_btn)
        right_layout.addLayout(btn_layout)

        splitter.addWidget(right)
        splitter.setSizes([400, 400])
        layout.addWidget(splitter)

        # Status bar
        self.status_label = QLabel("Ready. Configure worker URL in Settings to enable submission.")
        self.status_label.setStyleSheet("border: 1px solid #999; padding: 4px;")
        layout.addWidget(self.status_label)

        self._apply_template("RECEIVE", "PO", "po")

    def showEvent(self, event):
        super().showEvent(event)
        if not self._reasons_loaded:
            self.load_reasons()

    def _d1_client(self):
        if self.d1_client_getter:
            return self.d1_client_getter()
        return None

    def _worker_client(self):
        if self.worker_config_getter:
            url, api_key = self.worker_config_getter()
            if url:
                return InventoryWorkerClient(url, api_key)
        return None

    def _wait_worker(self, name):
        worker = getattr(self, name, None)
        if worker is not None and worker.isRunning():
            worker.wait(3000)

    def _toggle_scan(self, checked):
        self._scan_mode = checked
        self.scan_btn.setText("Scan: ON" if checked else "Scan: OFF")
        self.set_status(f"Scan mode {'ON' if checked else 'OFF'}.")

    def set_status(self, msg):
        self.status_label.setText(msg)

    def _on_search_text_changed(self, text):
        d1 = self._d1_client()
        if not d1:
            return
        q = text.strip()
        if len(q) < 2 and not (q.isdigit() and len(q) > 0):
            self._clear_product_table()
            return
        self._search_timer.start()

    def _do_search(self):
        d1 = self._d1_client()
        q = self.search_edit.text().strip()
        if not d1 or not q:
            return
        self._wait_worker("worker_search")
        self.worker_search = SearchProductsWorker(d1, q)
        self.worker_search.finished.connect(self._apply_products)
        self.worker_search.error.connect(lambda e: self.set_status(f"Product search failed: {e}"))
        self.worker_search.start()

    def _on_search_return(self):
        d1 = self._d1_client()
        if not d1:
            self.set_status("D1 disabled; scan search is unavailable.")
            return
        q = self.search_edit.text().strip()
        if not q:
            return
        self._pending_scan_submit = True
        self._do_search()

    def _apply_products(self, rows):
        self._products = rows
        self.product_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.product_table.setItem(i, 0, QTableWidgetItem(str(r.get("product_id", ""))))
            self.product_table.setItem(i, 1, QTableWidgetItem(str(r.get("upc") or "")))
            self.product_table.setItem(i, 2, QTableWidgetItem(str(r.get("model") or "")))
            self.product_table.setItem(i, 3, QTableWidgetItem(str(r.get("brand") or "")))
            self.product_table.setItem(i, 4, QTableWidgetItem(str(r.get("qty_on_hand", 0))))
            self.product_table.setItem(i, 5, QTableWidgetItem(str(r.get("qty_reserved", 0))))

        if not rows:
            self.product_note.setText("No matches." if self.search_edit.text().strip() else "")
        else:
            self.product_note.setText(f"{len(rows)} match(es).")

        if self._pending_scan_submit:
            self._pending_scan_submit = False
            if len(rows) == 1:
                self._select_product(rows[0])
            elif len(rows) > 1:
                self.product_table.setFocus()
                self.set_status("Multiple matches -- select the correct row.")
            else:
                self.set_status("No matches for scanned value.")

    def _clear_product_table(self):
        self._products = []
        self.product_table.setRowCount(0)
        self.product_note.setText("")

    def _on_product_double_clicked(self, index):
        row = index.row()
        if 0 <= row < len(self._products):
            self._select_product(self._products[row])

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.product_table.hasFocus():
                row = self.product_table.currentRow()
                if 0 <= row < len(self._products):
                    self._select_product(self._products[row])
                    return
        super().keyPressEvent(event)

    def _select_product(self, prod):
        pid = int(prod["product_id"])
        self._selected_product_id = pid
        self._selected_brand = str(prod.get("brand") or "")
        self._selected_model = str(prod.get("model") or "")
        self.product_id_edit.setText(str(pid))
        brand_model = f"{self._selected_brand} {self._selected_model}".strip() or f"product {pid}"
        self.stock_label.setText(
            f"{brand_model}: on_hand={prod.get('qty_on_hand', 0)} "
            f"reserved={prod.get('qty_reserved', 0)} (from search)"
        )
        d1 = self._d1_client()
        if d1:
            self._fetch_stock(pid)
            self._fetch_queue_errors(pid)
        if self._scan_mode:
            self.search_edit.clear()
        self.qty_delta_edit.setFocus()

    def _fetch_stock(self, product_id):
        d1 = self._d1_client()
        if not d1:
            return
        self._wait_worker("worker_stock")
        self.worker_stock = FetchStockWorker(d1, product_id)
        self.worker_stock.finished.connect(self._apply_stock)
        self.worker_stock.error.connect(lambda e: self.set_status(f"Stock lookup failed: {e}"))
        self.worker_stock.start()

    def _apply_stock(self, product_id, sb):
        brand_model = f"{self._selected_brand} {self._selected_model}".strip() or f"product {product_id}"
        if not sb:
            self.stock_label.setText(f"{brand_model}: (no stock_balance row)")
            return
        self.stock_label.setText(
            f"{brand_model}: on_hand={sb.get('qty_on_hand')} "
            f"reserved={sb.get('qty_reserved')} (updated {sb.get('updated_at')})"
        )

    def _fetch_queue_errors(self, product_id):
        d1 = self._d1_client()
        if not d1:
            return
        self._wait_worker("worker_queue_errors")
        self.worker_queue_errors = FetchQueueErrorsWorker(d1, product_id)
        self.worker_queue_errors.finished.connect(self._apply_queue_errors)
        self.worker_queue_errors.error.connect(lambda e: self.set_status(f"Queue error lookup failed: {e}"))
        self.worker_queue_errors.start()

    def _apply_queue_errors(self, product_id, rows):
        if not rows:
            self.queue_error_label.setVisible(False)
            return
        lines = []
        for i, r in enumerate(rows):
            acct = r.get("account_id", "?")
            reason = r.get("reason") or "?"
            err = (r.get("last_error") or "").strip()
            requested = r.get("requested_at") or "?"
            if len(rows) > 1:
                lines.append(f"[{i+1}] account={acct}  reason={reason}  at={requested}")
            else:
                lines.append(f"account={acct}  reason={reason}  at={requested}")
            lines.append(f"    {err}")
        self.queue_error_label.setText("\n".join(lines))
        self.queue_error_label.setVisible(True)

    def load_reasons(self):
        d1 = self._d1_client()
        if not d1:
            self.set_status("D1 disabled; cannot fetch reason codes.")
            return
        self._reasons_loaded = True
        self._wait_worker("worker_reasons")
        self.worker_reasons = LoadReasonsWorker(d1)
        self.worker_reasons.finished.connect(self._apply_reasons)
        self.worker_reasons.error.connect(lambda e: self.set_status(f"Failed to load reason codes: {e}"))
        self.worker_reasons.start()

    def _apply_reasons(self, reasons):
        self._reasons = reasons
        if not reasons:
            self.set_status("No active reason codes returned from D1; using defaults.")

    def _apply_template(self, reason_code, reference_type, ref_prefix, qty_delta=None):
        self._current_reason_code = reason_code
        self._current_reference_type = reference_type
        self._current_ref_prefix = ref_prefix
        self._highlight_template(reason_code)
        if qty_delta is not None:
            self.qty_delta_edit.setText(str(qty_delta))
        self.set_status(f"Template applied: {reference_type} / {reason_code}")
        if self._selected_product_id is None:
            self.search_edit.setFocus()
        else:
            self.qty_delta_edit.setFocus()

    def _highlight_template(self, reason_code):
        active_style = "font-weight: bold; background-color: #c0d8f0;"
        inactive_style = ""
        for code, btn in self._tpl_buttons.items():
            btn.setStyleSheet(active_style if code == reason_code else inactive_style)

    def _on_clear(self):
        self.search_edit.clear()
        self._clear_product_table()
        self._selected_product_id = None
        self._selected_brand = ""
        self._selected_model = ""
        self.product_id_edit.clear()
        self.qty_delta_edit.clear()
        self.notes_edit.clear()
        self.reason_text_edit.clear()
        self.stock_label.setText("--")
        self.queue_error_label.setVisible(False)
        self._current_reason_code = "RECEIVE"
        self._current_reference_type = "PO"
        self._current_ref_prefix = "po"
        self._highlight_template("RECEIVE")
        self.set_status("Cleared.")
        self.search_edit.setFocus()

    def _validate_int(self, text, field_name):
        text = text.strip()
        if not text:
            self.set_status(f"ERROR: {field_name} is required.")
            return None
        try:
            return int(text)
        except ValueError:
            self.set_status(f"ERROR: {field_name} must be a whole number.")
            return None

    def _on_recheck(self):
        """Send a queue-only recalc request — no inventory change."""
        pid = self._validate_int(self.product_id_edit.text(), "product_id")
        if pid is None:
            return
        account_id = self._validate_int(self.account_id_edit.text(), "account_id")
        if account_id is None:
            return

        worker = self._worker_client()
        if not worker:
            self.set_status("ERROR: Worker URL not configured. Set it in Settings.")
            return

        self.set_status("Enqueuing recalc...")
        self.recheck_btn.setEnabled(False)
        self._wait_worker("worker_recalc")
        self.worker_recalc = SubmitRecalcWorker(worker, account_id, pid)
        self.worker_recalc.finished.connect(self._on_recheck_success)
        self.worker_recalc.error.connect(self._on_recheck_error)
        self.worker_recalc.start()

    def _on_recheck_success(self, resp):
        self.recheck_btn.setEnabled(True)
        self.set_status(
            f"Recalc enqueued: account_id={resp.get('account_id')} "
            f"product_id={resp.get('product_id')} reason={resp.get('reason')}"
        )

    def _on_recheck_error(self, msg):
        self.recheck_btn.setEnabled(True)
        self.set_status(f"Recalc enqueue failed: {msg}")

    def _on_submit(self):
        pid = self._validate_int(self.product_id_edit.text(), "product_id")
        if pid is None:
            return
        qty_delta = self._validate_int(self.qty_delta_edit.text(), "qty_delta")
        if qty_delta is None:
            return

        reason_code = self._current_reason_code
        if not reason_code:
            self.set_status("ERROR: reason_code is required.")
            return

        ref_type = self._current_reference_type or "MANUAL"
        ref_id = new_ref_id(self._current_ref_prefix or "manual")
        notes = self.notes_edit.text().strip() or None
        reason_text = self.reason_text_edit.text().strip() or None
        entered_by = guess_entered_by()

        payload = {
            "product_id": int(pid),
            "qty_delta": int(qty_delta),
            "reason_code": reason_code,
            "reference_type": ref_type,
            "reference_id": ref_id,
            "entered_by": entered_by,
        }
        if notes:
            payload["notes"] = notes
        if reason_text:
            payload["reason"] = reason_text

        worker = self._worker_client()
        if not worker:
            self.set_status("ERROR: Worker URL not configured. Set it in Settings.")
            return

        self.set_status("Submitting...")
        self.submit_btn.setEnabled(False)
        self._wait_worker("worker_submit")
        self.worker_submit = SubmitMoveWorker(worker, payload)
        self.worker_submit.finished.connect(self._on_submit_success)
        self.worker_submit.error.connect(self._on_submit_error)
        self.worker_submit.start()

    def _on_submit_success(self, payload, resp):
        self.submit_btn.setEnabled(True)
        applied = bool(resp.get("applied", True))
        lines = [
            "Success",
            f"applied: {applied}",
            f"product_id: {resp.get('product_id')}",
            f"qty_delta: {resp.get('qty_delta')}",
            f"reason_code: {resp.get('reason_code')}",
            f"reference: {resp.get('reference_type')} / {resp.get('reference_id')}",
            f"new_qty_on_hand: {resp.get('new_qty_on_hand')}",
        ]
        if "affected_accounts" in resp:
            lines.append(
                f"affected_accounts: {resp.get('affected_accounts')}  enqueued: {resp.get('enqueued')}"
            )
        if resp.get("ledger"):
            led = resp["ledger"]
            lines.append(f"ledger_id: {led.get('stock_ledger_id')} at {led.get('occurred_at')}")
        self.set_status(" | ".join(lines))

        try:
            pid = int(resp.get("product_id"))
            d1 = self._d1_client()
            if d1 and pid:
                self._fetch_stock(pid)
        except Exception:
            pass

        if self._scan_mode:
            self.qty_delta_edit.clear()
            self.search_edit.clear()
            self._selected_product_id = None
            self.product_id_edit.clear()
            self.search_edit.setFocus()

    def _on_submit_error(self, msg):
        self.submit_btn.setEnabled(True)
        self.set_status(f"Submit failed: {msg}")


# ------------------- Settings Dialog with Table Visibility -------------------
class SettingsDialog(QDialog):
    def __init__(self, parent=None, get_current_tables_func=None):
        super().__init__(parent)
        self.get_current_tables_func = get_current_tables_func  # returns list of all table names from local DB
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # Credentials group
        cred_group = QGroupBox("Cloudflare D1 Credentials")
        form_layout = QFormLayout()
        self.account_id_edit = QLineEdit()
        self.database_id_edit = QLineEdit()
        self.api_token_edit = QLineEdit()
        self.api_token_edit.setEchoMode(QLineEdit.Password)
        form_layout.addRow("Account ID:", self.account_id_edit)
        form_layout.addRow("Database ID:", self.database_id_edit)
        form_layout.addRow("API Token:", self.api_token_edit)
        cred_group.setLayout(form_layout)
        layout.addWidget(cred_group)

        # Worker config group
        worker_group = QGroupBox("Inventory Worker")
        worker_form = QFormLayout()
        self.worker_url_edit = QLineEdit()
        self.worker_url_edit.setPlaceholderText("https://example.workers.dev")
        self.worker_api_key_edit = QLineEdit()
        self.worker_api_key_edit.setEchoMode(QLineEdit.Password)
        worker_form.addRow("Worker URL:", self.worker_url_edit)
        worker_form.addRow("API Key:", self.worker_api_key_edit)
        worker_group.setLayout(worker_form)
        layout.addWidget(worker_group)

        # Table visibility group
        table_group = QGroupBox("Table Visibility (Hide/Unhide tables in Spreadsheet Browser)")
        table_layout = QVBoxLayout()
        self.table_list = QListWidget()
        self.table_list.setSelectionMode(QAbstractItemView.NoSelection)
        table_layout.addWidget(self.table_list)

        btn_layout = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self.select_all_tables)
        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.clicked.connect(self.deselect_all_tables)
        self.refresh_tables_btn = QPushButton("Refresh Table List")
        self.refresh_tables_btn.clicked.connect(self.refresh_table_list)
        btn_layout.addWidget(self.select_all_btn)
        btn_layout.addWidget(self.deselect_all_btn)
        btn_layout.addWidget(self.refresh_tables_btn)
        table_layout.addLayout(btn_layout)

        table_group.setLayout(table_layout)
        layout.addWidget(table_group)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def load_settings(self):
        settings = QSettings("EbayBrowser", "D1Browser")
        self.account_id_edit.setText(settings.value("account_id", "").strip())
        self.database_id_edit.setText(settings.value("database_id", "").strip())
        self.api_token_edit.setText(settings.value("api_token", "").strip())
        self.worker_url_edit.setText(settings.value("inventory_worker_url", "").strip())
        self.worker_api_key_edit.setText(settings.value("inventory_api_key", "").strip())
        # Load hidden tables
        self.hidden_tables = set(settings.value("hidden_tables", []))
        self.refresh_table_list()

    def refresh_table_list(self):
        """Populate table list with checkboxes based on current database tables."""
        self.table_list.clear()
        tables = []
        if self.get_current_tables_func:
            tables = self.get_current_tables_func()
        if not tables:
            # Add a placeholder item
            item = QListWidgetItem("No tables loaded. Please sync data first.")
            item.setFlags(Qt.NoItemFlags)
            self.table_list.addItem(item)
            return

        for table in sorted(tables):
            item = QListWidgetItem(table)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if table not in self.hidden_tables else Qt.Unchecked)
            self.table_list.addItem(item)

    def select_all_tables(self):
        for i in range(self.table_list.count()):
            item = self.table_list.item(i)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Checked)

    def deselect_all_tables(self):
        for i in range(self.table_list.count()):
            item = self.table_list.item(i)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Unchecked)

    def get_credentials(self):
        return (
            self.account_id_edit.text().strip(),
            self.database_id_edit.text().strip(),
            self.api_token_edit.text().strip()
        )

    def get_worker_config(self):
        return (
            self.worker_url_edit.text().strip(),
            self.worker_api_key_edit.text().strip()
        )

    def save_credentials(self):
        settings = QSettings("EbayBrowser", "D1Browser")
        settings.setValue("account_id", self.account_id_edit.text().strip())
        settings.setValue("database_id", self.database_id_edit.text().strip())
        settings.setValue("api_token", self.api_token_edit.text().strip())
        settings.setValue("inventory_worker_url", self.worker_url_edit.text().strip())
        settings.setValue("inventory_api_key", self.worker_api_key_edit.text().strip())
        # Save hidden tables (those unchecked)
        hidden = []
        for i in range(self.table_list.count()):
            item = self.table_list.item(i)
            if item.flags() & Qt.ItemIsUserCheckable and item.checkState() == Qt.Unchecked:
                hidden.append(item.text())
        settings.setValue("hidden_tables", hidden)

# ------------------- Visual Custom Tables Dialog -------------------
class CustomTableDesigner(QDialog):
    def __init__(self, parent=None, existing_name=None, definition=None):
        super().__init__(parent)
        self.parent = parent  # D1BrowserWindow
        self.existing_name = existing_name
        self.definition = definition or {}
        self.selected_columns = []   # list of (table, column, alias)
        self.joined_tables = []      # list of dict: {table, join_type, on_condition}
        self.setWindowTitle("Create/Edit Custom Table")
        self.setMinimumSize(800, 600)
        self.setModal(True)
        self.build_ui()
        if definition:
            self.load_definition(definition)

    def build_ui(self):
        layout = QVBoxLayout(self)
        # Name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Custom table name:"))
        self.name_edit = QLineEdit()
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)

        # Splitter: left = all columns tree, right = selected columns + joins
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # Left: all tables/columns tree
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.addWidget(QLabel("All columns (double-click to add):"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Tables and Columns")
        self.tree.itemDoubleClicked.connect(self.on_tree_double_clicked)   # connect double-click
        left_layout.addWidget(self.tree)

        add_col_btn = QPushButton("➕ Add selected column")
        add_col_btn.clicked.connect(self.add_selected_tree_column)
        left_layout.addWidget(add_col_btn)

        splitter.addWidget(left_widget)

        # Right: selected columns and joins
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        # Selected columns list with reorder
        right_layout.addWidget(QLabel("Selected columns (order determines display order):"))
        self.col_list = QListWidget()
        self.col_list.setDragDropMode(QAbstractItemView.InternalMove)
        right_layout.addWidget(self.col_list)

        col_btn_layout = QHBoxLayout()
        self.remove_col_btn = QPushButton("Remove selected")
        self.remove_col_btn.clicked.connect(self.remove_selected_column)
        self.up_btn = QPushButton("Move Up")
        self.up_btn.clicked.connect(self.move_column_up)
        self.down_btn = QPushButton("Move Down")
        self.down_btn.clicked.connect(self.move_column_down)
        col_btn_layout.addWidget(self.remove_col_btn)
        col_btn_layout.addWidget(self.up_btn)
        col_btn_layout.addWidget(self.down_btn)
        right_layout.addLayout(col_btn_layout)

        # Join configuration group
        join_group = QGroupBox("Join other tables")
        join_layout = QVBoxLayout(join_group)

        self.join_list = QListWidget()
        join_layout.addWidget(self.join_list)

        join_btn_layout = QHBoxLayout()
        self.add_join_btn = QPushButton("Add Join")
        self.add_join_btn.clicked.connect(self.add_join)
        self.remove_join_btn = QPushButton("Remove Join")
        self.remove_join_btn.clicked.connect(self.remove_join)
        join_btn_layout.addWidget(self.add_join_btn)
        join_btn_layout.addWidget(self.remove_join_btn)
        join_layout.addLayout(join_btn_layout)

        right_layout.addWidget(join_group)
        splitter.addWidget(right_widget)
        splitter.setSizes([300, 500])

        # Primary table selector (simplifies joins)
        primary_layout = QHBoxLayout()
        primary_layout.addWidget(QLabel("Primary table (for joins):"))
        self.primary_table_combo = QComboBox()
        self.primary_table_combo.currentTextChanged.connect(self.on_primary_table_changed)
        primary_layout.addWidget(self.primary_table_combo)
        right_layout.addLayout(primary_layout)

        # Preview button
        self.preview_btn = QPushButton("Preview SQL")
        self.preview_btn.clicked.connect(self.preview_sql)
        right_layout.addWidget(self.preview_btn)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.populate_tables_columns()

    def populate_tables_columns(self):
        if not self.parent or not self.parent.local_conn:
            return
        # Get all tables (exclude hidden and internal)
        cursor = self.parent.local_conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [row[0] for row in cursor.fetchall()]
        hidden = self.parent.spreadsheet_tab.get_hidden_tables_func()
        tables = [t for t in tables if t not in hidden]
        self.primary_table_combo.addItems(tables)
        # Tree: each table with its columns
        self.tree.clear()
        for table in tables:
            table_item = QTreeWidgetItem([table])
            table_item.setFlags(table_item.flags() | Qt.ItemIsDragEnabled)
            # Get columns
            cursor.execute(f"PRAGMA table_info({table})")
            cols = cursor.fetchall()
            for col in cols:
                col_name = col[1]
                col_item = QTreeWidgetItem([col_name])
                col_item.setFlags(col_item.flags() | Qt.ItemIsDragEnabled)
                table_item.addChild(col_item)
            self.tree.addTopLevelItem(table_item)
        self.tree.expandAll()

    def on_primary_table_changed(self):
        # Update join possibilities (disable joins not referencing primary)
        pass

    def add_join(self):
        if not self.primary_table_combo.currentText():
            QMessageBox.warning(self, "No primary table", "Please select a primary table first.")
            return
        # Simple join dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Join")
        layout = QFormLayout(dialog)
        table_combo = QComboBox()
        # Get all tables except primary
        all_tables = [self.primary_table_combo.itemText(i) for i in range(self.primary_table_combo.count())]
        other_tables = [t for t in all_tables if t != self.primary_table_combo.currentText()]
        table_combo.addItems(other_tables)
        layout.addRow("Join table:", table_combo)

        join_type_combo = QComboBox()
        join_type_combo.addItems(["LEFT JOIN", "INNER JOIN"])
        layout.addRow("Join type:", join_type_combo)

        # Suggest possible join conditions based on foreign keys
        fk_relations = self.parent.get_foreign_key_relations()
        condition_combo = QComboBox()
        # Add custom edit line for manual condition
        condition_combo.setEditable(True)
        layout.addRow("ON condition (tableA.col = tableB.col):", condition_combo)

        def update_conditions():
            condition_combo.clear()
            primary = self.primary_table_combo.currentText()
            join_table = table_combo.currentText()
            # Look for FKs from primary to join_table
            for fk in fk_relations.get(primary, []):
                if fk[1] == join_table:
                    condition_combo.addItem(f"{primary}.{fk[0]} = {join_table}.{fk[2]}")
            # Look for FKs from join_table to primary
            for fk in fk_relations.get(join_table, []):
                if fk[1] == primary:
                    condition_combo.addItem(f"{join_table}.{fk[0]} = {primary}.{fk[2]}")
            condition_combo.addItem("(enter custom condition)")

        table_combo.currentTextChanged.connect(update_conditions)
        update_conditions()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec():
            join_table = table_combo.currentText()
            join_type = join_type_combo.currentText()
            condition = condition_combo.currentText()
            if condition == "(enter custom condition)":
                condition, ok = QInputDialog.getText(self, "Custom Condition", "Enter ON condition (e.g., listings.product_id = products.product_id):")
                if not ok or not condition:
                    return
            self.joined_tables.append({
                "table": join_table,
                "type": join_type,
                "on": condition
            })
            self.refresh_join_list()

    def remove_join(self):
        row = self.join_list.currentRow()
        if row >= 0:
            del self.joined_tables[row]
            self.refresh_join_list()

    def refresh_join_list(self):
        self.join_list.clear()
        for j in self.joined_tables:
            self.join_list.addItem(f"{j['type']} {j['table']} ON {j['on']}")

    def add_selected_column(self, item):
        if not item.parent():
            return  # it's a table
        table = item.parent().text(0)
        column = item.text(0)
        # Check if already selected
        for (t, c, alias) in self.selected_columns:
            if t == table and c == column:
                return
        self.selected_columns.append((table, column, f"{table}_{column}"))
        self.refresh_column_list()

    def remove_selected_column(self):
        row = self.col_list.currentRow()
        if row >= 0:
            del self.selected_columns[row]
            self.refresh_column_list()

    def move_column_up(self):
        row = self.col_list.currentRow()
        if row > 0:
            self.selected_columns[row], self.selected_columns[row-1] = self.selected_columns[row-1], self.selected_columns[row]
            self.refresh_column_list()
            self.col_list.setCurrentRow(row-1)

    def move_column_down(self):
        row = self.col_list.currentRow()
        if row >= 0 and row < len(self.selected_columns)-1:
            self.selected_columns[row], self.selected_columns[row+1] = self.selected_columns[row+1], self.selected_columns[row]
            self.refresh_column_list()
            self.col_list.setCurrentRow(row+1)

    def refresh_column_list(self):
        self.col_list.clear()
        for table, col, alias in self.selected_columns:
            self.col_list.addItem(f"{table}.{col} AS {alias}")

    def preview_sql(self):
        if not self.selected_columns:
            QMessageBox.warning(self, "No columns", "Please select at least one column.")
            return
        primary = self.primary_table_combo.currentText()
        if not primary:
            QMessageBox.warning(self, "No primary table", "Please select a primary table.")
            return

        # Auto-infer joins for tables referenced in columns but not joined
        fk_relations = {}
        if self.parent and hasattr(self.parent, 'get_foreign_key_relations'):
            fk_relations = self.parent.get_foreign_key_relations()
        joins, missing = self.parent._auto_infer_joins(
            primary, self.selected_columns, self.joined_tables, fk_relations
        ) if hasattr(self.parent, '_auto_infer_joins') else (self.joined_tables, set())
        if missing:
            QMessageBox.warning(self, "Missing Joins",
                f"Table(s) {missing} are referenced in columns but have no join.\n"
                "Please add joins or remove those columns.")

        cols_sql = ",\n    ".join([f"[{t}].[{c}] AS [{a}]" for t, c, a in self.selected_columns])
        sql = f"SELECT\n    {cols_sql}\nFROM [{primary}]"
        for join in joins:
            sql += f"\n{join['type']} [{join['table']}] ON {join['on']}"
        # Show preview dialog
        preview = QDialog(self)
        preview.setWindowTitle("SQL Preview")
        layout = QVBoxLayout(preview)
        edit = QTextEdit()
        edit.setPlainText(sql)
        edit.setFont(QFont("Courier New", 10))
        layout.addWidget(edit)
        btn = QPushButton("Close")
        btn.clicked.connect(preview.accept)
        layout.addWidget(btn)
        preview.exec()

    def get_definition(self):
        return {
            "name": self.name_edit.text().strip(),
            "primary_table": self.primary_table_combo.currentText(),
            "columns": self.selected_columns,  # list of (table, column, alias)
            "joins": self.joined_tables
        }

    def load_definition(self, defn):
        self.name_edit.setText(defn.get("name", ""))
        primary = defn.get("primary_table", "")
        idx = self.primary_table_combo.findText(primary)
        if idx >= 0:
            self.primary_table_combo.setCurrentIndex(idx)
        self.selected_columns = defn.get("columns", [])
        self.joined_tables = defn.get("joins", [])
        self.refresh_column_list()
        self.refresh_join_list()

    def on_tree_double_clicked(self, item, column):
        """Handle double-click on tree item: add column if it's a leaf."""
        self.add_selected_tree_column(item)

    def add_selected_tree_column(self, current_item=None):
        """Add the currently selected column in the tree to the selected columns list."""
        if current_item is None:
            current_item = self.tree.currentItem()
        if not current_item:
            QMessageBox.information(self, "No selection", "Please select a column in the tree first.")
            return
        # Only add if it's a column (has a parent)
        if not current_item.parent():
            QMessageBox.information(self, "Not a column", "Please select a column (child of a table).")
            return
        table = current_item.parent().text(0)
        column = current_item.text(0)
        # Avoid duplicates
        for (t, c, a) in self.selected_columns:
            if t == table and c == column:
                QMessageBox.information(self, "Duplicate", f"Column {table}.{column} already added.")
                return
        # Generate an alias (use table_column to avoid name clashes)
        alias = f"{table}_{column}"
        self.selected_columns.append((table, column, alias))
        self.refresh_column_list()

# ------------------- Main Window -------------------
class D1BrowserWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.client = None
        self.local_conn = None
        self.setup_ui()
        self.setup_menu()
        self.load_settings_and_connect()

    def setup_ui(self):
        self.setWindowTitle("eBay D1 Browser - SQL + Spreadsheet")
        self.setMinimumSize(1200, 800)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Top bar
        top = QHBoxLayout()
        self.sync_btn = QPushButton("Sync from Cloudflare")
        self.sync_btn.clicked.connect(self.load_all_data)
        self.backup_btn = QPushButton("Create Backup")
        self.backup_btn.clicked.connect(self.create_backup)
        self.backup_btn.setEnabled(False)
        self.restore_btn = QPushButton("Load Backup")
        self.restore_btn.clicked.connect(self.load_backup)
        self.restore_btn.setEnabled(False)
        self.status_label = QLabel("Not connected")
        top.addWidget(self.sync_btn)
        top.addWidget(self.backup_btn)
        top.addWidget(self.restore_btn)
        top.addStretch()
        top.addWidget(self.status_label)
        main_layout.addLayout(top)

        # Tab widget
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Create tabs (pass connection getter and a function to get hidden tables)
        self.inventory_tab = InventoryTab(
            d1_client_getter=lambda: self.client,
            worker_config_getter=lambda: (
                QSettings("EbayBrowser", "D1Browser").value("inventory_worker_url", ""),
                QSettings("EbayBrowser", "D1Browser").value("inventory_api_key", "")
            )
        )
        self.spreadsheet_tab = SpreadsheetBrowser(
            lambda: self.local_conn,
            lambda: set(QSettings("EbayBrowser", "D1Browser").value("hidden_tables", [])),
            client_getter=lambda: self.client
        )
        self.sql_tab = SQLQueryTab(lambda: self.local_conn)
        self.tabs.addTab(self.inventory_tab, "Inventory")
        self.tabs.addTab(self.spreadsheet_tab, "Spreadsheet Browser")
        self.import_tab = ImportEbayTab(d1_client_getter=lambda: self.client)
        self.tabs.addTab(self.sql_tab, "SQL Query")
        self.tabs.addTab(self.import_tab, "Import eBay Listings")
        # Default to inventory tab (first tab)
        self.tabs.setCurrentIndex(0)

    def setup_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        export_action = QAction("Export Settings...", self)
        export_action.triggered.connect(self.export_settings)
        file_menu.addAction(export_action)
        import_action = QAction("Import Settings...", self)
        import_action.triggered.connect(self.import_settings)
        file_menu.addAction(import_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Custom Tables menu
        custom_menu = menubar.addMenu("Custom Tables")
        create_action = QAction("Create Custom Table", self)
        create_action.triggered.connect(self.create_custom_table)
        edit_action = QAction("Edit Custom Table", self)
        edit_action.triggered.connect(self.edit_custom_table)
        delete_action = QAction("Delete Custom Table", self)
        delete_action.triggered.connect(self.delete_custom_table)
        custom_menu.addAction(create_action)
        custom_menu.addAction(edit_action)
        custom_menu.addAction(delete_action)

    def load_settings_and_connect(self):
        settings = QSettings("EbayBrowser", "D1Browser")
        account_id = settings.value("account_id", "")
        database_id = settings.value("database_id", "")
        api_token = settings.value("api_token", "")

        if not account_id or not database_id or not api_token:
            self.status_label.setText("⚠️ Credentials not set. Please configure.")
            self.sync_btn.setEnabled(False)
            self.backup_btn.setEnabled(False)
            self.restore_btn.setEnabled(False)
            self.open_settings()
        else:
            self.client = CloudflareD1Client(account_id, database_id, api_token)
            self.sync_btn.setEnabled(True)
            self.backup_btn.setEnabled(True)
            self.restore_btn.setEnabled(True)
            self.load_all_data()

    def open_settings(self):
        # Provide function to get current tables from local_conn (if loaded)
        def get_current_tables():
            if self.local_conn:
                cursor = self.local_conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                return [row[0] for row in cursor.fetchall()]
            return []
        dialog = SettingsDialog(self, get_current_tables)
        if dialog.exec():
            dialog.save_credentials()
            # Reinitialize client (user will sync manually)
            account_id, database_id, api_token = dialog.get_credentials()
            self.client = CloudflareD1Client(account_id, database_id, api_token)
            self.sync_btn.setEnabled(True)
            self.backup_btn.setEnabled(True)
            self.restore_btn.setEnabled(True)
            self.status_label.setText("Credentials saved. Click Sync to load data.")

    def _all_settings_keys(self):
        return [
            "account_id", "database_id", "api_token",
            "inventory_worker_url", "inventory_api_key",
            "hidden_tables", "format_rules_all", "custom_tables",
        ]

    def export_settings(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Settings", "d1browser_settings.json", "JSON (*.json)"
        )
        if not path:
            return
        settings = QSettings("EbayBrowser", "D1Browser")
        data = {}
        for key in self._all_settings_keys():
            val = settings.value(key)
            if key == "format_rules_all":
                try:
                    val = json.loads(val) if val else {}
                except (json.JSONDecodeError, TypeError):
                    val = val or {}
            elif key == "custom_tables":
                # Stored as raw dict; if somehow a string, try to parse
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        val = {}
                val = val or {}
            elif key == "hidden_tables":
                val = val or []
            data[key] = val
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            QMessageBox.information(self, "Exported", f"Settings saved to {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def import_settings(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Settings", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Import Failed", f"Could not read file:\n{e}")
            return
        if not isinstance(data, dict):
            QMessageBox.critical(self, "Import Failed", "Invalid format: expected a JSON object.")
            return

        reply = QMessageBox.question(
            self, "Confirm Import",
            "This will overwrite all current settings (credentials, worker config, hidden tables, "
            "format rules, custom tables). Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        settings = QSettings("EbayBrowser", "D1Browser")
        for key in self._all_settings_keys():
            if key in data:
                val = data[key]
                if key == "format_rules_all":
                    val = json.dumps(val) if isinstance(val, (dict, list)) else val
                # custom_tables stays as a raw dict (matches save_custom_table_definitions)
                settings.setValue(key, val)

        settings.sync()
        # Reinitialize client from imported credentials
        account_id = settings.value("account_id", "")
        database_id = settings.value("database_id", "")
        api_token = settings.value("api_token", "")
        if account_id and database_id and api_token:
            self.client = CloudflareD1Client(account_id, database_id, api_token)
            self.sync_btn.setEnabled(True)
            self.backup_btn.setEnabled(True)
            self.restore_btn.setEnabled(True)
            self.status_label.setText("Settings imported. Click Sync to load data.")
        else:
            self.client = None
            self.sync_btn.setEnabled(False)
            self.backup_btn.setEnabled(False)
            self.restore_btn.setEnabled(False)
            self.status_label.setText("Settings imported (no D1 credentials in file).")
        self.spreadsheet_tab._load_format_rules()
        self.spreadsheet_tab.refresh_table_list()
        if self.spreadsheet_tab.current_table:
            self.spreadsheet_tab.update_table_view()
        QMessageBox.information(self, "Imported", "Settings imported.")

    def create_backup(self):
        if not self.client:
            QMessageBox.warning(self, "No Credentials", "Please set Cloudflare credentials in Settings.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Create Backup", "d1_backup.json", "JSON (*.json)"
        )
        if not path:
            return
        self.progress_dialog = QProgressDialog("Creating backup...", "Cancel", 0, 0, self)
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.show()

        self.backup_btn.setEnabled(False)
        self.restore_btn.setEnabled(False)
        self.status_label.setText("Creating backup...")

        self.backup_worker = BackupWorker(self.client, path)
        self.backup_worker.progress.connect(self._on_backup_progress)
        self.backup_worker.finished.connect(self._on_backup_done)
        self.backup_worker.error.connect(self._on_backup_error)
        self.backup_worker.start()

    def _on_backup_progress(self, current, total, message):
        if self.progress_dialog:
            self.progress_dialog.setLabelText(message)

    def _on_backup_done(self, filepath):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None
        self.backup_btn.setEnabled(True)
        self.restore_btn.setEnabled(True)
        self.status_label.setText(f"Backup saved to {os.path.basename(filepath)}")
        QMessageBox.information(self, "Backup Complete",
                                f"Database backup saved to:\n{filepath}")

    def _on_backup_error(self, msg):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None
        self.backup_btn.setEnabled(True)
        self.restore_btn.setEnabled(True)
        self.status_label.setText("Backup failed")
        QMessageBox.critical(self, "Backup Failed", msg)

    def load_backup(self):
        if not self.client:
            QMessageBox.warning(self, "No Credentials", "Please set Cloudflare credentials in Settings.")
            return
        reply = QMessageBox.warning(
            self, "Confirm Restore",
            "This will DROP ALL existing tables and replace them with the backup data.\n"
            "This cannot be undone. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Backup", "", "JSON (*.json)"
        )
        if not path:
            return
        self.backup_btn.setEnabled(False)
        self.restore_btn.setEnabled(False)
        self.status_label.setText("Restoring backup...")

        self.restore_worker = RestoreWorker(self.client, path)
        self.restore_worker.progress.connect(self._on_restore_progress)
        self.restore_worker.finished.connect(self._on_restore_done)
        self.restore_worker.error.connect(self._on_restore_error)
        self.restore_worker.start()

    def _on_restore_progress(self, current, total, message):
        if not hasattr(self, "progress_dialog") or not self.progress_dialog:
            self.progress_dialog = QProgressDialog("Restoring backup...", "Cancel", 0, total, self)
            self.progress_dialog.setWindowModality(Qt.WindowModal)
            self.progress_dialog.canceled.connect(self._on_restore_cancel)
            self.progress_dialog.show()
        self.progress_dialog.setMaximum(total)
        self.progress_dialog.setValue(current)
        self.progress_dialog.setLabelText(message)

    def _on_restore_cancel(self):
        if hasattr(self, "restore_worker") and self.restore_worker.isRunning():
            self.restore_worker.cancel()

    def _on_restore_done(self, stats):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None
        self.backup_btn.setEnabled(True)
        self.restore_btn.setEnabled(True)
        msg = (f"Restore complete.\n"
               f"Tables dropped: {stats.get('dropped', 0)}\n"
               f"Tables created: {stats.get('created', 0)}\n"
               f"Rows inserted: {stats.get('rows_inserted', 0)}\n"
               f"Errors: {stats.get('errors', 0)}")
        errors_list = stats.get("errors_list", [])
        if errors_list:
            msg += "\n\nError details:\n" + "\n".join(f"  - {e}" for e in errors_list)
        self.status_label.setText("Restore complete. Click Sync to reload local data.")
        QMessageBox.information(self, "Restore Complete", msg)

    def _on_restore_error(self, msg):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None
        self.backup_btn.setEnabled(True)
        self.restore_btn.setEnabled(True)
        self.status_label.setText("Restore failed")
        QMessageBox.critical(self, "Restore Failed", msg)

    def load_all_data(self):
        if not self.client:
            QMessageBox.warning(self, "No Credentials", "Please set Cloudflare credentials in Settings.")
            return

        self.progress_dialog = QProgressDialog("Fetching data from Cloudflare D1...", "Cancel", 0, 0, self)
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.show()

        self.sync_btn.setEnabled(False)
        self.status_label.setText("⏳ Loading data from Cloudflare...")

        self.worker = LoadDataWorker(self.client)
        # Use a normal connection (auto). No partial, no extra arguments.
        self.worker.finished.connect(self.on_data_loaded)
        self.worker.start()

    def on_data_loaded(self, table_data, error):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None

        self.sync_btn.setEnabled(True)
        if error:
            self.status_label.setText("❌ Load failed")
            QMessageBox.critical(self, "Error", f"Failed to load data:\n{error}")
            return

        # Create in-memory database
        self.local_conn = sqlite3.connect(":memory:")
        for table, df in table_data.items():
            try:
                df = clean_dataframe_columns(df)
                # Skip if DataFrame has no columns after sanitizing
                if df.empty and len(df.columns) == 0:
                    print(f"Table {table} has no columns, skipping")
                    continue
                df.to_sql(table, self.local_conn, if_exists="replace", index=False)
            except Exception as e:
                print(f"Failed to save table {table}: {e}")
                continue
            
        self.recreate_custom_tables()
            
        # Count rows from first table for status
        cursor = self.local_conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        if tables:
            first_table = tables[0][0]
            cursor.execute(f"SELECT COUNT(*) FROM [{first_table}]")
            count = cursor.fetchone()[0]
            self.status_label.setText(f"✅ Loaded {count} rows across {len(table_data)} tables")
        else:
            self.status_label.setText("✅ Loaded, but no tables found")

        # Refresh the spreadsheet browser's table list
        self.spreadsheet_tab.refresh_table_list()
        if self.spreadsheet_tab.table_combo.count() > 0:
            self.spreadsheet_tab.table_combo.setCurrentIndex(0)

        self.sql_tab.reset_to_full()

    def save_custom_table_definitions(self, definitions):
        """definitions: dict {name: sql_query}"""
        settings = QSettings("EbayBrowser", "D1Browser")
        settings.setValue("custom_tables", definitions)

    def load_custom_table_definitions(self):
        settings = QSettings("EbayBrowser", "D1Browser")
        val = settings.value("custom_tables", {})
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                val = {}
        return val if isinstance(val, dict) else {}

    def open_custom_table_dialog(self):
        definitions = self.load_custom_table_definitions()
        dialog = CustomTableDialog(self, list(definitions.keys()))
        if dialog.exec():
            name, sql = dialog.get_definition()
            if not name or not sql:
                QMessageBox.warning(self, "Incomplete", "Name and SQL are required.")
                return
            if name in definitions and name not in [dialog.existing_names]:
                # editing existing – we'll overwrite
                pass
            definitions[name] = sql
            self.save_custom_table_definitions(definitions)
            QMessageBox.information(self, "Saved", f"Custom table '{name}' saved. It will be recreated on next sync.")
            # Optionally recreate now if data is loaded
            if self.local_conn:
                self.recreate_custom_tables()
                
    def _auto_infer_joins(self, primary, columns, existing_joins, fk_relations):
        """Return complete join list, filling in missing tables from FK relations."""
        referenced_tables = set(t for t, c, a in columns)
        covered_tables = {primary}
        joins = list(existing_joins)
        for j in joins:
            covered_tables.add(j['table'])

        for table in referenced_tables:
            if table in covered_tables:
                continue
            # Try FK from primary -> table
            for fk in fk_relations.get(primary, []):
                if fk[1] == table:
                    joins.append({
                        "table": table,
                        "type": "LEFT JOIN",
                        "on": f"[{primary}].[{fk[0]}] = [{table}].[{fk[2]}]"
                    })
                    covered_tables.add(table)
                    break
            else:
                # Try FK from table -> primary
                for fk in fk_relations.get(table, []):
                    if fk[1] == primary:
                        joins.append({
                            "table": table,
                            "type": "LEFT JOIN",
                            "on": f"[{table}].[{fk[0]}] = [{primary}].[{fk[2]}]"
                        })
                        covered_tables.add(table)
                        break

        return joins, referenced_tables - covered_tables

    def recreate_custom_tables(self):
        if not self.local_conn:
            return
        definitions = self.load_custom_table_definitions()
        cursor = self.local_conn.cursor()
        fk_relations = self.get_foreign_key_relations()
        for name, defn in definitions.items():
            try:
                # Build SQL from definition
                cols = defn.get("columns", [])
                if not cols:
                    continue
                primary = defn.get("primary_table", "")
                if not primary:
                    continue

                joins, missing = self._auto_infer_joins(
                    primary, cols, defn.get("joins", []), fk_relations
                )
                if missing:
                    print(f"Warning: custom table '{name}' references tables {missing} "
                          f"but no FK relationship found to auto-join them")

                col_parts = [f"[{t}].[{c}] AS [{a}]" for t, c, a in cols]
                cols_sql = ",\n    ".join(col_parts)
                sql = f"SELECT\n    {cols_sql}\nFROM [{primary}]"
                for join in joins:
                    sql += f"\n{join['type']} [{join['table']}] ON {join['on']}"
                # Drop and recreate view
                cursor.execute(f"DROP VIEW IF EXISTS [{name}]")
                cursor.execute(f"CREATE VIEW [{name}] AS {sql}")
                self.local_conn.commit()
            except Exception as e:
                print(f"Failed to create custom view {name}: {e}")
        self.spreadsheet_tab.refresh_table_list()

    def get_foreign_key_relations(self):
        """Return dict: {table_name: [(column, foreign_table, foreign_column), ...]}

        Uses PRAGMA foreign_key_list plus heuristic detection based on column naming
        conventions (_id suffix matching a target table), since pandas.to_sql
        does not preserve explicit FK constraints.
        """
        if not self.local_conn:
            return {}
        relations = {}
        cursor = self.local_conn.cursor()

        # Get all tables and their columns
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        table_columns = {}
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            table_columns[table] = [row[1] for row in cursor.fetchall()]

        for table in tables:
            fk_list = []
            # 1) Explicit FKs from PRAGMA
            cursor.execute(f"PRAGMA foreign_key_list({table})")
            for fk in cursor.fetchall():
                # fk = (id, seq, table, from, to, on_update, on_delete, match)
                fk_list.append((fk[3], fk[2], fk[4]))  # (column, foreign_table, foreign_column)

            # 2) Heuristic: columns ending in _id that match a known table
            for col in table_columns.get(table, []):
                if not col.endswith('_id') or col == 'id':
                    continue
                # Derive target table name: product_id -> products, listing_id -> listings
                prefix = col[:-3]  # strip '_id'
                # Try plural forms first
                candidates = [prefix + 's', prefix + 'es', prefix]
                for target in candidates:
                    if target in table_columns and target != table:
                        # Check that target has a matching column (product_id or id)
                        if col in table_columns[target]:
                            fk_list.append((col, target, col))
                            break
                        if 'id' in table_columns[target] or prefix + '_id' == col:
                            # target has id column, use it
                            target_col = col if col in table_columns[target] else 'id'
                            # Avoid duplicates
                            if not any(f[1] == target for f in fk_list):
                                fk_list.append((col, target, target_col))
                            break

            if fk_list:
                # Deduplicate
                seen = set()
                unique = []
                for f in fk_list:
                    key = (f[0], f[1])
                    if key not in seen:
                        seen.add(key)
                        unique.append(f)
                relations[table] = unique

        return relations

    def open_custom_table_designer(self, existing_name=None):
        definitions = self.load_custom_table_definitions()
        definition = definitions.get(existing_name, {}) if existing_name else {}
        dialog = CustomTableDesigner(self, existing_name, definition)
        if dialog.exec():
            new_def = dialog.get_definition()
            name = new_def["name"]
            if not name:
                QMessageBox.warning(self, "Missing name", "Please enter a name.")
                return
            definitions[name] = new_def
            self.save_custom_table_definitions(definitions)
            if self.local_conn:
                self.recreate_custom_tables()
            QMessageBox.information(self, "Saved", f"Custom table '{name}' saved.")

    def create_custom_table(self):
        self.open_custom_table_designer()

    def edit_custom_table(self):
        definitions = self.load_custom_table_definitions()
        if not definitions:
            QMessageBox.information(self, "No Tables", "No custom tables to edit.")
            return
        names = list(definitions.keys())
        name, ok = QInputDialog.getItem(self, "Edit Custom Table", "Select table:", names, 0, False)
        if ok and name:
            self.open_custom_table_designer(name)

    def delete_custom_table(self):
        # same as before
        definitions = self.load_custom_table_definitions()
        if not definitions:
            QMessageBox.information(self, "No Tables", "No custom tables defined.")
            return
        names = list(definitions.keys())
        name, ok = QInputDialog.getItem(self, "Delete Custom Table", "Select table to delete:", names, 0, False)
        if ok and name:
            del definitions[name]
            self.save_custom_table_definitions(definitions)
            if self.local_conn:
                try:
                    self.local_conn.execute(f"DROP VIEW IF EXISTS [{name}]")
                    self.local_conn.commit()
                except:
                    pass
            self.spreadsheet_tab.refresh_table_list()
            QMessageBox.information(self, "Deleted", f"Custom table '{name}' deleted.")

# ------------------- Main Entry Point -------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setOrganizationName("EbayBrowser")
    app.setApplicationName("D1Browser")
    window = D1BrowserWindow()
    window.show()
    sys.exit(app.exec())