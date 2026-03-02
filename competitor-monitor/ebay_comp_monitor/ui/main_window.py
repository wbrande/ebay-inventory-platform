from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QSortFilterProxyModel, QThreadPool

from ebay_comp_monitor.config import Settings
from ebay_comp_monitor.models import CheckState
from ebay_comp_monitor.services.compare_service import CompareService
from ebay_comp_monitor.ui.product_table_model import ProductTableModel
from ebay_comp_monitor.workers.price_check_worker import PriceCheckWorker, build_error_result


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: Settings,
        model: ProductTableModel,
        compare_service: CompareService,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.source_model = model
        self.compare_service = compare_service
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(settings.max_concurrent_checks)
        self.queued_product_ids: set[int] = set()

        self.setWindowTitle("eBay Competitor Monitor")
        self.resize(1500, 800)

        self.proxy_model = QSortFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.source_model)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy_model.setFilterKeyColumn(-1)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter products, sellers, notes...")
        self.search_box.textChanged.connect(self.proxy_model.setFilterFixedString)

        self.concurrency_box = QSpinBox()
        self.concurrency_box.setRange(1, 32)
        self.concurrency_box.setValue(settings.max_concurrent_checks)
        self.concurrency_box.valueChanged.connect(self._on_concurrency_changed)

        self.refresh_visible_button = QPushButton("Refresh Visible")
        self.refresh_selected_button = QPushButton("Refresh Selected")
        self.refresh_all_button = QPushButton("Refresh All")
        self.refresh_visible_button.clicked.connect(self.refresh_visible_rows)
        self.refresh_selected_button.clicked.connect(self.refresh_selected_rows)
        self.refresh_all_button.clicked.connect(self.refresh_all_rows)

        self.table = QTableView()
        self.table.setModel(self.proxy_model)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Search:"))
        controls.addWidget(self.search_box, stretch=1)
        controls.addWidget(QLabel("Concurrency:"))
        controls.addWidget(self.concurrency_box)
        controls.addWidget(self.refresh_visible_button)
        controls.addWidget(self.refresh_selected_button)
        controls.addWidget(self.refresh_all_button)

        layout = QVBoxLayout()
        layout.addLayout(controls)
        layout.addWidget(self.table)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

        self._resize_columns()

    def start_initial_refresh(self) -> None:
        initial_count = min(self.settings.initial_visible_batch_size, self.proxy_model.rowCount())
        proxy_rows = list(range(initial_count))
        self._queue_proxy_rows(proxy_rows)

    def refresh_visible_rows(self) -> None:
        first_visible = self.table.rowAt(0)
        last_visible = self.table.rowAt(self.table.viewport().height() - 1)
        if first_visible < 0:
            first_visible = 0
        if last_visible < 0:
            last_visible = min(self.proxy_model.rowCount() - 1, self.settings.initial_visible_batch_size - 1)
        self._queue_proxy_rows(list(range(first_visible, last_visible + 1)))

    def refresh_selected_rows(self) -> None:
        selected_rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not selected_rows:
            QMessageBox.information(self, "No selection", "Select one or more rows first.")
            return
        self._queue_proxy_rows(selected_rows)

    def refresh_all_rows(self) -> None:
        self._queue_proxy_rows(list(range(self.proxy_model.rowCount())))

    def _queue_proxy_rows(self, proxy_rows: list[int]) -> None:
        scheduled = 0
        for proxy_row in proxy_rows:
            if proxy_row < 0 or proxy_row >= self.proxy_model.rowCount():
                continue
            source_row = self.proxy_model.mapToSource(self.proxy_model.index(proxy_row, 0)).row()
            product = self.source_model.rows[source_row]
            if product.product_id in self.queued_product_ids:
                continue
            self.queued_product_ids.add(product.product_id)
            self.source_model.set_status(product.product_id, CheckState.QUEUED, "Waiting for eBay check...")
            worker = PriceCheckWorker(product, self.compare_service)
            worker.signals.finished.connect(self._on_worker_finished)
            worker.signals.failed.connect(self._on_worker_failed)
            self.thread_pool.start(worker)
            scheduled += 1
        self.status.showMessage(f"Queued {scheduled} product(s) for refresh.")

    def _on_worker_finished(self, result) -> None:
        self.queued_product_ids.discard(result.product_id)
        self.source_model.update_result(result)
        self.status.showMessage(f"Updated product {result.product_id}: {result.status.value}")

    def _on_worker_failed(self, product_id: int, message: str) -> None:
        logger.exception("Worker failed for product %s", product_id)
        self.queued_product_ids.discard(product_id)
        self.source_model.update_result(build_error_result(product_id, message))
        self.status.showMessage(f"Error updating product {product_id}")

    def _on_double_click(self, proxy_index) -> None:
        if not proxy_index.isValid():
            return

        source_index = self.proxy_model.mapToSource(proxy_index)

        if source_index.column() == 12:
            self.source_model.open_listing(source_index.row())
        elif source_index.column() == 13:
            self._show_matches_dialog(source_index.row())

    def _on_concurrency_changed(self, value: int) -> None:
        self.thread_pool.setMaxThreadCount(value)
        self.status.showMessage(f"Max concurrent checks set to {value}")

    def _resize_columns(self) -> None:
        widths = {
            0: 90,
            1: 150,
            2: 150,
            3: 120,
            4: 100,
            5: 120,
            6: 150,
            7: 100,
            8: 90,
            9: 90,
            10: 100,
            11: 100,
            12: 70,
            13: 420,
        }
        for column, width in widths.items():
            self.table.setColumnWidth(column, width)

    def _show_matches_dialog(self, source_row_number: int) -> None:
        row = self.source_model.rows[source_row_number]
        if not row.top_matches:
            QMessageBox.information(self, "No matches", "No competitor matches are available for this product.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Competitor Matches - Product {row.product_id}")
        dialog.resize(1100, 500)

        table = QTableWidget(dialog)
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels([
            "Seller",
            "Title",
            "Total",
            "Item",
            "Shipping",
            "Condition",
            "Listing",
        ])
        table.setRowCount(len(row.top_matches))
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setWordWrap(False)

        for i, match in enumerate(row.top_matches):
            seller = ""
            seller_info = match.get("seller") or {}
            seller = (
                seller_info.get("username")
                or seller_info.get("userId")
                or seller_info.get("sellerUsername")
                or ""
            )

            title = match.get("title") or ""
            condition = match.get("condition") or ""
            item_url = match.get("itemWebUrl") or match.get("itemAffiliateWebUrl") or ""

            price_obj = match.get("price") or {}
            item_price = price_obj.get("value")
            shipping_price = ""
            shipping_options = match.get("shippingOptions") or []
            if shipping_options:
                costs = []
                for option in shipping_options:
                    shipping_cost = option.get("shippingCost") or {}
                    value = shipping_cost.get("value")
                    if value is not None:
                        try:
                            costs.append(float(value))
                        except (TypeError, ValueError):
                            pass
                if costs:
                    shipping_price = f"${min(costs):,.2f}"

            item_price_text = ""
            total_text = ""
            try:
                if item_price is not None:
                    item_price_value = float(item_price)
                    item_price_text = f"${item_price_value:,.2f}"
                    if shipping_price:
                        total_text = f"${item_price_value + float(shipping_price.replace('$', '').replace(',', '')):,.2f}"
                    else:
                        total_text = item_price_text
            except (TypeError, ValueError):
                pass

            table.setItem(i, 0, QTableWidgetItem(seller))
            table.setItem(i, 1, QTableWidgetItem(title))
            table.setItem(i, 2, QTableWidgetItem(total_text))
            table.setItem(i, 3, QTableWidgetItem(item_price_text))
            table.setItem(i, 4, QTableWidgetItem(shipping_price))
            table.setItem(i, 5, QTableWidgetItem(condition))
            listing_item = QTableWidgetItem("Open" if item_url else "")
            listing_item.setData(Qt.UserRole, item_url)
            if item_url:
                listing_item.setForeground(QColor("#1a73e8"))
                font = listing_item.font()
                font.setUnderline(True)
                listing_item.setFont(font)
            table.setItem(i, 6, listing_item)

        def open_match(item: QTableWidgetItem) -> None:
            if item.column() == 6:
                url = item.data(Qt.UserRole)
                if url:
                    QDesktopServices.openUrl(QUrl(url))

        table.itemDoubleClicked.connect(open_match)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        layout = QVBoxLayout()
        layout.addWidget(table)
        dialog.setLayout(layout)
        dialog.exec()
