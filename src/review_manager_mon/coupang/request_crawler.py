from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
from html.parser import HTMLParser
import json
import re
import shlex
from typing import Callable, Iterable, Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class ParsedCurl:
    url: str
    headers: dict[str, str]


class MultiProductOrderError(ValueError):
    pass


def run_request_crawl(
    *,
    config,
    platform_account: dict,
    db,
    fetch_page: Callable[[int], str] | None = None,
    fetch_order_detail: Callable[[str], str] | None = None,
) -> dict:
    raw_curl = str(platform_account.get("curl") or "").strip()
    if not raw_curl:
        raise RuntimeError("platform_accounts.curl is empty")

    parsed_curl = parse_curl(raw_curl)
    page_fetcher = fetch_page or (
        lambda page_index: fetch_order_list_html(
            parsed_curl=parsed_curl,
            page_index=page_index,
            timeout_ms=config.request_timeout_ms,
        )
    )
    detail_fetcher = fetch_order_detail or (
        lambda order_number: fetch_order_detail_html(
            parsed_curl=parsed_curl,
            order_number=order_number,
            timeout_ms=config.request_timeout_ms,
        )
    )
    payment_method_ids = db.get_coupang_payment_method_mappings()

    inserted: list[str] = []
    skipped_duplicates: list[str] = []
    skipped_multi_product: list[str] = []
    failed: list[dict] = []
    requested_pages: list[int] = []

    for page_index in range(config.max_pages):
        requested_pages.append(page_index)
        order_list = extract_order_list(page_fetcher(page_index))
        if not order_list:
            break

        order_numbers = [str(order["orderId"]) for order in order_list if order.get("orderId")]
        existing = db.get_existing_order_numbers(
            user_id=platform_account["user_id"],
            platform_id=platform_account["platform_id"],
            buyer_account_id=platform_account["buyer_account_id"],
            order_numbers=order_numbers,
        )
        stop_after_page = bool(existing)

        for order in order_list:
            order_number = str(order.get("orderId") or "")
            if not order_number:
                failed.append({"orderNumber": None, "message": "Missing orderId"})
                continue

            if order_number in existing:
                skipped_duplicates.append(order_number)
                continue

            try:
                payload = order_to_payload(order, platform_account)
                payload["payment_method_id"] = payment_method_id_for_order(
                    order_number=order_number,
                    detail_fetcher=detail_fetcher,
                    payment_method_ids=payment_method_ids,
                )
                row = db.insert_crawl_order(payload)
            except MultiProductOrderError:
                skipped_multi_product.append(order_number)
                continue
            except Exception as exc:
                failed.append({"orderNumber": order_number, "message": str(exc)})
                continue

            if row:
                inserted.append(order_number)
            else:
                skipped_duplicates.append(order_number)

        if stop_after_page:
            break

    discovered_count = len(inserted) + len(skipped_duplicates) + len(skipped_multi_product) + len(failed)
    return {
        "platformAccountId": platform_account["id"],
        "requestedPages": requested_pages,
        "discoveredCount": discovered_count,
        "insertedCount": len(inserted),
        "skippedDuplicateCount": len(skipped_duplicates),
        "skippedMultiProductCount": len(skipped_multi_product),
        "failedCount": len(failed),
        "inserted": inserted,
        "skipped": skipped_duplicates,
        "skippedMultiProduct": skipped_multi_product,
        "failed": failed,
    }


