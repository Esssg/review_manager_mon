from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import html
import io
from http.cookies import CookieError, SimpleCookie
from http.cookiejar import Cookie, CookieJar
from html.parser import HTMLParser
import json
import re
import shlex
from socket import timeout as SocketTimeout
from time import sleep
from typing import Callable, Iterable, Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
import zlib


KST = timezone(timedelta(hours=9))
COUPANG_REQUEST_ATTEMPTS = 3
COUPANG_RETRY_DELAYS_SECONDS = (0.5, 1.5)
RETRYABLE_HTTP_STATUS_CODES = {408, 500, 502, 503, 504}
BLOCKED_HTTP_STATUS_CODES = {401, 403, 429}


@dataclass(frozen=True)
class ParsedCurl:
    url: str
    headers: dict[str, str]


class CoupangCrawlerError(Exception):
    http_status_code = 502

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        upstream_status_code: int | None = None,
        page_index: int | None = None,
        order_number: str | None = None,
        reason: str | None = None,
        title: str | None = None,
        content_type: str | None = None,
        snippet: str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.stage = stage
        self.upstream_status_code = upstream_status_code
        self.page_index = page_index
        self.order_number = order_number
        self.reason = reason
        self.title = title
        self.content_type = content_type
        self.snippet = snippet
        self.retryable = retryable

    def to_dict(self) -> dict:
        detail = {
            "message": str(self),
            "stage": self.stage,
            "upstreamStatusCode": self.upstream_status_code,
            "pageIndex": self.page_index,
            "orderNumber": self.order_number,
            "reason": self.reason,
            "title": self.title,
            "contentType": self.content_type,
            "snippet": self.snippet,
            "retryable": self.retryable,
        }
        return {key: value for key, value in detail.items() if value is not None}


class CoupangCurlError(CoupangCrawlerError, ValueError):
    http_status_code = 422


class CoupangRequestError(CoupangCrawlerError):
    http_status_code = 502


class CoupangTimeoutError(CoupangRequestError):
    http_status_code = 504


class CoupangResponseError(CoupangCrawlerError, ValueError):
    http_status_code = 502


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
    http_client = CoupangHttpClient(
        parsed_curl=parsed_curl,
        timeout_ms=config.request_timeout_ms,
    )
    page_fetcher = fetch_page or (
        lambda page_index: fetch_order_list_html(
            client=http_client,
            page_index=page_index,
        )
    )
    detail_fetcher = fetch_order_detail or (
        lambda order_number: fetch_order_detail_html(
            client=http_client,
            order_number=order_number,
        )
    )
    payment_method_ids = db.get_coupang_payment_method_mappings()

    inserted: list[str] = []
    skipped_duplicates: list[str] = []
    skipped_multi_product: list[str] = []
    failed: list[dict] = []
    requested_pages: list[int] = []
    has_trusted_order_list_response = False

    for page_index in range(config.max_pages):
        requested_pages.append(page_index)
        order_list = extract_order_list(page_fetcher(page_index))
        if fetch_page is None:
            has_trusted_order_list_response = True
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

    curl_cookie_updated = False
    curl_cookie_update_error = None
    final_cookie_header = http_client.cookie_header() if has_trusted_order_list_response else None
    if final_cookie_header:
        try:
            curl_cookie_updated = update_platform_account_curl_cookie(
                db=db,
                platform_account=platform_account,
                raw_curl=raw_curl,
                cookie_header=final_cookie_header,
            )
        except Exception as exc:
            curl_cookie_update_error = str(exc)

    discovered_count = len(inserted) + len(skipped_duplicates) + len(skipped_multi_product) + len(failed)
    result = {
        "platformAccountId": platform_account["id"],
        "requestedPages": requested_pages,
        "discoveredCount": discovered_count,
        "insertedCount": len(inserted),
        "skippedDuplicateCount": len(skipped_duplicates),
        "skippedMultiProductCount": len(skipped_multi_product),
        "failedCount": len(failed),
        "curlCookieUpdated": curl_cookie_updated,
        "inserted": inserted,
        "skipped": skipped_duplicates,
        "skippedMultiProduct": skipped_multi_product,
        "failed": failed,
    }
    if curl_cookie_update_error:
        result["curlCookieUpdateError"] = curl_cookie_update_error
    return result


