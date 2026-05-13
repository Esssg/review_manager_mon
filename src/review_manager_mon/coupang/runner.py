from __future__ import annotations

from review_manager_mon.coupang.config import load_config
from review_manager_mon.coupang.request_crawler import run_request_crawl
from review_manager_mon.db import supabase_rest


class CrawlerDb:
    def __init__(self, client: supabase_rest.SupabaseRestClient):
        self.client = client

    def get_existing_order_numbers(self, **kwargs) -> set[str]:
        return supabase_rest.get_existing_order_numbers(self.client, **kwargs)

    def get_coupang_payment_method_mappings(self) -> dict[str, str]:
        return supabase_rest.get_coupang_payment_method_mappings(self.client)

    def insert_crawl_order(self, payload: dict) -> dict | None:
        return supabase_rest.insert_crawl_order(self.client, payload)

    def update_platform_account_curl(self, **kwargs) -> dict | None:
        return supabase_rest.update_platform_account_curl(self.client, **kwargs)


def run_crawler(args) -> dict:
    config = load_config(args.max_pages)
    client = supabase_rest.SupabaseRestClient(
        url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )

    platform_account = supabase_rest.get_platform_account(
        client,
        platform_account_id=args.platform_account_id,
    )
    supabase_rest.update_platform_account_status(
        client,
        platform_account_id=platform_account["id"],
        status=True,
    )
    try:
        return run_request_crawl(
            config=config,
            platform_account=platform_account,
            db=CrawlerDb(client),
        )
    finally:
        # 크롤링 성공/실패와 상관없이 작업 종료 상태를 DB에 남깁니다.
        supabase_rest.update_platform_account_status(
            client,
            platform_account_id=platform_account["id"],
            status=False,
        )
