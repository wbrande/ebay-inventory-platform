from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtCore import QUrl

from ebay_comp_monitor.models import CheckState, CompareResult, ProductRow


class ProductTableModel(QAbstractTableModel):
    HEADERS = [
        "Product ID",
        "Brand",
        "Model",
        "UPC",
        "Your Price",
        "Competitor Total",
        "Competitor Seller",
        "Item Price",
        "Shipping",
        "Delta",
        "Status",
        "Match Method",
        "Listing",
        "Matches",
        "Notes",
    ]

    def __init__(self, rows: list[ProductRow], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rows = rows
        self.product_id_to_row = {row.product_id: index for index, row in enumerate(rows)}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            return self._display_value(row, column)
        if role == Qt.ToolTipRole:
            return self._tooltip_value(row, column)
        if role == Qt.ForegroundRole and (
            (column == 12 and row.competitor_url) or
            (column == 13 and row.top_matches)
        ):
            return QColor("#1a73e8")
        if role == Qt.BackgroundRole:
            return self._background(row)
        if role == Qt.TextAlignmentRole and column in {0, 4, 5, 7, 8, 9}:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def update_result(self, result: CompareResult) -> None:
        row_number = self.product_id_to_row[result.product_id]
        row = self.rows[row_number]
        row.status = result.status
        row.match_method = result.match_method
        row.notes = result.notes
        row.top_matches = [listing.raw for listing in result.top_matches]

        if result.best_match:
            listing = result.best_match
            row.competitor_total_price = listing.total_price
            row.competitor_item_price = listing.item_price
            row.competitor_shipping_price = listing.shipping_price
            row.competitor_seller = listing.seller_display or listing.seller_key
            row.competitor_title = listing.title
            row.competitor_url = listing.item_url
            row.delta_vs_you = _delta(row.your_price, listing.total_price)
        else:
            row.competitor_total_price = None
            row.competitor_item_price = None
            row.competitor_shipping_price = None
            row.competitor_seller = None
            row.competitor_title = None
            row.competitor_url = None
            row.delta_vs_you = None

        top_left = self.index(row_number, 0)
        bottom_right = self.index(row_number, self.columnCount() - 1)
        self.dataChanged.emit(top_left, bottom_right)

    def set_status(self, product_id: int, status: CheckState, notes: str | None = None) -> None:
        row_number = self.product_id_to_row[product_id]
        row = self.rows[row_number]
        row.status = status
        if notes is not None:
            row.notes = notes
        top_left = self.index(row_number, 0)
        bottom_right = self.index(row_number, self.columnCount() - 1)
        self.dataChanged.emit(top_left, bottom_right)

    def open_listing(self, row_number: int) -> None:
        row = self.rows[row_number]
        if row.competitor_url:
            QDesktopServices.openUrl(QUrl(row.competitor_url))

    def _display_value(self, row: ProductRow, column: int) -> Any:
        mapping = {
            0: row.product_id,
            1: row.brand or "",
            2: row.model or "",
            3: row.upc or "",
            4: _money(row.your_price),
            5: _money(row.competitor_total_price),
            6: row.competitor_seller or "",
            7: _money(row.competitor_item_price),
            8: _money(row.competitor_shipping_price),
            9: _money(row.delta_vs_you),
            10: row.status.value,
            11: row.match_method or "",
            12: "Open" if row.competitor_url else "",
            13: f"View ({len(row.top_matches)})" if row.top_matches else "",
            14: row.notes or "",
        }
        return mapping.get(column, "")

    def _tooltip_value(self, row: ProductRow, column: int) -> Any:
        if column == 12 and row.competitor_url:
            return row.competitor_url
        if column == 13 and row.top_matches:
            return f"{len(row.top_matches)} competitor match(es) available"
        if row.competitor_title and column in {5, 6, 12}:
            return row.competitor_title
        if row.notes:
            return row.notes
        return None

    def _background(self, row: ProductRow) -> QColor | None:
        if row.status == CheckState.ERROR:
            return QColor("#eb2c2c")
        if row.status == CheckState.NO_MATCH:
            return QColor("#fff7db")
        if row.delta_vs_you is None:
            return None
        if row.delta_vs_you > 0:
            return QColor("#ffe5e5")
        return QColor("#e8f5e9")


def _money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.2f}"


def _delta(your_price: float | None, competitor_total: float | None) -> float | None:
    if your_price is None or competitor_total is None:
        return None
    return your_price - competitor_total