def parse_curl(raw_curl: str) -> ParsedCurl:
    tokens = shlex.split(raw_curl.replace("\\\n", " "))
    if not tokens or tokens[0] != "curl":
        raise ValueError("curl string must start with curl")

    url: str | None = None
    headers: dict[str, str] = {}
    cookies: list[str] = []
    index = 1

    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"}:
            index += 1
            name, value = parse_header(tokens[index])
            # 압축 응답은 표준 urllib에서 자동 해제되지 않을 수 있어 요청하지 않습니다.
            if name.lower() not in {"accept-encoding", "content-length", "host"}:
                headers[name] = value
        elif token in {"-b", "--cookie", "--cookie-jar"}:
            index += 1
            if token != "--cookie-jar":
                cookies.append(tokens[index])
        elif token == "--compressed":
            pass
        elif token.startswith("http://") or token.startswith("https://"):
            url = token
        elif token in {"-X", "--request", "--data", "--data-raw", "--data-binary"}:
            index += 1
        index += 1

    if not url:
        raise ValueError("curl string does not include a request URL")

    if cookies:
        existing_cookie = headers.get("Cookie") or headers.get("cookie")
        cookie_value = "; ".join([*(filter(None, [existing_cookie])), *cookies])
        headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
        headers["Cookie"] = cookie_value

    return ParsedCurl(url=url, headers=headers)


def parse_header(value: str) -> tuple[str, str]:
    name, separator, header_value = value.partition(":")
    if not separator or not name.strip():
        raise ValueError(f"Invalid curl header: {value}")
    return name.strip(), header_value.strip()


def page_url(url: str, page_index: int) -> str:
    parts = urlsplit(url)
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name != "pageIndex"]
    query.append(("pageIndex", str(page_index)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def order_detail_url(url: str, order_number: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/ssr/desktop/order/{order_number}", "", ""))


def fetch_order_list_html(*, parsed_curl: ParsedCurl, page_index: int, timeout_ms: int) -> str:
    request = Request(page_url(parsed_curl.url, page_index), headers=parsed_curl.headers, method="GET")
    try:
        with urlopen(request, timeout=timeout_ms / 1000) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Coupang order list request failed on pageIndex={page_index} ({exc.code})") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Coupang order list request failed on pageIndex={page_index}: {exc}") from exc


def fetch_order_detail_html(*, parsed_curl: ParsedCurl, order_number: str, timeout_ms: int) -> str:
    request = Request(order_detail_url(parsed_curl.url, order_number), headers=parsed_curl.headers, method="GET")
    try:
        with urlopen(request, timeout=timeout_ms / 1000) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Coupang order detail request failed for order_number={order_number} ({exc.code})") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Coupang order detail request failed for order_number={order_number}: {exc}") from exc


def extract_order_list(response_html: str) -> list[dict]:
    next_data = extract_next_data(response_html)
    desktop_order = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("domains", {})
        .get("desktopOrder", {})
    )
    order_list = desktop_order.get("orderList")
    if order_list is None:
        order_list = find_order_list(next_data)
    if not isinstance(order_list, list):
        raise ValueError("Coupang response does not include orderList")
    return [order for order in order_list if isinstance(order, dict)]


def extract_next_data(response_html: str) -> dict:
    match = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        response_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise ValueError("Coupang response does not include __NEXT_DATA__")

    # Next.js JSON은 HTML script 안에 들어 있으므로 먼저 HTML entity만 원래 문자로 되돌립니다.
    data = json.loads(html.unescape(match.group(1)).strip())
    if not isinstance(data, dict):
        raise ValueError("__NEXT_DATA__ is not a JSON object")
    return data


