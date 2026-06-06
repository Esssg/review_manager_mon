from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from review_manager_mon.api.logging_config import configure_server_logging


configure_server_logging()

app = FastAPI(title="review_manager_mon")
# 운영 웹과 로컬 개발 서버에서 API를 직접 호출할 때 브라우저 preflight 요청을 허용합니다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://review-manager.jinitlab.com",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
logger = logging.getLogger("review_manager_mon.api")


@app.middleware("http")
async def log_http_request(request: Request, call_next):
    started_at = time.perf_counter()
    client_host = request.client.host if request.client else "-"
    request_target = str(request.url.path)
    if request.url.query:
        request_target = f"{request_target}?{request.url.query}"

    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        # 라우트 처리 중 예외가 나도 어떤 요청에서 실패했는지 나중에 txt 로그로 추적할 수 있게 남깁니다.
        logger.exception(
            "request_failed client=%s method=%s path=%s elapsed_ms=%.2f",
            client_host,
            request.method,
            request_target,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    # 정상 응답과 404 같은 스캐닝 요청을 같은 형식으로 남겨 빈번한 외부 접근을 집계하기 쉽게 합니다.
    logger.info(
        "request_done client=%s method=%s path=%s status=%s elapsed_ms=%.2f",
        client_host,
        request.method,
        request_target,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/crawl/coupang")
def crawl_coupang(
    platform_account_id: str = Query(..., min_length=1),
    max_pages: Optional[int] = Query(None, ge=1),
) -> dict:
    try:
        # 상태 확인 API는 크롤러 의존성과 분리하고, 실제 크롤링 요청에서만 실행 코드를 불러옵니다.
        from review_manager_mon.coupang.runner import run_crawler

        # CLI 인자와 같은 이름으로 묶어서 기존 크롤러 실행 흐름을 그대로 재사용합니다.
        return run_crawler(
            SimpleNamespace(
                platform_account_id=platform_account_id,
                max_pages=max_pages,
            )
        )
    except Exception as exc:
        from review_manager_mon.coupang.request_crawler import CoupangCrawlerError

        if isinstance(exc, CoupangCrawlerError):
            # 쿠팡 응답 문제는 500으로 숨기지 않고, 어느 단계에서 막혔는지 JSON으로 알려줍니다.
            raise HTTPException(status_code=exc.http_status_code, detail=exc.to_dict()) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
