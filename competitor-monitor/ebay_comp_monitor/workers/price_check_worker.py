from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ebay_comp_monitor.models import CheckState, CompareResult, ProductRow
from ebay_comp_monitor.services.compare_service import CompareService


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(int, str)


class PriceCheckWorker(QRunnable):
    def __init__(self, product: ProductRow, compare_service: CompareService) -> None:
        super().__init__()
        self.product = product
        self.compare_service = compare_service
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.compare_service.compare_product(self.product)
            self.signals.finished.emit(result)
        except Exception as exc:  # pragma: no cover - UI/runtime path
            message = f"{exc}\n{traceback.format_exc(limit=3)}"
            self.signals.failed.emit(self.product.product_id, message)


def build_error_result(product_id: int, message: str) -> CompareResult:
    return CompareResult(
        product_id=product_id,
        status=CheckState.ERROR,
        notes=message,
    )
