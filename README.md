# review_manager_mon

쿠팡 주문목록 SSR 페이지를 HTTP request로 호출해 Supabase `crawl_orders`에 적재하는 Python 배치 크롤러입니다.

## Run

의존성을 설치합니다.

```bash
uv sync
cp .env.example .env
```

Supabase `platform_accounts.curl`에는 Chrome 개발자도구에서 주문목록 요청을 `Copy cURL`로 복사한 전체 문자열을 저장해야 합니다.

로컬 실행:

```bash
uv run crawl-coupang \
  --platform-account-id "platform-account-uuid" \
  --max-pages 5
```

API 실행:

```bash
uv run uvicorn review_manager_mon.api.app:app --host 0.0.0.0 --port 8000
```

API 요청:

```bash
curl "http://127.0.0.1:8000/crawl/coupang?platform_account_id=platform-account-uuid&max_pages=5"
```

## Vercel Deploy

이 저장소는 루트 `app.py`와 `pyproject.toml`의 `app` 스크립트가 `review_manager_mon.api.app:app`을 가리키도록 구성되어 있어 Vercel FastAPI 배포 진입점으로 사용할 수 있습니다.

필수 환경 변수는 Vercel 프로젝트의 Settings > Environment Variables에 등록합니다.

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

선택 환경 변수:

- `CRAWL_MAX_PAGES`: 기본값 `5`
- `CRAWL_REQUEST_TIMEOUT_MS`: 기본값 `15000`

CLI 배포:

```bash
npm i -g vercel
vercel login
vercel link
vercel env add SUPABASE_URL production
vercel env add SUPABASE_SERVICE_ROLE_KEY production
vercel env add CRAWL_MAX_PAGES production
vercel env add CRAWL_REQUEST_TIMEOUT_MS production
vercel deploy
vercel deploy --prod
```

배포 후 확인:

```bash
curl "https://your-vercel-domain.vercel.app/health"
curl "https://your-vercel-domain.vercel.app/crawl/coupang?platform_account_id=platform-account-uuid&max_pages=5"
```

필수 환경 변수:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

선택 환경 변수:

- `CRAWL_MAX_PAGES`: 기본값 `5`
- `CRAWL_REQUEST_TIMEOUT_MS`: 기본값 `15000`

## Supabase MCP

Project ref:

```text
xhjjoxzwpgqlodflaiix
```

Codex MCP 설정은 저장소가 아니라 사용자 전역 설정 파일에 둡니다. Cursor는 `.cursor/mcp.json`을 사용할 수 있습니다.

## Test

```bash
uv run python -m unittest discover -s tests
```
