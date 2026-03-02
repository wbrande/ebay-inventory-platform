from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from ebay_comp_monitor.config import Settings
from ebay_comp_monitor.models import CompetitorListing, ProductRow


@dataclass(slots=True)
class TokenCache:
    access_token: str | None = None
    expires_at: float = 0.0


class EbayClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.token_cache = TokenCache()
        if settings.ebay_environment == "sandbox":
            self.api_base = "https://api.sandbox.ebay.com"
            self.identity_base = "https://api.sandbox.ebay.com"
        else:
            self.api_base = "https://api.ebay.com"
            self.identity_base = "https://api.ebay.com"

    def search_competitors(self, product: ProductRow) -> tuple[list[CompetitorListing], str]:
        filter_parts: list[str] = []

        if self.settings.competitor_sellers:
            filter_parts.append(f"sellers:{{{'|'.join(self.settings.competitor_sellers)}}}")

        if self.settings.fixed_price_only:
            filter_parts.append("buyingOptions:{FIXED_PRICE}")

        if product.condition:
            normalized_condition = product.condition.strip()
            if normalized_condition:
                # The textual condition returned by your DB may not always align perfectly
                # with Browse filter enumerations. This is left out intentionally from the
                # request and handled as a softer client-side signal instead.
                pass

        base_params: dict[str, str] = {
            "limit": str(self.settings.search_limit),
            "sort": "price",
        }

        if filter_parts:
            base_params["filter"] = ",".join(filter_parts)

        if product.ebay_category_1_number:
            base_params["category_ids"] = str(product.ebay_category_1_number)

        headers = self._browse_headers()
        all_listings: list[CompetitorListing] = []

        if product.upc:
            match_method = "GTIN"
            for gtin in self._gtin_variants(product.upc):
                params = dict(base_params)
                params["gtin"] = gtin
                response = self.session.get(
                    f"{self.api_base}/buy/browse/v1/item_summary/search",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                summaries = payload.get("itemSummaries") or []
                all_listings.extend(self._parse_listing(summary) for summary in summaries)
        else:
            query = self._build_keyword_query(product)
            if not query:
                raise RuntimeError(
                    f"Product {product.product_id} does not have enough identifying data to search eBay."
                )
            match_method = "KEYWORDS"
            params = dict(base_params)
            params["q"] = query
            response = self.session.get(
                f"{self.api_base}/buy/browse/v1/item_summary/search",
                headers=headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            summaries = payload.get("itemSummaries") or []
            all_listings.extend(self._parse_listing(summary) for summary in summaries)

        listings = [listing for listing in all_listings if listing.seller_key]
        listings = self._apply_seller_exclusions(listings)

        deduped: list[CompetitorListing] = []
        seen_keys: set[str] = set()
        for listing in listings:
            listing_key = listing.item_id or listing.legacy_item_id or listing.item_url or ""
            if listing_key and listing_key in seen_keys:
                continue
            if listing_key:
                seen_keys.add(listing_key)
            deduped.append(listing)
        listings = deduped

        if match_method != "GTIN":
            listings = self._filter_keyword_matches(product, listings)

        listings.sort(key=lambda listing: (listing.total_price is None, listing.total_price or float("inf")))
        return listings, match_method

    def get_rate_limits(self) -> dict[str, Any]:
        response = self.session.get(
            f"{self.api_base}/developer/analytics/v1_beta/rate_limit/",
            headers=self._analytics_headers(),
            params={"api_name": "browse"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _apply_seller_exclusions(self, listings: list[CompetitorListing]) -> list[CompetitorListing]:
        if not self.settings.excluded_sellers:
            return listings

        excluded = {value.casefold() for value in self.settings.excluded_sellers}
        kept: list[CompetitorListing] = []

        for listing in listings:
            candidates = {
                (listing.seller_key or "").casefold(),
                (listing.seller_display or "").casefold(),
            }
            if candidates & excluded:
                continue
            kept.append(listing)

        return kept

    def _browse_headers(self) -> dict[str, str]:
        token = self._get_access_token()
        end_user_ctx = quote(
            f"contextualLocation=country={self.settings.buyer_country},zip={self.settings.buyer_zip}",
            safe="=,",
        )
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id,
            "X-EBAY-C-ENDUSERCTX": end_user_ctx,
        }

    def _analytics_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Accept": "application/json",
        }

    def _get_access_token(self) -> str:
        now = time.time()
        if self.token_cache.access_token and now < self.token_cache.expires_at - 60:
            return self.token_cache.access_token

        raw = f"{self.settings.ebay_client_id}:{self.settings.ebay_client_secret}".encode("utf-8")
        basic = base64.b64encode(raw).decode("ascii")

        response = self.session.post(
            f"{self.identity_base}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": self.settings.ebay_oauth_scope,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()

        access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 7200))
        self.token_cache = TokenCache(
            access_token=access_token,
            expires_at=now + expires_in,
        )
        return access_token

    def _build_keyword_query(self, product: ProductRow) -> str:
        terms: list[str] = []

        for value in (product.brand, product.model):
            if value:
                cleaned = " ".join(value.split())
                if cleaned:
                    terms.append(cleaned)

        if not terms and product.sample_title:
            title = self._clean_title(product.sample_title)
            if title:
                terms.append(title)

        return " ".join(dict.fromkeys(terms))

    def _gtin_variants(self, upc: str) -> list[str]:
        upc = (upc or "").strip()
        variants = []
        seen = set()

        def add(value: str) -> None:
            if value and value not in seen:
                seen.add(value)
                variants.append(value)

        add(upc)

        if upc.isdigit():
            if len(upc) == 12:
                add("0" + upc)
            elif len(upc) == 13 and upc.startswith("0"):
                add(upc[1:])

        return variants

    def _filter_keyword_matches(
        self,
        product: ProductRow,
        listings: list[CompetitorListing],
    ) -> list[CompetitorListing]:
        brand_tokens = _tokens(product.brand)
        model_tokens = _tokens(product.model)
        accepted: list[CompetitorListing] = []

        for listing in listings:
            haystack = f"{listing.title or ''} {listing.raw.get('shortDescription', '')}".lower()

            if brand_tokens and not all(token in haystack for token in brand_tokens[:1]):
                continue

            if model_tokens and not any(token in haystack for token in model_tokens):
                continue

            accepted.append(listing)

        return accepted

    def _parse_listing(self, summary: dict[str, Any]) -> CompetitorListing:
        seller = summary.get("seller") or {}
        seller_key = (
            seller.get("username")
            or seller.get("userId")
            or seller.get("sellerUsername")
            or seller.get("sellerAccount")
        )
        seller_display = seller.get("username") or seller.get("userId") or seller_key

        item_price = _money_value(summary.get("price"))
        currency = _money_currency(summary.get("price"))
        shipping_price = _lowest_shipping(summary.get("shippingOptions") or [])

        total_price = None
        if item_price is not None:
            total_price = item_price + (shipping_price or 0.0)

        return CompetitorListing(
            seller_key=seller_key,
            seller_display=seller_display,
            title=summary.get("title"),
            item_id=summary.get("itemId"),
            legacy_item_id=summary.get("legacyItemId"),
            item_url=summary.get("itemWebUrl") or summary.get("itemAffiliateWebUrl"),
            item_price=item_price,
            shipping_price=shipping_price,
            total_price=total_price,
            currency=currency,
            condition=summary.get("condition"),
            buying_options=list(summary.get("buyingOptions") or []),
            raw=summary,
        )

    @staticmethod
    def _clean_title(title: str) -> str:
        junk = [
            "new",
            "used",
            "free shipping",
            "fast shipping",
            "read description",
            "look",
            "wow",
        ]
        lowered = title.lower()
        for token in junk:
            lowered = lowered.replace(token, " ")
        return " ".join(lowered.split())


def _money_value(money: dict[str, Any] | None) -> float | None:
    if not money:
        return None
    value = money.get("value")
    if value in (None, ""):
        return None
    return float(value)


def _money_currency(money: dict[str, Any] | None) -> str | None:
    if not money:
        return None
    return money.get("currency")


def _lowest_shipping(options: list[dict[str, Any]]) -> float | None:
    shipping_values: list[float] = []
    for option in options:
        shipping_cost = option.get("shippingCost")
        value = _money_value(shipping_cost)
        if value is not None:
            shipping_values.append(value)
        elif option.get("shippingCostType") == "FREE":
            shipping_values.append(0.0)

    if not shipping_values:
        return 0.0

    return min(shipping_values)


def _tokens(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = "".join(char.lower() if char.isalnum() else " " for char in value)
    return [token for token in normalized.split() if len(token) >= 2]
