from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CheckState(str, Enum):
    IDLE = "Idle"
    QUEUED = "Queued"
    CHECKING = "Checking..."
    OK = "OK"
    NO_MATCH = "No match"
    ERROR = "Error"


@dataclass(slots=True)
class ProductRow:
    product_id: int
    upc: str | None
    brand: str | None
    model: str | None
    your_price: float | None
    sample_title: str | None
    ebay_category_1_number: int | None
    condition: str | None
    status: CheckState = CheckState.IDLE
    competitor_total_price: float | None = None
    competitor_item_price: float | None = None
    competitor_shipping_price: float | None = None
    competitor_seller: str | None = None
    competitor_title: str | None = None
    competitor_url: str | None = None
    delta_vs_you: float | None = None
    match_method: str | None = None
    notes: str | None = None
    top_matches: list[dict[str, Any]] = field(default_factory=list)

    def display_name(self) -> str:
        parts = [part for part in [self.brand, self.model] if part]
        if parts:
            return " ".join(parts)
        if self.sample_title:
            return self.sample_title
        return f"Product {self.product_id}"


@dataclass(slots=True)
class CompetitorListing:
    seller_key: str | None
    seller_display: str | None
    title: str | None
    item_id: str | None
    legacy_item_id: str | None
    item_url: str | None
    item_price: float | None
    shipping_price: float | None
    total_price: float | None
    currency: str | None
    condition: str | None
    buying_options: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CompareResult:
    product_id: int
    status: CheckState
    best_match: CompetitorListing | None = None
    top_matches: list[CompetitorListing] = field(default_factory=list)
    match_method: str | None = None
    notes: str | None = None
