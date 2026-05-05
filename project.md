# review_manager_mon 프로젝트 문서

## 프로젝트 개요

`review_manager_mon`은 쿠팡 주문목록/주문상세 URL을 HTTP request로 호출해서 Supabase의 `crawl_orders` 테이블에 저장하는 Python 배치 크롤러입니다.

주요 작업 흐름은 다음과 같습니다.

1. Supabase에서 `platform_accounts` 행을 조회합니다.
2. 크롤링을 시작하면 해당 `platform_accounts.status`를 `true`로 바꿉니다.
3. `platform_accounts.curl`에 저장된 Chrome Copy cURL 문자열을 파싱합니다.
4. 쿠팡 주문목록 URL에 pageIndex를 0부터 붙여 request를 보냅니다.
5. 응답 HTML의 `script#__NEXT_DATA__` JSON에서 `orderList`를 추출합니다.
6. 이미 `crawl_orders` 또는 `orders`에 존재하는 주문번호는 제외합니다.
7. 단일 상품 주문은 주문상세 URL을 호출해서 결제수단 텍스트를 추출합니다.
8. `coupang_payment_method_mappings`에 등록된 결제수단이면 `payment_method_id`로 변환합니다.
9. 단일 상품 주문만 `crawl_orders`에 저장하고, 여러 상품 주문은 skip합니다.
10. 중복 주문번호가 발견된 페이지 또는 최대 페이지에 도달하면 종료합니다.
11. 크롤링이 정상 종료되거나 실패하면 해당 `platform_accounts.status`를 `false`로 되돌립니다.
12. 처리 결과를 JSON으로 출력합니다.

## 기술 스택

- Python 3.9 이상
- uv
- Python 표준 라이브러리 기반 HTTP request
- FastAPI
- Uvicorn
- Supabase REST API
- GitHub Actions 수동 실행 워크플로

## 디렉터리 구조

```text
.
├── README.md
├── app.py
├── plans.md
├── project.md
├── pyproject.toml
├── request.txt
├── response.txt
├── src/
│   └── review_manager_mon/
│       ├── api/
│       │   └── app.py
│       ├── cli/
│       │   ├── args.py
│       │   └── crawl_coupang.py
│       ├── coupang/
│       │   ├── config.py
│       │   ├── parsers.py
│       │   ├── request_crawler.py
│       │   └── runner.py
│       ├── db/
│       │   └── supabase_rest.py
│       └── utils/
│           └── env.py
├── supabase/
│   └── migrations/
│       ├── 202605040001_create_crawl_orders_and_platform_accounts.sql
│       ├── 202605050001_add_curl_to_platform_accounts.sql
│       ├── 202605050002_create_coupang_payment_method_mappings.sql
│       ├── 202605050003_recreate_coupang_payment_method_mappings.sql
│       └── 202605050004_add_status_to_platform_accounts.sql
└── tests/
    ├── test_args.py
    ├── test_env.py
    ├── test_parsers.py
    └── test_request_crawler.py
```

## 주요 파일 역할

### CLI

- `src/review_manager_mon/cli/crawl_coupang.py`
  - `crawl-coupang` 명령의 진입점입니다.
  - CLI 인자를 파싱하고 `run_crawler()`를 실행합니다.
  - 성공 시 결과를 JSON으로 출력하고, 실패 시 에러 메시지를 표준 에러에 출력한 뒤 종료 코드 `1`로 종료합니다.

- `src/review_manager_mon/cli/args.py`
  - 필수 인자 `--platform-account-id`를 받습니다.
  - 선택 인자 `--max-pages`를 받습니다. 기본값은 `CRAWL_MAX_PAGES` 또는 5입니다.

### API

- `src/review_manager_mon/api/app.py`
  - FastAPI 앱 진입점입니다.
  - `GET /health`로 서버 상태를 확인합니다.
  - `GET /crawl/coupang`에서 `platform_account_id`, `max_pages` query parameter를 받아 기존 `run_crawler()`를 실행합니다.
  - Vercel 상태 확인이 크롤러 import 문제와 같이 실패하지 않도록 `run_crawler()`는 크롤링 요청 시점에만 불러옵니다.

