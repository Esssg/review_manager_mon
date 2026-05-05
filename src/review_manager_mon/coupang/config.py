from __future__ import annotations

from dataclasses import dataclass

from review_manager_mon.utils.env import optional_int_env, require_env


@dataclass(frozen=True)
class CrawlerConfig:
    supabase_url: str
    supabase_service_role_key: str
    request_timeout_ms: int
    max_pages: int


def load_config(max_pages: int | None) -> CrawlerConfig:
    return CrawlerConfig(
        supabase_url=require_env("SUPABASE_URL"),
        supabase_service_role_key=require_env("SUPABASE_SERVICE_ROLE_KEY"),
        request_timeout_ms=optional_int_env("CRAWL_REQUEST_TIMEOUT_MS", 15000),
        max_pages=max_pages or optional_int_env("CRAWL_MAX_PAGES", 5),
    )
