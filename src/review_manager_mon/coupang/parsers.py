from __future__ import annotations

import re
from urllib.parse import urlparse


def parse_korean_price(value: str | int | float) -> int | float:
    normalized = re.sub(r"[^\d.-]", "", str(value))
    if not normalized:
        raise ValueError(f"Invalid price: {value}")

    parsed = float(normalized) if "." in normalized else int(normalized)
    return parsed


def parse_korean_date(value: str) -> str:
    match = re.search(r"(\d{4})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", str(value).strip())
    if not match:
        raise ValueError(f"Invalid purchase date: {value}")

    year, month, day = match.groups()
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def parse_order_number(value: str) -> str | None:
    match = re.search(r"\b\d{10,30}\b", str(value))
    return match.group(0) if match else None


def parse_order_number_from_url(value: str | None) -> str | None:
    path = urlparse(str(value or "")).path
    match = re.search(r"/order/(\d{10,30})(?:/|$)", path)
    return match.group(1) if match else None


def normalize_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        return "https://www.coupang.com" + text
    return text
