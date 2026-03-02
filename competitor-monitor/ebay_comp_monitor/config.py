from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    cloudflare_account_id: str
    cloudflare_d1_database_id: str
    cloudflare_api_token: str
    ebay_client_id: str
    ebay_client_secret: str
    ebay_environment: str = "production"
    ebay_marketplace_id: str = "EBAY_US"
    ebay_oauth_scope: str = "https://api.ebay.com/oauth/api_scope"
    buyer_country: str = "US"
    buyer_zip: str = "10001"
    competitor_sellers: tuple[str, ...] = ()
    excluded_sellers: tuple[str, ...] = ()
    max_concurrent_checks: int = 6
    initial_visible_batch_size: int = 50
    search_limit: int = 25
    fixed_price_only: bool = True
    log_level: str = "INFO"

    @property
    def root_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def load_settings() -> Settings:
    return Settings(
        cloudflare_account_id=_env("CLOUDFLARE_ACCOUNT_ID"),
        cloudflare_d1_database_id=_env("CLOUDFLARE_D1_DATABASE_ID"),
        cloudflare_api_token=_env("CLOUDFLARE_API_TOKEN"),
        ebay_client_id=_env("EBAY_CLIENT_ID"),
        ebay_client_secret=_env("EBAY_CLIENT_SECRET"),
        ebay_environment=_env("EBAY_ENVIRONMENT", "production").lower(),
        ebay_marketplace_id=_env("EBAY_MARKETPLACE_ID", "EBAY_US"),
        ebay_oauth_scope=_env("EBAY_OAUTH_SCOPE", "https://api.ebay.com/oauth/api_scope"),
        buyer_country=_env("BUYER_COUNTRY", "US"),
        buyer_zip=_env("BUYER_ZIP", "10001"),
        competitor_sellers=_split_csv(_env("COMPETITOR_SELLERS", "")),
        excluded_sellers=_split_csv(_env("EXCLUDED_SELLERS", "")),
        max_concurrent_checks=int(_env("MAX_CONCURRENT_CHECKS", "6")),
        initial_visible_batch_size=int(_env("INITIAL_VISIBLE_BATCH_SIZE", "50")),
        search_limit=int(_env("SEARCH_LIMIT", "25")),
        fixed_price_only=_env("FIXED_PRICE_ONLY", "1") not in {"0", "false", "False"},
        log_level=_env("LOG_LEVEL", "INFO"),
    )
