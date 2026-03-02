from __future__ import annotations

from ebay_comp_monitor.models import CheckState, CompareResult, ProductRow
from ebay_comp_monitor.services.ebay_client import EbayClient


class CompareService:
    def __init__(self, ebay_client: EbayClient) -> None:
        self.ebay_client = ebay_client

    def compare_product(self, product: ProductRow) -> CompareResult:
        listings, match_method = self.ebay_client.search_competitors(product)
        if not listings:
            return CompareResult(
                product_id=product.product_id,
                status=CheckState.NO_MATCH,
                match_method=match_method,
                notes="No competitor matches returned by the current eBay search.",
            )

        best_match = listings[0]
        return CompareResult(
            product_id=product.product_id,
            status=CheckState.OK,
            best_match=best_match,
            top_matches=listings,
            match_method=match_method,
            notes=f"Returned {len(listings)} accepted competitor match(es).",
        )

