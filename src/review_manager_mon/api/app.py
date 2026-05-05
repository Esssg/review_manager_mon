from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from review_manager_mon.coupang.runner import run_crawler


app = FastAPI(title="review_manager_mon")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/crawl/coupang")
def crawl_coupang(
    platform_account_id: str = Query(..., min_length=1),
    max_pages: Optional[int] = Query(None, ge=1),
) -> dict:
    try:
        # CLI 인자와 같은 이름으로 묶어서 기존 크롤러 실행 흐름을 그대로 재사용합니다.
        return run_crawler(
            SimpleNamespace(
                platform_account_id=platform_account_id,
                max_pages=max_pages,
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
