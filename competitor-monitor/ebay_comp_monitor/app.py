from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from ebay_comp_monitor.config import load_settings
from ebay_comp_monitor.services.compare_service import CompareService
from ebay_comp_monitor.services.d1_client import D1Client
from ebay_comp_monitor.services.ebay_client import EbayClient
from ebay_comp_monitor.ui.main_window import MainWindow
from ebay_comp_monitor.ui.product_table_model import ProductTableModel


logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

from PySide6.QtWidgets import QApplication, QMessageBox

def apply_light_theme(app: QApplication) -> None:
    app.setStyleSheet("""
        QWidget {
            background-color: #f7f7f7;
            color: #202124;
        }
        QMainWindow, QWidget {
            background-color: #f7f7f7;
            color: #202124;
        }
        QTableView {
            background-color: #ffffff;
            alternate-background-color: #f3f6fa;
            color: #202124;
            gridline-color: #d0d7de;
            selection-background-color: #cfe8ff;
            selection-color: #111111;
        }
        QHeaderView::section {
            background-color: #e9eef5;
            color: #202124;
            padding: 4px;
            border: 1px solid #d0d7de;
        }
        QLineEdit, QSpinBox, QPushButton {
            background-color: #ffffff;
            color: #202124;
            border: 1px solid #c5cbd3;
            padding: 4px;
        }
        QPushButton:hover {
            background-color: #f0f4f8;
        }
        QStatusBar {
            background-color: #eef2f6;
            color: #202124;
        }
    """)


def main() -> int:
    app = QApplication(sys.argv)
    apply_light_theme(app)
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        d1_client = D1Client(settings)
        ebay_client = EbayClient(settings)
        compare_service = CompareService(ebay_client)
        rows = d1_client.load_product_rows()
        model = ProductTableModel(rows)
        window = MainWindow(settings, model, compare_service)
        window.show()
        window.start_initial_refresh()
        return app.exec()
    except Exception as exc:  # pragma: no cover - app startup path
        logger.exception("Startup failure")
        QMessageBox.critical(None, "Startup failure", str(exc))
        return 1