def find_order_list(value: Any) -> list | None:
    if isinstance(value, dict):
        order_list = value.get("orderList")
        if isinstance(order_list, list):
            return order_list
        for child in value.values():
            found = find_order_list(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_order_list(child)
            if found is not None:
                return found
    return None


def payment_method_id_for_order(
    *,
    order_number: str,
    detail_fetcher: Callable[[str], str],
    payment_method_ids: dict[str, str],
) -> str | None:
    payment_method_name = extract_payment_method_name(detail_fetcher(order_number))
    if not payment_method_name:
        return None
    return payment_method_ids.get(payment_method_name)


def extract_payment_method_name(response_html: str) -> str | None:
    parser = PaymentMethodParser()
    parser.feed(response_html)
    return parser.payment_method_name


class PaymentMethodParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.target_tbody_depth: int | None = None
        self.target_tr_depth: int | None = None
        self.target_th_depth: int | None = None
        self.target_div_depth: int | None = None
        self.row_index = 0
        self.text_parts: list[str] = []
        self.payment_method_name: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        depth = len(self.stack)
        attr_map = {name: value or "" for name, value in attrs}

        if tag == "tbody" and self._has_target_tbody_class(attr_map.get("class", "")):
            self.target_tbody_depth = depth
            self.row_index = 0
        elif tag == "tr" and self.target_tbody_depth is not None and self._parent_is("tbody"):
            self.row_index += 1
            if self.row_index == 2:
                self.target_tr_depth = depth
        elif tag == "th" and self.target_tr_depth is not None and self._parent_is("tr"):
            self.target_th_depth = depth
        elif tag == "div" and self.target_th_depth is not None and self._parent_is("th"):
            self.target_div_depth = depth

        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        depth = len(self.stack) - 1

        if self.target_div_depth == depth and tag == "div":
            # 쿠팡 상세 페이지의 결제수단 셀 텍스트만 공백을 정리해 저장합니다.
            text = re.sub(r"\s+", " ", "".join(self.text_parts)).strip()
            self.payment_method_name = text or None
            self.text_parts = []
            self.target_div_depth = None
        if self.target_th_depth == depth and tag == "th":
            self.target_th_depth = None
        if self.target_tr_depth == depth and tag == "tr":
            self.target_tr_depth = None
        if self.target_tbody_depth == depth and tag == "tbody":
            self.target_tbody_depth = None

        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.target_div_depth is not None and self.payment_method_name is None:
            self.text_parts.append(data)

    def _parent_is(self, tag: str) -> bool:
        return bool(self.stack) and self.stack[-1] == tag

    def _has_target_tbody_class(self, class_value: str) -> bool:
        classes = set(class_value.split())
        return {"sc-97871ab4-1", "gbbDZu"}.issubset(classes)


def order_to_payload(order: dict, platform_account: dict, payment_method_id: str | None = None) -> dict:
    order_number = str(required_value(order, "orderId"))
    products = distinct_delivery_products(order)
    if len(products) != 1:
        raise MultiProductOrderError(f"Order {order_number} has {len(products)} products and was skipped")

    unit_price = required_value(products[0], "unitPrice")
    return {
        "user_id": platform_account["user_id"],
        "product_name": str(required_value(order, "title")).strip(),
        "purchase_date": purchase_date_from_millis(required_value(order, "orderedAt")),
        "purchase_price_krw": unit_price,
        "product_url": None,
        "order_number": order_number,
        "platform_id": platform_account["platform_id"],
        "payment_method_id": payment_method_id,
        "buyer_account_id": platform_account["buyer_account_id"],
        "crawl_order_status": 0,
    }


def distinct_delivery_products(order: dict) -> list[dict]:
    products: dict[tuple[str, str, str], dict] = {}
    for product in iter_delivery_products(order.get("deliveryGroupList", [])):
        product_key = (
            str(product.get("vendorItemId") or ""),
            str(product.get("itemId") or ""),
            str(product.get("productId") or ""),
        )
        products.setdefault(product_key, product)
    return list(products.values())


def iter_delivery_products(delivery_groups: Iterable) -> Iterable[dict]:
    for group in delivery_groups:
        if not isinstance(group, dict):
            continue
        for product in group.get("productList", []):
            if isinstance(product, dict):
                yield product


def purchase_date_from_millis(value: int | str) -> str:
    timestamp_ms = int(value)
    kst_datetime = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone(KST)
    return kst_datetime.date().isoformat()


def required_value(source: dict, key: str):
    value = source.get(key)
    if value in (None, ""):
        raise ValueError(f"Missing required order value: {key}")
    return value