- `app.py`
  - Vercel이 기본 FastAPI 진입점으로 찾을 수 있도록 `review_manager_mon.api.app:app`을 다시 내보냅니다.

### 쿠팡 크롤러

- `src/review_manager_mon/coupang/runner.py`
  - 환경 설정, Supabase REST 클라이언트 생성, 플랫폼 계정 조회를 담당합니다.
  - 크롤링 실행 전 `platform_accounts.status`를 `true`로 바꾸고, 성공/실패와 상관없이 종료 시 `false`로 되돌립니다.
  - 조회한 `platform_accounts` 행과 DB helper를 `run_request_crawl()`에 전달합니다.

- `src/review_manager_mon/coupang/config.py`
  - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CRAWL_REQUEST_TIMEOUT_MS`, `CRAWL_MAX_PAGES`를 읽어 `CrawlerConfig`로 만듭니다.

- `src/review_manager_mon/coupang/request_crawler.py`
  - `platform_accounts.curl` 문자열을 파싱합니다.
  - pageIndex별 쿠팡 주문목록 request를 보냅니다.
  - 주문번호별 쿠팡 주문상세 request를 보내 결제수단 셀 텍스트를 읽습니다.
  - `__NEXT_DATA__` JSON에서 `orderList`를 추출합니다.
  - 주문 JSON을 `crawl_orders` insert payload로 바꿉니다.
  - 결제수단 텍스트를 DB 매핑값으로 바꿔 `payment_method_id`에 넣습니다.
  - 같은 주문 안에 서로 다른 상품이 여러 개 있으면 저장하지 않고 skip합니다.
  - `orderedAt` 밀리초 Unix timestamp를 KST 날짜로 변환해서 `purchase_date`에 넣습니다.

- `src/review_manager_mon/coupang/parsers.py`
  - 기존 한국어 가격, 날짜, 주문번호, URL 정규화 helper입니다.
  - 현재 request 기반 주문목록 흐름의 핵심 경로에서는 `request_crawler.py`가 직접 JSON 값을 사용합니다.

### Supabase 연동

- `src/review_manager_mon/db/supabase_rest.py`
  - Python 표준 라이브러리 `urllib`로 Supabase REST API를 호출합니다.
  - 플랫폼 계정 조회, 플랫폼 계정 크롤링 상태 업데이트, 기존 주문번호 조회, 쿠팡 결제수단 매핑 조회, 크롤링 주문 저장을 담당합니다.

## 데이터베이스

- `supabase/migrations/202605040001_create_crawl_orders_and_platform_accounts.sql`
  - `platform_accounts`, `crawl_orders` 테이블과 index/RLS를 만듭니다.
  - 기존 migration에는 과거 로그인 기반 구조의 `login_id`, `login_password_encrypted` 컬럼이 남아 있습니다.

- `supabase/migrations/202605050001_add_curl_to_platform_accounts.sql`
  - `platform_accounts.curl text` 컬럼을 추가합니다.
  - 크롤러는 이 컬럼에 저장된 전체 cURL 문자열을 사용합니다.

- `supabase/migrations/202605050002_create_coupang_payment_method_mappings.sql`
  - 쿠팡 주문상세 페이지의 결제수단 텍스트와 `payment_methods.id`를 연결하는 `coupang_payment_method_mappings` 테이블을 만듭니다.
  - 기본키는 자동 증가하는 `id`이고, 쿠팡 결제수단 텍스트인 `payment_method_name`은 중복을 막기 위해 unique로 관리합니다.
  - 초기 매핑값은 `쿠페이 머니`, `현대카드 / 일시불`, `쿠팡와우카드(KB국민) / 일시불`입니다.

- `supabase/migrations/202605050003_recreate_coupang_payment_method_mappings.sql`
  - 이미 `payment_method_name` primary key 구조로 만들어진 `coupang_payment_method_mappings` 테이블을 drop한 뒤 다시 만듭니다.
  - 새 구조는 자동 증가 `id`를 primary key로 쓰고, `payment_method_name`은 unique로 유지합니다.
  - 테이블 재생성 후 기본 결제수단 매핑 3개를 다시 insert합니다.

- `supabase/migrations/202605050004_add_status_to_platform_accounts.sql`
  - `platform_accounts.status boolean not null default false` 컬럼을 추가합니다.
  - 값이 `true`이면 해당 계정의 크롤링이 실행 중이고, `false`이면 실행 중이 아닙니다.

## 필드 매핑

- `user_id`: `platform_accounts.user_id`
- `product_name`: `orderList[].title`
- `purchase_date`: `orderList[].orderedAt`을 KST 날짜로 변환
- `purchase_price_krw`: 단일 상품의 `unitPrice`
- `product_url`: `null`
- `order_number`: `orderList[].orderId`
- `platform_id`: `platform_accounts.platform_id`
- `payment_method_id`: 주문상세 페이지의 결제수단 텍스트를 `coupang_payment_method_mappings.payment_method_id`로 변환한 값입니다. 매핑이 없으면 `null`입니다.
- `buyer_account_id`: `platform_accounts.buyer_account_id`
- `crawl_order_status`: `0`

## 실행 준비

### 1. 의존성 설치

```bash
uv sync
cp .env.example .env
```

### 2. 환경 변수 설정

필수:

- `SUPABASE_URL`: Supabase 프로젝트 URL입니다.
- `SUPABASE_SERVICE_ROLE_KEY`: Supabase service role key입니다. 서버/배치 실행 환경에서만 사용해야 합니다.

선택:

- `CRAWL_MAX_PAGES`: 주문 목록에서 탐색할 최대 페이지 수입니다. 기본값은 `5`입니다.
- `CRAWL_REQUEST_TIMEOUT_MS`: 쿠팡 request timeout입니다. 기본값은 `15000`입니다.

### 3. cURL 저장

쿠팡 주문목록 페이지 요청을 Chrome 개발자도구에서 `Copy cURL`로 복사한 뒤, 전체 문자열을 Supabase `platform_accounts.curl`에 저장합니다.

요청 URL은 다음 형태여야 합니다.

```text
https://mc.coupang.com/ssr/desktop/order/list?pageIndex=0
```

크롤러는 저장된 URL의 다른 query는 유지하고 `pageIndex`만 0, 1, 2 순서로 바꿉니다.

주문상세 URL은 같은 cURL의 도메인과 헤더를 사용해서 다음 형태로 호출합니다.

```text
https://mc.coupang.com/ssr/desktop/order/{order_number}
```

결제수단은 주문상세 HTML에서 `table > tbody.sc-97871ab4-1.gbbDZu > tr:nth-child(2) > th > div` 위치의 텍스트를 읽습니다.

## 크롤러 실행 방법

```bash
uv run crawl-coupang \
  --platform-account-id "platform-account-uuid" \
  --max-pages 5