def parse_curl(raw_curl: str) -> ParsedCurl:
    try:
        tokens = shlex.split(raw_curl.replace("\\\n", " "))
    except ValueError as exc:
        raise CoupangCurlError(f"Invalid curl string: {exc}", stage="curl") from exc
    if not tokens or tokens[0] != "curl":
        raise CoupangCurlError("curl string must start with curl", stage="curl")

    url: str | None = None
    headers: dict[str, str] = {}
    cookies: list[str] = []
    index = 1

    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"}:
            value, index = curl_option_value(tokens, index, token)
            name, value = parse_header(value)
            headers[name] = value
        elif token.startswith("--header="):
            name, value = parse_header(token.partition("=")[2])
            headers[name] = value
        elif token in {"-b", "--cookie", "--cookie-jar"}:
            value, index = curl_option_value(tokens, index, token)
            if token != "--cookie-jar":
                cookies.append(value)
        elif token.startswith("--cookie="):
            cookies.append(token.partition("=")[2])
        elif token.startswith("--cookie-jar="):
            pass
        elif token == "--compressed":
            pass
        elif token in {"--url"}:
            url, index = curl_option_value(tokens, index, token)
        elif token.startswith("--url="):
            url = token.partition("=")[2]
        elif token in {"-A", "--user-agent"}:
            headers["User-Agent"], index = curl_option_value(tokens, index, token)
        elif token.startswith("--user-agent="):
            headers["User-Agent"] = token.partition("=")[2]
        elif token in {"-e", "--referer"}:
            headers["Referer"], index = curl_option_value(tokens, index, token)
        elif token.startswith("--referer="):
            headers["Referer"] = token.partition("=")[2]
        elif token.startswith("http://") or token.startswith("https://"):
            url = token
        elif token in {"-X", "--request", "--data", "--data-raw", "--data-binary"}:
            _, index = curl_option_value(tokens, index, token)
        elif token.startswith("--request=") or token.startswith("--data=") or token.startswith("--data-raw="):
            pass
        index += 1

    if not url:
        raise CoupangCurlError("curl string does not include a request URL", stage="curl")

    existing_cookie = cookie_header_from_headers(headers)
    if existing_cookie or cookies:
        cookie_value = "; ".join([*(filter(None, [existing_cookie])), *cookies])
        headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
        headers["Cookie"] = cookie_value

    return ParsedCurl(url=url, headers=headers)


def parse_header(value: str) -> tuple[str, str]:
    name, separator, header_value = value.partition(":")
    if not separator or not name.strip():
        raise CoupangCurlError(f"Invalid curl header: {value}", stage="curl")
    return name.strip(), header_value.strip()


def curl_option_value(tokens: list[str], index: int, option: str) -> tuple[str, int]:
    if index + 1 >= len(tokens):
        raise CoupangCurlError(f"curl option {option} requires a value", stage="curl")
    return tokens[index + 1], index + 1


def update_platform_account_curl_cookie(
    *,
    db,
    platform_account: dict,
    raw_curl: str,
    cookie_header: str,
) -> bool:
    updater = getattr(db, "update_platform_account_curl", None)
    if updater is None:
        return False

    current_cookie_header = cookie_header_from_headers(parse_curl(raw_curl).headers)
    if current_cookie_header == cookie_header:
        return False

    updated_curl = curl_with_cookie(raw_curl, cookie_header)
    if updated_curl == raw_curl:
        return False

    updater(platform_account_id=platform_account["id"], curl=updated_curl)
    return True


def curl_with_cookie(raw_curl: str, cookie_header: str) -> str:
    try:
        tokens = shlex.split(raw_curl.replace("\\\n", " "))
    except ValueError as exc:
        raise CoupangCurlError(f"Invalid curl string: {exc}", stage="curl") from exc

    if not tokens or tokens[0] != "curl":
        raise CoupangCurlError("curl string must start with curl", stage="curl")

    updated = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"}:
            value, index = curl_option_value(tokens, index, token)
            name, _ = parse_header(value)
            if name.lower() == "cookie":
                tokens[index] = f"{name}: {cookie_header}"
                updated = True
        elif token.startswith("--header="):
            value = token.partition("=")[2]
            name, _ = parse_header(value)
            if name.lower() == "cookie":
                tokens[index] = f"--header={name}: {cookie_header}"
                updated = True
        elif token in {"-b", "--cookie"}:
            _, index = curl_option_value(tokens, index, token)
            tokens[index] = cookie_header
            updated = True
        elif token.startswith("--cookie="):
            tokens[index] = f"--cookie={cookie_header}"
            updated = True
        elif token == "--cookie-jar":
            _, index = curl_option_value(tokens, index, token)
        index += 1

    if not updated:
        # 기존 cURL에 쿠키 옵션이 없으면 다음 실행에서 같은 세션을 시작할 수 있도록 헤더를 추가합니다.
        tokens.extend(["-H", f"cookie: {cookie_header}"])

    return shlex.join(tokens)


