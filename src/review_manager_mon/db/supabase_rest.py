from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError


@dataclass(frozen=True)
class SupabaseRestClient:
    url: str
    service_role_key: str

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: dict | None = None,
    ):
        base_url = self.url.rstrip("/") + "/rest/v1/" + path
        request_url = base_url if not params else base_url + "?" + urlencode(params)
        request_headers = {
            "apikey": self.service_role_key,
            "authorization": f"Bearer {self.service_role_key}",
            **(headers or {}),
        }
        payload = None

        if body is not None:
            request_headers["content-type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")

        request = Request(request_url, data=payload, headers=request_headers, method=method)

        try:
            with urlopen(request) as response:
                text = response.read().decode("utf-8")
        except HTTPError as exc:
            error_text = exc.read().decode("utf-8")
            message = _extract_error_message(error_text) or exc.reason
            raise RuntimeError(f"Supabase request failed ({exc.code}): {message}") from exc

        return json.loads(text) if text else None


def get_platform_account(
    client: SupabaseRestClient,
    *,
    platform_account_id: str,
) -> dict:
    rows = client.request(
        "platform_accounts",
        params={
            "select": "id,user_id,platform_id,buyer_account_id,curl,is_active,status",
            "id": f"eq.{platform_account_id}",
            "limit": "1",
        },
    )

    if not rows:
        raise RuntimeError("No matching platform account found for platform-account argument")
    if not rows[0]["is_active"]:
        raise RuntimeError("Platform account is inactive")

    return rows[0]


def update_platform_account_status(
    client: SupabaseRestClient,
    *,
    platform_account_id: str,
    status: bool,
) -> dict | None:
    # 크롤링이 진행 중인지 한눈에 볼 수 있도록 해당 계정 행의 상태만 바꿉니다.
    rows = client.request(
        "platform_accounts",
        method="PATCH",
        params={"id": f"eq.{platform_account_id}"},
        headers={"Prefer": "return=representation"},
        body={"status": status},
    )
    return rows[0] if rows else None


def get_existing_order_numbers(
    client: SupabaseRestClient,
    *,
    user_id: str,
    platform_id: str,
    buyer_account_id: str,
    order_numbers: list[str],
) -> set[str]:
    existing: set[str] = set()

    for order_number in dict.fromkeys(filter(None, order_numbers)):
        crawl_rows = client.request(
            "crawl_orders",
            params={
                "select": "order_number",
                "user_id": f"eq.{user_id}",
                "platform_id": f"eq.{platform_id}",
                "buyer_account_id": f"eq.{buyer_account_id}",
                "order_number": f"eq.{order_number}",
                "limit": "1",
            },
        )
        if crawl_rows:
            existing.add(order_number)
            continue

        order_rows = client.request(
            "orders",
            params={
                "select": "order_number",
                "user_id": f"eq.{user_id}",
                "order_number": f"eq.{order_number}",
                "limit": "1",
            },
        )
        if order_rows:
            existing.add(order_number)

    return existing


def get_coupang_payment_method_mappings(client: SupabaseRestClient) -> dict[str, str]:
    rows = client.request(
        "coupang_payment_method_mappings",
        params={
            "select": "payment_method_name,payment_method_id",
        },
    )
    return {row["payment_method_name"]: row["payment_method_id"] for row in rows or []}


def insert_crawl_order(client: SupabaseRestClient, payload: dict) -> dict | None:
    rows = client.request(
        "crawl_orders",
        method="POST",
        params={"on_conflict": "user_id,platform_id,buyer_account_id,order_number"},
        headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
        body=payload,
    )
    return rows[0] if rows else None


def _extract_error_message(error_text: str) -> str | None:
    try:
        parsed = json.loads(error_text)
    except json.JSONDecodeError:
        return error_text or None
    return parsed.get("message") or parsed.get("error")