```

## API 실행 방법

```bash
uv run uvicorn review_manager_mon.api.app:app --host 0.0.0.0 --port 8000
```

요청 예시:

```bash
curl "http://127.0.0.1:8000/crawl/coupang?platform_account_id=platform-account-uuid&max_pages=5"
```

`max_pages`를 생략하면 CLI와 동일하게 `CRAWL_MAX_PAGES` 또는 기본값 `5`를 사용합니다.

성공 시 표준 출력으로 다음 형태의 JSON을 반환합니다.

```json
{
  "platformAccountId": "platform-account-uuid",
  "requestedPages": [0],
  "discoveredCount": 0,
  "insertedCount": 0,
  "skippedDuplicateCount": 0,
  "skippedMultiProductCount": 0,
  "failedCount": 0,
  "inserted": [],
  "skipped": [],
  "skippedMultiProduct": [],
  "failed": []
}
```

## GitHub Actions 실행

`.github/workflows/crawl-coupang.yml`에 수동 실행 워크플로가 있습니다.

GitHub Actions 입력값:

- `platform_account_id`
- `max_pages`

필요한 GitHub Secrets:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## 테스트 실행

```bash
uv run python -m unittest discover -s tests
```

## Vercel 배포 방법

루트 `app.py`는 Vercel이 FastAPI 앱 객체를 찾기 위한 배포 진입점입니다. 실제 API 로직은 기존 `src/review_manager_mon/api/app.py`를 그대로 사용합니다.

### 1. Vercel CLI 준비

```bash
npm i -g vercel
vercel login
vercel link
```

### 2. 환경 변수 등록

필수:

```bash
vercel env add SUPABASE_URL production
vercel env add SUPABASE_SERVICE_ROLE_KEY production
```

선택:

```bash
vercel env add CRAWL_MAX_PAGES production
vercel env add CRAWL_REQUEST_TIMEOUT_MS production
```

Preview 배포에서도 같은 값을 쓰려면 `production` 대신 `preview`로도 등록합니다.

### 3. Preview 배포와 확인

```bash
vercel deploy
curl "https://배포주소.vercel.app/health"
```

정상 응답:

```json
{"status":"ok"}
```

### 4. Production 배포와 크롤링 호출

```bash
vercel deploy --prod
curl "https://운영주소.vercel.app/crawl/coupang?platform_account_id=platform-account-uuid&max_pages=5"
```

크롤링 요청은 외부 쿠팡 페이지와 Supabase를 호출하므로 실행 시간이 길어질 수 있습니다. Vercel의 요청 제한 안에 들어오도록 `max_pages`를 작게 시작해서 확인합니다.

테스트 대상:

- CLI 인자 파싱
- `.env` 로드
- 기존 parser helper
- cURL 파싱
- pageIndex URL 생성
- 주문상세 URL 생성
- `response.txt` 기반 `orderList` 추출
- 주문상세 HTML 기반 결제수단 텍스트 추출
- `orderedAt` KST 날짜 변환
- 단일 상품 주문 payload 생성
- 결제수단 매핑값을 `payment_method_id`에 반영
- 여러 상품 주문 skip
- 중복 발견 페이지에서 수집 종료
- 크롤링 시작/종료 시 `platform_accounts.status` 업데이트
- 크롤링 실패 시에도 `platform_accounts.status`를 `false`로 복구

## 개발 시 주의사항

- 코드를 수정하기 전 이 문서를 먼저 확인해서 이미 있는 기능과 helper를 재사용합니다.
- 코드 수정이 생기면 변경된 구조, 실행 방법, 환경 변수, 데이터 흐름을 이 문서에 반영합니다.
- 새 코드를 작성할 때는 비개발자도 이해할 수 있도록 한국어 주석을 작성합니다.
- 단일 용도로만 쓰이는 기능은 불필요한 추상화를 만들지 않습니다.
- Supabase service role key는 로컬 `.env`, GitHub Secrets 같은 서버 측 비밀 저장소에서만 사용합니다.
- 쿠팡 cURL에는 민감한 쿠키가 포함되므로 로그에 출력하지 않습니다.

## 중복 방지 기준

기능 추가 전 먼저 확인할 위치:

- 환경 변수 추가 또는 읽기 방식 변경: `src/review_manager_mon/coupang/config.py`, `src/review_manager_mon/utils/env.py`
- CLI 인자 추가: `src/review_manager_mon/cli/args.py`
- API 요청 파라미터 추가: `src/review_manager_mon/api/app.py`
- 쿠팡 주문목록 request/파싱 변경: `src/review_manager_mon/coupang/request_crawler.py`
- 쿠팡 주문상세 결제수단 request/파싱 변경: `src/review_manager_mon/coupang/request_crawler.py`
- Supabase REST 호출 추가: `src/review_manager_mon/db/supabase_rest.py`
- DB 구조 변경: `supabase/migrations/`

## 현재 알려진 제약

- `platform_accounts.curl`에 저장된 쿠팡 쿠키가 만료되면 request가 실패하거나 `orderList`를 찾지 못할 수 있습니다.
- `GET /crawl/coupang`은 DB에 주문을 저장하는 요청이므로 외부 공개용 엔드포인트로 열지 않는 것을 전제로 합니다.
- 쿠팡의 `__NEXT_DATA__` 구조가 바뀌면 파서 수정이 필요할 수 있습니다.
- 쿠팡 주문상세 HTML의 결제수단 셀렉터가 바뀌면 `extract_payment_method_name()` 수정이 필요할 수 있습니다.
- 한 주문에 서로 다른 상품이 여러 개 있으면 사용자 답변 기준에 따라 저장하지 않고 skip합니다.
- 크롤러는 목록 중 이미 저장된 주문번호가 발견된 페이지까지만 탐색합니다.