def page_url(url: str, page_index: int) -> str:
    parts = urlsplit(url)
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name != "pageIndex"]
    query.append(("pageIndex", str(page_index)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def order_detail_url(url: str, order_number: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/ssr/desktop/order/{order_number}", "", ""))


class CoupangHttpClient:
    def __init__(
        self,
        *,
        parsed_curl: ParsedCurl,
        timeout_ms: int,
        sleeper: Callable[[float], None] = sleep,
    ):
        self.parsed_curl = parsed_curl
        self.timeout_seconds = timeout_ms / 1000
        self.headers = dict(parsed_curl.headers)
        self.cookie_jar = cookie_jar_from_headers(parsed_curl.url, parsed_curl.headers)
        self.seeded_cookies = {
            (cookie.domain, cookie.path, cookie.name): cookie
            for cookie in self.cookie_jar
        }
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.sleeper = sleeper

    def fetch_html(
        self,
        url: str,
        *,
        stage: str,
        page_index: int | None = None,
        order_number: str | None = None,
    ) -> str:
        for attempt in range(COUPANG_REQUEST_ATTEMPTS):
            request = Request(url, headers=self.headers, method="GET")
            try:
                with self.opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    self.discard_replaced_seed_cookies()
                    self.refresh_cookie_header()
                    return decode_response_body(
                        body,
                        response.headers,
                        stage=stage,
                        page_index=page_index,
                        order_number=order_number,
                    )
            except HTTPError as exc:
                error = coupang_http_error(
                    exc,
                    stage=stage,
                    page_index=page_index,
                    order_number=order_number,
                )
                if error.retryable and attempt < COUPANG_REQUEST_ATTEMPTS - 1:
                    self.sleep_before_retry(attempt)
                    continue
                raise error from exc
            except (URLError, TimeoutError, SocketTimeout) as exc:
                if attempt < COUPANG_REQUEST_ATTEMPTS - 1:
                    self.sleep_before_retry(attempt)
                    continue
                raise coupang_timeout_error(
                    exc,
                    stage=stage,
                    page_index=page_index,
                    order_number=order_number,
                ) from exc

        raise CoupangRequestError("Coupang request failed unexpectedly", stage=stage)

    def sleep_before_retry(self, attempt: int) -> None:
        # 일시적인 네트워크 오류만 짧게 쉬었다가 다시 시도합니다.
        delay = COUPANG_RETRY_DELAYS_SECONDS[min(attempt, len(COUPANG_RETRY_DELAYS_SECONDS) - 1)]
        self.sleeper(delay)

    def cookie_header(self) -> str | None:
        cookies = [
            f"{cookie.name}={cookie.value}"
            for cookie in self.cookie_jar
            if not cookie.is_expired()
        ]
        return "; ".join(cookies) or None

    def refresh_cookie_header(self) -> None:
        # 쿠팡이 갱신한 쿠키도 다음 요청의 Cookie 헤더에 그대로 실어 보냅니다.
        cookie_header = self.cookie_header()
        if cookie_header:
            self.headers["Cookie"] = cookie_header

    def discard_replaced_seed_cookies(self) -> None:
        cookies = list(self.cookie_jar)
        current_by_key = {
            (cookie.domain, cookie.path, cookie.name): cookie
            for cookie in cookies
        }

        for key, seeded_cookie in list(self.seeded_cookies.items()):
            current_cookie = current_by_key.get(key)
            if current_cookie is not seeded_cookie:
                self.seeded_cookies.pop(key, None)
                continue

            if any(cookie.name == seeded_cookie.name and cookie is not seeded_cookie for cookie in cookies):
                # cURL에는 도메인 정보가 없으므로, 응답이 같은 이름의 실제 쿠키를 주면 임시 쿠키를 버립니다.
                self.cookie_jar.clear(
                    seeded_cookie.domain,
                    seeded_cookie.path,
                    seeded_cookie.name,
                )
                self.seeded_cookies.pop(key, None)


def fetch_order_list_html(
    *,
    page_index: int,
    client: CoupangHttpClient | None = None,
    parsed_curl: ParsedCurl | None = None,
    timeout_ms: int | None = None,
) -> str:
    http_client = client_or_new(client=client, parsed_curl=parsed_curl, timeout_ms=timeout_ms)
    return http_client.fetch_html(
        page_url(http_client.parsed_curl.url, page_index),
        stage="order_list",
        page_index=page_index,
    )


def fetch_order_detail_html(
    *,
    order_number: str,
    client: CoupangHttpClient | None = None,
    parsed_curl: ParsedCurl | None = None,
    timeout_ms: int | None = None,
) -> str:
    http_client = client_or_new(client=client, parsed_curl=parsed_curl, timeout_ms=timeout_ms)
    return http_client.fetch_html(
        order_detail_url(http_client.parsed_curl.url, order_number),
        stage="order_detail",
        order_number=order_number,
    )


def client_or_new(
    *,
    client: CoupangHttpClient | None,
    parsed_curl: ParsedCurl | None,
    timeout_ms: int | None,
) -> CoupangHttpClient:
    if client is not None:
        return client
    if parsed_curl is None or timeout_ms is None:
        raise ValueError("parsed_curl and timeout_ms are required when client is not provided")
    return CoupangHttpClient(parsed_curl=parsed_curl, timeout_ms=timeout_ms)


def cookie_header_from_headers(headers: dict[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "cookie":
            return value
    return None


def cookie_jar_from_headers(url: str, headers: dict[str, str]) -> CookieJar:
    jar = CookieJar()
    cookie_header = cookie_header_from_headers(headers)
    if not cookie_header:
        return jar

    # cURL에 들어 있던 쿠키를 브라우저 세션의 시작값처럼 CookieJar에 넣습니다.
    for name, value in parse_cookie_header(cookie_header):
        jar.set_cookie(make_cookie(url, name, value))
    return jar


def parse_cookie_header(cookie_header: str) -> list[tuple[str, str]]:
    try:
        simple_cookie = SimpleCookie()
        simple_cookie.load(cookie_header)
    except CookieError:
        simple_cookie = SimpleCookie()

    if simple_cookie:
        return [(morsel.key, morsel.value) for morsel in simple_cookie.values()]

    cookies: list[tuple[str, str]] = []
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            cookies.append((name, value))
    return cookies


def make_cookie(url: str, name: str, value: str) -> Cookie:
    parts = urlsplit(url)
    domain = parts.hostname or ""
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=parts.scheme == "https",
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def decode_response_body(
    body: bytes,
    headers,
    *,
    stage: str,
    page_index: int | None = None,
    order_number: str | None = None,
) -> str:
    # cURL의 Accept-Encoding을 그대로 보내므로 쿠팡의 압축 응답을 사람이 읽을 수 있는 HTML로 풉니다.
    for encoding in reversed(content_encodings(headers)):
        body = decompress_response_body(
            body,
            encoding,
            stage=stage,
            page_index=page_index,
            order_number=order_number,
        )

    charset = headers.get_content_charset() if headers else None
    return body.decode(charset or "utf-8", errors="replace")


def content_encodings(headers) -> list[str]:
    content_encoding = headers.get("content-encoding") if headers else None
    return [
        encoding.strip().lower()
        for encoding in (content_encoding or "").split(",")
        if encoding.strip()
    ]


def decompress_response_body(
    body: bytes,
    encoding: str,
    *,
    stage: str,
    page_index: int | None = None,
    order_number: str | None = None,
) -> bytes:
    try:
        if encoding in {"identity"}:
            return body
        if encoding in {"gzip", "x-gzip"}:
            return gzip.decompress(body)
        if encoding == "deflate":
            return decompress_deflate(body)
        if encoding == "br":
            import brotli

            return brotli.decompress(body)
        if encoding == "zstd":
            import zstandard

            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as reader:
                return reader.read()
    except ImportError as exc:
        raise CoupangResponseError(
            f"Coupang response uses unsupported content encoding: {encoding}",
            stage=stage,
            page_index=page_index,
            order_number=order_number,
            reason="unsupported_content_encoding",
        ) from exc
    except Exception as exc:
        raise CoupangResponseError(
            f"Failed to decode Coupang response content encoding: {encoding}",
            stage=stage,
            page_index=page_index,
            order_number=order_number,
            reason="content_encoding_decode_failed",
        ) from exc

    raise CoupangResponseError(
        f"Coupang response uses unknown content encoding: {encoding}",
        stage=stage,
        page_index=page_index,
        order_number=order_number,
        reason="unknown_content_encoding",
    )


def decompress_deflate(body: bytes) -> bytes:
    try:
        return zlib.decompress(body)
    except zlib.error:
        return zlib.decompress(body, -zlib.MAX_WBITS)


def coupang_http_error(
    exc: HTTPError,
    *,
    stage: str,
    page_index: int | None = None,
    order_number: str | None = None,
) -> CoupangRequestError:
    body = exc.read(4096)
    debug = response_debug_from_bytes(body, exc.headers)
    reason = debug.get("reason") or reason_from_status_code(exc.code)
    retryable = exc.code in RETRYABLE_HTTP_STATUS_CODES and exc.code not in BLOCKED_HTTP_STATUS_CODES
    return CoupangRequestError(
        request_error_message(stage=stage, status_code=exc.code, reason=reason),
        stage=stage,
        upstream_status_code=exc.code,
        page_index=page_index,
        order_number=order_number,
        reason=reason,
        title=debug.get("title"),
        content_type=debug.get("content_type"),
        snippet=debug.get("snippet"),
        retryable=retryable,
    )


def coupang_timeout_error(
    exc: URLError | TimeoutError | SocketTimeout,
    *,
    stage: str,
    page_index: int | None = None,
    order_number: str | None = None,
) -> CoupangTimeoutError:
    return CoupangTimeoutError(
        f"Coupang {stage} request timed out or failed: {exc}",
        stage=stage,
        page_index=page_index,
        order_number=order_number,
        reason="timeout_or_network_error",
    )


def request_error_message(*, stage: str, status_code: int, reason: str | None) -> str:
    reason_text = f", reason={reason}" if reason else ""
    return f"Coupang {stage} request failed (upstream_status={status_code}{reason_text})"


def response_debug_from_bytes(body: bytes, headers) -> dict[str, str]:
    charset = headers.get_content_charset() if headers else None
    text = body.decode(charset or "utf-8", errors="replace")
    debug = response_debug_from_html(text)
    content_type = headers.get("content-type") if headers else None
    if content_type:
        debug["content_type"] = content_type
    return debug


def response_debug_from_html(response_html: str) -> dict[str, str]:
    title = extract_title(response_html)
    snippet = response_html[:500]
    reason = classify_coupang_response(title=title, snippet=snippet)
    debug = {"snippet": snippet}
    if title:
        debug["title"] = title
    if reason:
        debug["reason"] = reason
    return debug


def extract_title(response_html: str) -> str | None:
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>",
        response_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not title_match:
        return None
    return re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip() or None


def classify_coupang_response(*, title: str | None, snippet: str | None) -> str | None:
    haystack = f"{title or ''} {snippet or ''}".lower()
    if any(word in haystack for word in ("captcha", "robot", "access denied", "akamai")):
        return "blocked_or_challenge"
    if any(word in haystack for word in ("login", "로그인", "sign in")):
        return "login_required"
    if any(word in haystack for word in ("too many requests", "rate limit")):
        return "rate_limited"
    return None


def reason_from_status_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "blocked_or_login_required"
    if status_code == 429:
        return "rate_limited"
    if status_code in RETRYABLE_HTTP_STATUS_CODES:
        return "temporary_upstream_error"
    return "upstream_http_error"


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
        raise CoupangResponseError(
            "Coupang response does not include orderList",
            stage="parse_order_list",
            reason="missing_order_list",
        )
    return [order for order in order_list if isinstance(order, dict)]


def extract_next_data(response_html: str) -> dict:
    match = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        response_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        # 차단/로그인/구조 변경 중 무엇인지 API 응답에서 바로 볼 수 있게 핵심 정보만 남깁니다.
        debug = response_debug_from_html(response_html)
        raise CoupangResponseError(
            "Coupang response does not include __NEXT_DATA__ "
            f"(length={len(response_html)}, title={debug.get('title')!r}, snippet={debug.get('snippet')!r})",
            stage="parse_next_data",
            reason=debug.get("reason") or "missing_next_data",
            title=debug.get("title"),
            snippet=debug.get("snippet"),
        )

    # Next.js JSON은 HTML script 안에 들어 있으므로 먼저 HTML entity만 원래 문자로 되돌립니다.
    try:
        data = json.loads(html.unescape(match.group(1)).strip())
    except json.JSONDecodeError as exc:
        raise CoupangResponseError(
            f"__NEXT_DATA__ is not valid JSON: {exc}",
            stage="parse_next_data",
            reason="invalid_next_data_json",
        ) from exc
    if not isinstance(data, dict):
        raise CoupangResponseError(
            "__NEXT_DATA__ is not a JSON object",
            stage="parse_next_data",
            reason="invalid_next_data",
        )
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

    discounted_unit_price = required_value(products[0], "discountedUnitPrice")
    return {
        "user_id": platform_account["user_id"],
        "product_name": str(required_value(order, "title")).strip(),
        "purchase_date": purchase_date_from_millis(required_value(order, "orderedAt")),
        "purchase_price_krw": discounted_unit_price,
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
