from __future__ import annotations

from typing import Any

import requests

from ebay_comp_monitor.config import Settings
from ebay_comp_monitor.models import ProductRow


class D1Client:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.cloudflare_api_token}",
                "Content-Type": "application/json",
            }
        )
        self.base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{settings.cloudflare_account_id}/d1/database/{settings.cloudflare_d1_database_id}"
        )

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/query",
            json={"sql": sql, "params": params or []},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", False):
            raise RuntimeError(f"Cloudflare D1 query failed: {payload}")

        result_sets = payload.get("result") or []
        if not result_sets:
            return []

        first_result = result_sets[0]
        if not first_result.get("success", True):
            raise RuntimeError(f"Cloudflare D1 SQL execution failed: {first_result}")
        return first_result.get("results") or []

    def load_product_rows(self) -> list[ProductRow]:
        sql = """
        SELECT
            p.product_id,
            p.upc,
            p.brand,
            p.model,
            MIN(l.current_price) AS your_price,
            MAX(l.title) AS sample_title,
            MAX(l.ebay_category_1_number) AS ebay_category_1_number,
            MAX(l.condition) AS condition
        FROM products p
        JOIN listings l ON l.product_id = p.product_id
        GROUP BY p.product_id, p.upc, p.brand, p.model
        ORDER BY COALESCE(p.brand, ''), COALESCE(p.model, ''), p.product_id
        """
        rows = self.query(sql)
        results: list[ProductRow] = []
        for row in rows:
            results.append(
                ProductRow(
                    product_id=int(row["product_id"]),
                    upc=row.get("upc") or None,
                    brand=row.get("brand") or None,
                    model=row.get("model") or None,
                    your_price=_to_float(row.get("your_price")),
                    sample_title=row.get("sample_title") or None,
                    ebay_category_1_number=_to_int(row.get("ebay_category_1_number")),
                    condition=row.get("condition") or None,
                )
            )
        return results


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)



def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
