# Fofu Backend

Fofu iOS 프로토타입, 향후 Android 앱, 설치 없이 카메라로 QR을 여는 웹 클라이언트가
공유하는 FastAPI 백엔드입니다. 현재 iOS 화면의 하드코딩된 상태를 그대로 복제하지 않고,
식당 탐색부터 음식 여권, 장바구니, 한국어 주문표, 메시지, 점주 관리까지 공통 도메인 계약으로
구현했습니다.

> iOS의 계정·My Page·카탈로그·검색·Explore·메시지·음식 상세·네이티브 QR·장바구니·
> 주문표·점주 신청/대시보드는 현재 API에 연결되어 있습니다. QR 도착 후 표시할 반응형 웹
> 프런트엔드는 별도 연결 작업입니다.
> 주문표는 직원에게 보여 줄 준비된 주문 카드이며 POS 전송, 주방 주문 또는 결제가 아닙니다.

## 구현 범위

- 버전 API `/api/v1`과 일관된 오류·request ID 응답
- 익명/회원/Google 인증, Argon2 비밀번호, 짧은 JWT access token, 회전형 refresh token
- 주변 식당·검색·facet·트렌드·다국어 메뉴·리뷰·Explore 커서 피드
- 알레르겐·재료·식단 근거를 사용한 `compatible | conflict | unknown` 음식 여권 판정
- 서버 기준 가격·메뉴 revision·재고를 확인하는 장바구니와 멱등 주문표 생성
- 저장 식당, 프로필, 주문표 이력, 참여자 권한과 멱등 ID를 갖춘 메시지
- 인증 세션에 묶인 iOS APNs 디바이스와 메시지 transaction outbox·재시도 worker
- 비공개 사업자 파일, 점주 신청, membership 기반 대시보드·영업시간·메뉴 상태·감사 기록
- 별도 프로비저닝이 필요한 웹 관리자 화면과 점주 신청 검토·감사 조회
- 원문을 저장하지 않는 QR 코드, QR 전용 guest session, 부트스트랩, 발급·폐기
- SQLAlchemy 2 모델, Alembic migration, SQLite 로컬 실행, PostgreSQL 배포 구성
- Docker/Compose, OpenAPI, idempotent demo seed, 보안 기본 헤더와 로컬 QR/Google 인증 rate limit

설계 근거와 클라이언트 계약은 다음 문서에 있습니다.

- [iOS 화면 및 상태 분석](docs/IOS_ANALYSIS.md)
- [아키텍처와 확장 경계](docs/ARCHITECTURE.md)
- [설치 없는 QR 웹 흐름](docs/WEB_QR_FLOW.md)
- [iOS·Android·웹 공통 계약](docs/CLIENT_INTEGRATION.md)

## 구조

```text
app/
  api/          HTTP 라우터와 권한 경계
  schemas/      Pydantic 요청·응답 계약
  services/     인증, 카탈로그, 판정, 장바구니, 메시지, 점주 규칙
  google_identity.py  Google ID token 검증 adapter
  models.py     관계형 도메인 모델
  seed.py       SwiftUI 프로토타입 기반 idempotent demo data
migrations/     Alembic migration
scripts/        OpenAPI export, 관리자 계정 프로비저닝, 안전한 카탈로그 가져오기
tests/          설정·보안·유틸리티·API 테스트
docs/           분석, 아키텍처, 클라이언트 및 QR 계약
```

한 제품 도메인을 modular monolith로 유지해 모든 클라이언트가 같은 트랜잭션 규칙을 사용합니다.
로컬 adapter(SQLite, 파일 업로드, 프로세스 내 limiter)는 계약을 바꾸지 않고 PostgreSQL/PostGIS,
private object storage, Redis 또는 gateway로 교체할 수 있습니다.

## 로컬 실행

Python 3.10 이상이 필요합니다.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

기본 로컬 설정은 SQLite schema 자동 생성과 demo seed를 허용합니다. 그래도 위처럼 migration을
먼저 실행하면 배포 절차와 동일한 schema를 검증할 수 있습니다. 서버가 실행되면 다음 URL을
사용합니다.

- API 문서: <http://127.0.0.1:8000/api/v1/docs>
- OpenAPI JSON: <http://127.0.0.1:8000/api/v1/openapi.json>
- 관리자: <http://127.0.0.1:8000/admin>
- liveness: <http://127.0.0.1:8000/health/live>
- readiness: <http://127.0.0.1:8000/health/ready>

새 migration 적용 및 모델 drift 확인:

```bash
alembic upgrade head
alembic check
```

OpenAPI 파일을 생성하려면:

```bash
python scripts/export_openapi.py > openapi.json
```

## Google 로그인 설정

Google 로그인은 iOS 앱이 받은 Google ID token을 `POST /api/v1/auth/google`로 보내고, API가
서명·발급자·만료·audience와 확인된 이메일을 검증한 뒤 기존 `AuthResponse`를 발급하는 방식입니다.
클라이언트가 전달한 이메일이나 Google 사용자 ID를 별도 검증 없이 신뢰하지 않습니다. Kakao 로그인은
지원하지 않습니다.

Google Cloud의 같은 프로젝트에 다음 OAuth client를 만듭니다.

- iOS application client: bundle ID `im.fofu.fofu`에 연결합니다.
- Web application client: 백엔드 audience로 사용합니다. 이 전체 client ID를 iOS의
  `GIDServerClientID`와 API의 `FOFU_GOOGLE_OAUTH_CLIENT_IDS` 양쪽에 동일하게 설정합니다.

iOS target의 Debug/Release별 user-defined build setting 또는 환경별 `.xcconfig`에 다음 값을
지정합니다. `Fofu-Info.plist`는 이 값을 각각 `GIDClientID`, `GIDServerClientID`, callback URL
scheme으로 참조합니다.

```text
FOFU_API_BASE_URL = <environment API origin ending in /api/v1>
GOOGLE_SIGN_IN_IOS_CLIENT_ID = <full iOS OAuth client ID>
GOOGLE_SIGN_IN_SERVER_CLIENT_ID = <full Web application OAuth client ID>
GOOGLE_SIGN_IN_REVERSED_CLIENT_ID = <iOS client의 reversed client ID>
```

Debug에는 로컬/개발 API 주소를 사용할 수 있지만 Release의 `FOFU_API_BASE_URL`은 반드시 직접
운영하는 HTTPS origin으로 교체하십시오. 저장소의 Release 기본값은 의도적으로 유효하지 않은
placeholder라서 개발용 tunnel로 Google ID token을 보내지 않습니다.

`GOOGLE_SIGN_IN_REVERSED_CLIENT_ID`에는 Google Cloud에서 iOS client를 선택했을 때 표시되는
`REVERSED_CLIENT_ID`/iOS URL scheme 값을 그대로 사용합니다. `GoogleMapsAPIKey`는 지도·Places용
API key이므로 위 OAuth client ID 중 어느 것으로도 재사용하지 않습니다. Google의
[iOS 설정 안내](https://developers.google.com/identity/sign-in/ios/start-integrating)도 함께 확인하십시오.
OAuth client ID는 audience 식별자이며 client secret이 아닙니다. Google OAuth client secret은
iOS 앱이나 이 ID-token 교환 설정에 넣지 마십시오.

백엔드에는 허용할 ID-token audience를 JSON 배열로 설정합니다. 여러 앱/환경을 동시에 운영할 때만
검증된 client ID를 추가하고 wildcard 값은 사용하지 않습니다.

```dotenv
FOFU_GOOGLE_OAUTH_CLIENT_IDS=["<full Web application OAuth client ID>"]
```

설정을 바꾼 뒤 API를 재시작합니다. 이 배열이 비어 있으면 Google 로그인은 구성되지 않은 상태로
거부됩니다. ID token은 로그인 교환에만 사용하고 로그, `.env`, 저장소 또는 데이터베이스에
기록하지 마십시오. API는 Google의 안정적인 `sub`를 provider identity로 보관하며, 이미 다른
방식으로 가입된 같은 이메일을 자동 연결하지 않습니다. 로그인된 기존 계정은 Settings에서
Google 계정 하나를 명시적으로 연결할 수 있으며, 비밀번호 계정의 로그인 이메일은 이후 Google
profile 이메일이 바뀌어도 유지됩니다. QR 범위에서 연결할 때 iOS가 보내는
`replaced_refresh_token`은 같은 사용자에 속한 숨은 full-session backup만 원자적으로 폐기하며
다른 사용자의 token에는 영향을 주지 않습니다. 모든 인증 세션은 발급 당시 guest/member 상태에
묶이며, 승격 후에는 과거 또는 경합 중 뒤늦게 발급된 guest refresh token이 member credential로
바뀔 수 없습니다. 마이그레이션은 기존 `guest`/`qr_guest` 세션을 보수적으로 guest 발급으로
분류하므로, 기존 회원의 오래된 QR 세션은 배포 후 QR을 다시 스캔해야 할 수 있습니다.

## iOS 푸시 알림 설정

푸시는 기본적으로 꺼져 있고, 설정이 불완전한 상태에서는 디바이스를 저장한 척하지 않고
`push_not_configured`(503)를 반환합니다. Apple Developer에서 APNs Auth Key(`.p8`)를 발급한 뒤
키 파일은 저장소나 image에 넣지 말고 secret volume으로 mount하십시오.

```dotenv
FOFU_APNS_ENABLED=true
FOFU_APNS_ENVIRONMENT=sandbox
FOFU_APNS_TEAM_ID=<Apple Team ID>
FOFU_APNS_KEY_ID=<APNs Key ID>
FOFU_APNS_BUNDLE_ID=im.fofu.fofu
FOFU_APNS_PRIVATE_KEY_PATH=/run/secrets/fofu-apns-auth-key.p8
```

기본 `compose.yaml`은 이 값들을 pass-through하지만 APNs를 비활성화한 채 시작하므로 별도 secret
파일이 없어도 로컬 실행이 깨지지 않습니다. Compose에서 활성화할 때는 배포 전용 override의
`secrets`/read-only volume으로 `.p8`를 container에 mount하고, 그 container 내부 절대 경로를
`FOFU_APNS_PRIVATE_KEY_PATH`로 전달해야 합니다. host 경로나 키 원문을 compose 파일에 직접 넣지
마십시오.

Debug 개발 앱의 token은 `sandbox`, TestFlight/App Store 앱의 token은 `production`입니다. 운영 API는
`FOFU_APNS_ENVIRONMENT=production`이 아니면 시작을 거부합니다. 같은 API 배포에 서로 다른 APNs
환경을 섞지 마십시오.

정회원의 full iOS session만 `PUT /api/v1/push/devices/{installation_id}`로 token을 등록할 수
있습니다. 익명·QR session은 짧게 만료되므로 등록할 수 없고, 정회원이 QR 화면에 들어가도 기존
full-session binding을 유지합니다. 로그아웃과 session 폐기는 binding과 대기 delivery를 함께
비활성화합니다. `DELETE`는 APNs가 꺼져 있어도 항상 실행할 수 있으며 이미 해제된 설치에도 204를
반환합니다. 한 사용자는 기본 10개의 활성 설치만 허용되고 PUT은 별도 로컬 limiter를 거칩니다.
다중 API replica에서는 gateway/Redis 기반 공유 limiter도 적용하십시오.

새 메시지는 message와 같은 DB transaction에 디바이스별 outbox row로 기록됩니다. 각 API replica의
worker는 PostgreSQL `SKIP LOCKED`와 만료 lease를 사용해 중복 claim을 막습니다. 네트워크·429는
제한된 지수 backoff, 모든 APNs 5xx는 최소 15분 뒤 재시도하고, 만료 token은 새 등록 시각을 확인한
뒤 비활성화합니다. SQLite는 local/test 단일 process용입니다. 잠금화면과 APNs/outbox에는 메시지
원문이나 발신자 이름을 넣지 않고 generic 문구와 `conversation_id`, `message_id`만 넣습니다. 앱은
알림을 탭한 뒤 인증된 메시지 API에서 내용을 다시 읽어야 합니다. worker는 terminal delivery를
기본 30일 보존한 뒤 시간당 최대 1,000개씩 정리하며, `pending`/`processing` row는 이 보존 정리의
대상이 아닙니다. 적체량·최고 대기 시간과 정리 지연을 운영 metric으로 감시하십시오.

## Demo 데이터

`FOFU_SEED_DEMO_DATA=true`인 local/test 환경에서만 사용하는 값입니다. 운영에서는 반드시 seed를
끄고 아래 자격 증명과 QR을 사용하지 마십시오.

| 용도 | 값 |
|---|---|
| 음식 여권이 설정된 사용자 | `demo@fofu.app` / `fofu-demo-password` |
| Halmoni's Table 점주 | `owner@fofu.app` / `fofu-demo-password` |
| 공개 개발 QR 원문 | `halmoni-table-demo` |

빠른 확인:

```bash
curl -sS http://127.0.0.1:8000/api/v1/restaurants
curl -i http://127.0.0.1:8000/q/halmoni-table-demo
curl -sS http://127.0.0.1:8000/api/v1/qr/halmoni-table-demo
curl -sS -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@fofu.app","password":"fofu-demo-password","client_type":"ios"}'
```

### 명시적 QA 데이터 프로필

기본 demo seed는 위의 핵심 식당과 계정만 유지합니다. 저장 장소·주문 이력·리뷰 공개 여부와 식당의
영업/검증/공개 상태 조합을 수동 또는 E2E 테스트하려면, migration을 적용한 **local/test DB**에
별도 QA 프로필을 명시적으로 설치합니다.

```bash
cd backend
source .venv/bin/activate
alembic upgrade head
python -m scripts.seed_qa
```

이 명령은 먼저 기존 core demo fixture를 보장한 다음 다음과 같은 QA 전용 레코드를 추가합니다.

- 공개됐지만 휴무인 검증 식당, 공개됐지만 미검증인 영업 식당, 비공개 초안 식당 각 1개
- 식당별 가상 메뉴와 공개 리뷰 1개·비공개 리뷰 1개
- `demo@fofu.app`에서 확인할 저장 식당 3개와 준비된 주문 이력 2개

모든 QA 상호·주소·리뷰에는 가상 테스트 데이터임을 명시하며 실제 사업자 정보를 사용하지 않습니다.
ID는 버전이 포함된 결정적 UUID이고 삽입 전용으로 동작하므로 같은 명령을 반복해도 중복되지 않으며,
동일 ID로 이미 존재하는 행이나 그 밖의 사용자 데이터는 수정하거나 삭제하지 않습니다. QA 프로필은
기본 서버 시작이나 `FOFU_SEED_DEMO_DATA`에 연결되어 있지 않고, `staging`과 `production` 환경에서는
실행을 거부합니다. 실행 전 `FOFU_DATABASE_URL`이 의도한 로컬/테스트 DB인지 반드시 확인하십시오.

### 식당·메뉴 JSON 가져오기

실제 상호를 SQL로 직접 넣거나 웹에서 수집하지 말고, 출처·사용 허가를 확인한 데이터를 JSON
매니페스트로 정리한 뒤 가져옵니다. 저장소에는 형식 확인용
[`examples/catalog.fictional.json`](examples/catalog.fictional.json)만 있으며 모두 가상 데이터입니다.

점주 신청을 승인하려면 먼저 대상 식당이 DB에 있어야 합니다. 신규 식당 온보딩은 다음 순서로
진행합니다.

1. 아래 가져오기로 식당과 메뉴를 `is_published: false` 상태로 등록합니다.
2. 점주가 앱에서 그 식당을 선택해 owner application을 제출합니다.
3. 관리자가 `/admin`의 **Owner applications**에서 서류와 대상 식당을 확인하고 승인합니다. 승인은
   owner 권한과 검증 상태를 부여하지만 식당을 자동 공개하지 않습니다.
4. 점주가 메뉴와 영업 상태를 확인한 뒤, 관리자가 **Restaurants**에서 검증·영업 상태를 최종
   확인하고 `published`로 전환합니다.

이렇게 하면 점주 신청이 임의의 새 DB 행을 만들지 않고 검토된 카탈로그와 명확하게 연결되며,
승인과 공개 결정을 서로 분리할 수 있습니다.

```bash
cd backend
source .venv/bin/activate
alembic upgrade head

# 기본 동작: 전체 JSON과 DB 참조/제약을 확인한 뒤 트랜잭션을 롤백
python -m scripts.import_catalog examples/catalog.fictional.json

# 위 dry-run 결과를 확인한 경우에만 반영
python -m scripts.import_catalog examples/catalog.fictional.json --apply
```

매니페스트 v1의 구조는 다음과 같습니다.

```text
schema_version: 1
ingredients[]: code, name_en, name_ko?, emoji?
allergens[]: code, name_en, name_ko?
restaurants[]:
  slug, name_en, category, address_en, latitude, longitude
  handle?, 번역?, 영업시간?, 공개/검증/영업 상태?, 이미지?
  menu_categories[]:
    slug, name_en, 정렬/활성 상태?, 번역?
    items[]:
      slug, name_en, price_amount?
      설명/통화/맵기/재고/이미지/번역?
      ingredients[]?, allergens[]?, dietary_claims[]?
```

식당은 `slug`, 카테고리와 메뉴는 식당 범위의 `slug`, 분류 사전은 `code`를 안정 키로 사용합니다.
기존 행은 ID를 유지한 채 명시된 필드만 갱신하고, 매니페스트에서 생략한 식당·카테고리·메뉴·연결
행은 삭제하지 않습니다. 따라서 메뉴 중단은 행을 빼는 대신 `is_available: false`, 카테고리 중단은
`is_active: false`, 식당 비공개는 `is_published: false`로 명시합니다. 기존 점주 연결, 저장 장소,
리뷰, 주문 등 사용자 데이터도 건드리지 않습니다.

CLI는 JSON 전체를 먼저 엄격히 검증하고, 모든 변경을 한 트랜잭션에서 처리합니다. `--apply`가 없으면
항상 롤백하며, `FOFU_ENVIRONMENT=local|test`에서만 실행됩니다. `staging`과 `production`에서는
`--apply` 여부와 관계없이 거부하므로 운영 반영은 검토·백업·승인 절차가 있는 별도 배포 작업으로
만들어야 합니다. 실제 가게 정보에는 사업자 동의, 이미지/메뉴 저작권, 주소·전화번호의 최신성,
알레르겐 정보의 출처와 검증 상태를 함께 관리하십시오.

### 지역 SQLite 가져오기 (제주·서울·경기)

지역별 장소·메뉴 SQLite를 현재 `FOFU_DATABASE_URL` 카탈로그에 연결하려면 공통 가져오기를
사용합니다. `jeju`는 `databases/jeju_full.sqlite3`와 `databases/menus_jeju.sqlite3`, `seoul`은
`databases/seoul_full.sqlite3`와 `databases/menus_seoul.sqlite3`, `gyeonggi`는
`databases/gyeonggi_full.sqlite3`와 `databases/menus_gyeonggi.sqlite3`를 기본 원본으로
선택합니다. 원본은 읽기 전용으로 열며, `--apply`가 없는 실행은 전체 검증과 변경 계산 후 항상
롤백합니다.

```bash
# 원본 무결성, 지역 경계, 장소/메뉴 키와 대상 DB 변경 내용을 확인
python -m scripts.import_region_catalog jeju
python -m scripts.import_region_catalog seoul
python -m scripts.import_region_catalog gyeonggi

# dry-run 결과를 확인한 뒤 현재 local/test DB에 한 지역씩 반영
python -m scripts.import_region_catalog jeju --apply
python -m scripts.import_region_catalog seoul --apply
python -m scripts.import_region_catalog gyeonggi --apply
```

기존 제주 명령 `python -m scripts.import_jeju_catalog [--apply]`도 같은 제주 가져오기를 호출하는
호환 진입점으로 유지됩니다. 별도 원본을 검증할 때는 지역 다음에 장소 DB와 메뉴 DB 경로를 차례로
지정할 수 있습니다.

장소는 `discoveries.region`이 선택 지역이고 `in_region=1`인 행만 가져옵니다. 따라서 제주 가게
16,792곳과 서울 원본 191,414곳 중 서울 경계 안의 127,012곳만 지도 좌표 검색에 노출되며,
경계 밖 서울 수집 행 64,402곳은 제외됩니다. 경기 원본은 전체 349,651곳 중 경계 안의 155,432곳만
가져오고 경계 밖 194,219곳은 제외합니다. 이 지역 장소 ID 집합과 `crawl_jobs` ID 집합이 정확히
같지 않으면 가져오기를 중단합니다.

가게·메뉴 ID, 가게 slug와 handle은 지역과 Kakao 키로 결정되므로 반복 실행에 안정적이고, 같은
Kakao ID가 제주·서울·경기 원본에 있어도 충돌하지 않습니다. 기존 제주 결정적 ID는 호환성을 위해
그대로 유지합니다. 메뉴는 실제 항목이 있는 `menus`를 우선하고, 빈 그룹뿐이면 항목이 있는
`yogiyo_menus`로 대체하며 픽업 소스는 제외합니다. 가게 대표 사진은 선택된 메뉴 소스를 먼저 쓰고,
그 소스에 사진이 없을 때만 다른 허용 소스 사진으로 대체합니다. 메뉴 가격은 1원 이상 500,000원
이하일 때 금액과 함께 가져옵니다. 가격이 없거나 0원·음수·상한 초과·비정수처럼 신뢰할 수 없는
값이어도 메뉴 이름과 설명은 유지하고, 가격만 미상으로 정규화해 가져옵니다. 가격 미상 메뉴는
카탈로그에는 노출하지만 가격 필터와 주문에서는 제외하며, importer 결과에는 비정상 가격을
정규화한 수를 별도로 기록합니다. `http` 이미지 주소는 `https`로
정규화합니다.

반복 실행은 기존 ID와 점주·평점·리뷰·저장 식당 같은 사용자 연결을 보존하며 원본에서 생략된 기존
행을 삭제하지 않습니다. 이 명령은 JSON 가져오기와 마찬가지로 `local`과 `test` 환경에서만
실행됩니다. 운영 반영은 검토된 배포 절차와 백업을 거쳐 별도로 수행해야 합니다. 원본에는 영업시간과
평점이 없으므로 가져온 가게는 미검증 상태이며, 식단·알레르겐 정보도 추정해서 생성하지 않습니다.
원본 SQLite에 비어 있지 않은 WAL 또는 rollback journal이 있으면 일반 읽기 전용 모드로 해당
트랜잭션 내용을 포함하며, 이를 무시하는 immutable 모드로 대체하지 않습니다. sidecar가 없거나 비어
있는 정적 스냅샷만 immutable 읽기로 열어 WAL 모드 헤더 때문에 불필요한 sidecar를 만들지 않습니다.

## 관리자 계정과 `/admin`

기본 또는 demo 관리자 계정은 만들지 않습니다. migration이 적용된 대상 DB에 별도 관리자 계정을
프로비저닝한 뒤, 서버와 같은 origin의 `/admin`에서 해당 이메일과 비밀번호로 로그인합니다.

```bash
cd backend
source .venv/bin/activate
alembic upgrade head
python -m scripts.create_admin \
  --email admin@example.com \
  --display-name "Fofu Admin"
uvicorn app.main:app --reload
```

CLI가 비밀번호와 확인 값을 화면에 표시하지 않고 입력받습니다. 관리자 비밀번호는 12~128자여야
합니다. 자동화 환경에서는 prompt 대신 secret manager가 주입한 `FOFU_ADMIN_PASSWORD`를 사용할
수 있습니다. 비밀번호를 command 인자로 넘기거나 저장소·shell history·배포 로그에 기록하지
마십시오.

같은 이메일이 이미 있으면 새 계정을 중복 생성하지 않고 기존 역할을 보존한 채 `admin` 역할을
추가합니다. 이때 비밀번호를 새 입력값으로 교체하고 계정을 활성 회원 상태로 전환하며, 기존의
모든 활성 세션을 폐기하므로 `/admin`에서 다시 로그인해야 합니다. CLI는 현재 `.env` 또는 실행
환경의 `FOFU_DATABASE_URL`이 가리키는 DB만 변경하므로 운영 실행 전 대상 DB를 반드시 확인하십시오.

**Restaurants** 화면에서는 `published`, `verified`, `open` 상태를 각각 확인하고 전환할 수 있습니다.
각 변경은 확인창을 거쳐 `PATCH /api/v1/admin/restaurants/{restaurant_id}`로 저장되고 감사 로그에
이전·새 상태가 기록됩니다. 이 작업은 승인된 owner/manager 연결을 변경하지 않습니다. 공개 전환 뒤
목록이 자동 새로고침되며, 네트워크 오류가 있었다면 **Refresh**로 서버의 최종 상태를 다시 확인합니다.

관리자 화면은 일반 사용자 화면보다 피해 범위가 큽니다. 인터넷에 공개하기 전에 HTTPS와
gateway 로그인 rate limit을 적용하고, 가능하면 IAP/VPN 및 MFA 같은 별도 접근 통제를 앞단에
두십시오. 관리자 비밀번호와 DB 자격 증명은 secret manager에서 관리하고 감사 이벤트와 비정상
로그인을 모니터링해야 합니다.

브라우저 관리자는 일반 웹 로그인 cookie를 공유하지 않습니다. `/admin`은 same-origin 전용
`/api/v1/admin/auth/*`와 `HttpOnly; SameSite=Strict` 관리자 cookie를 사용하며 access token은
페이지 메모리에만 둡니다. 관리자 역할 계정의 일반 `client_type=web` 로그인은 거부됩니다.
reverse proxy를 사용할 때는 외부 HTTPS scheme과 host가 애플리케이션에 정확히 전달되도록 trusted
proxy 설정을 제한해서 구성하십시오.

## API 영역

전체 요청·응답 schema와 상태 코드는 Swagger/OpenAPI를 기준으로 확인합니다.

| 영역 | 대표 경로 |
|---|---|
| 인증 | `/api/v1/auth/anonymous`, `/register`, `/login`, `/google`, `/refresh`, `/logout` |
| 탐색·검색 | `/api/v1/restaurants?lat=...&lng=...`, `/search`, `/search/facets`, `/explore` |
| 식당·메뉴·리뷰 | `/api/v1/restaurants/{id-or-slug}`, `/menu`, `/menu-items/{id}` |
| 음식 여권·저장 | `/api/v1/me/passport`, `/me/saved-restaurants`, `/me/orders` |
| 장바구니·주문표 | `/api/v1/cart`, `/api/v1/cart/order-card` |
| 메시지 | `/api/v1/conversations`, `/conversations/{id}/messages` |
| 푸시 알림 | `PUT`, `DELETE /api/v1/push/devices/{installation_id}` |
| 점주·미디어 | `/api/v1/owner/...`, `/api/v1/media/uploads` |
| 관리자 | `/admin`, `/api/v1/admin/...` |
| QR | `/q/{code}`, `/api/v1/qr/{code}`, `/guest-sessions/qr`, `/sessions/current/bootstrap` |

인증 요청은 `Authorization: Bearer <access_token>`을 사용합니다. 웹 refresh token은
`HttpOnly; SameSite=Lax` cookie로만 전달하고, iOS/Android refresh token은 Keychain/Keystore에
보관합니다. 클라이언트는 오류 문구가 아니라 `error.code`로 분기해야 합니다.

음식 판정의 `compatible`은 의학적 안전 보증이 아닙니다. 근거가 없거나 교차 접촉 가능성만
확인되면 `unknown`이며, 모든 클라이언트는 식당 확인 안내와 근거를 함께 보여야 합니다.

## 설치 없는 QR 웹 흐름

인쇄할 값은 `https://api.example.com/q/<고엔트로피-코드>` 형태의 HTTPS URL입니다. QR은 로그인
자격 증명이 아니라 폐기 가능한 공개 locator이며 서버 DB에는 SHA-256 digest만 저장됩니다.

```text
휴대폰 기본 카메라
  -> GET /q/{code}
  -> 307 https://web.example.com/r/{slug}#qr={code}
  -> 웹이 POST /api/v1/guest-sessions/qr
  <- QR 범위 guest session + 식당/메뉴/장바구니 bootstrap
  -> history.replaceState로 fragment 제거
```

fragment는 웹 서버와 Referer에 전달되지 않습니다. 각 스캔은 별도 사용자·세션·장바구니를 만들고,
QR session은 해당 식당의 공개 메뉴와 본인 장바구니에만 접근합니다. 점주 권한, 공동 테이블 주문,
결제, POS 전송 권한은 생기지 않습니다. 실제 브라우저 화면과 `getUserMedia` 기반 웹 스캐너는 향후
웹 프런트엔드에서 구현해야 하며 카메라 frame을 이 API에 업로드할 필요는 없습니다.

상세 위협 모델과 운영 설정은 [WEB_QR_FLOW.md](docs/WEB_QR_FLOW.md)를 참조하십시오.

## 테스트와 품질 검사

```bash
pytest
ruff check app tests scripts migrations
python -m compileall -q app tests scripts migrations
alembic check
```

## Docker Compose

PostgreSQL 17, migration, API와 demo seed를 함께 실행합니다.

```bash
docker compose up --build
docker compose down
```

Compose는 개발 편의를 위한 고정 DB 비밀번호와 local JWT secret을 사용합니다. 그대로 외부에
노출하거나 운영 배포에 재사용하지 마십시오.

AWS의 단일 저비용 EC2에서 private RDS와 연결해 운영하는 절차와 별도 production Compose는
[AWS EC2 production deployment](docs/AWS_EC2_DEPLOYMENT.md)에 있습니다. 운영 구성은 SSM
SecureString에서 DB URL과 JWT secret을 실행 시점에 주입하며, HTTPS proxy, readiness check,
재시작 정책과 container log rotation을 포함합니다.

### 로컬 카탈로그 PostgreSQL

제주·서울·경기 카탈로그만 적재한 격리 PostgreSQL 17은 기본 Homebrew PostgreSQL과 충돌하지
않도록 로컬 Unix socket과 포트 `55432`를 사용합니다. `.env`는 이 DB를 가리키며 demo seed와
자동 schema 생성을 비활성화합니다.

```bash
scripts/local_catalog_postgres.sh status
scripts/local_catalog_postgres.sh start
scripts/local_catalog_postgres.sh stop
```

검증된 RDS 복원용 custom-format dump는
`backups/fofu_catalog_pg17_20260816.dump`에 생성합니다. 이 파일과 로컬 PostgreSQL data directory는
저장소 및 Docker build context에서 제외합니다.

## 주요 환경 변수

전체 로컬 예시는 [.env.example](.env.example)에 있습니다.

| 변수 | 의미 |
|---|---|
| `FOFU_ENVIRONMENT` | `local`, `test`, `staging`, `production` |
| `FOFU_DATABASE_URL` | SQLAlchemy URL; 운영은 `postgresql+psycopg://...` |
| `FOFU_JWT_SECRET` | token과 privacy digest용 비밀값 |
| `FOFU_GOOGLE_OAUTH_CLIENT_IDS` | Google ID token에서 허용할 정확한 OAuth audience JSON 배열 |
| `FOFU_APNS_ENABLED` | APNs sender/outbox worker 활성화; 기본 `false` |
| `FOFU_APNS_ENVIRONMENT` | `sandbox` 또는 `production`; 운영은 반드시 `production` |
| `FOFU_APNS_TEAM_ID`, `FOFU_APNS_KEY_ID` | Apple provider-token 서명 식별자 |
| `FOFU_APNS_BUNDLE_ID` | APNs topic; 현재 iOS bundle ID `im.fofu.fofu` |
| `FOFU_APNS_PRIVATE_KEY_PATH` | 저장소 밖에 mount한 APNs `.p8` secret의 절대 경로 |
| `FOFU_PUSH_*` | worker batch/lease/retry와 사용자별 활성 디바이스 상한 |
| `FOFU_CORS_ORIGINS` | credential을 허용할 정확한 웹 origin JSON 배열 |
| `FOFU_WEB_APP_BASE_URL` | `/q/{code}`가 redirect할 웹 앱 기준 URL |
| `FOFU_PUBLIC_API_BASE_URL` | 인쇄 QR과 SVG에 들어갈 공개 API 기준 URL |
| `FOFU_UPLOAD_DIR` | 비공개 업로드 경로; 기존 디렉터리는 권한이 `0700`이어야 함 |
| `FOFU_AUTO_CREATE_SCHEMA` | 로컬/test bootstrap 전용 |
| `FOFU_SEED_DEMO_DATA` | 로컬/test demo data 전용 |
| `FOFU_ADMIN_PASSWORD` | 관리자 CLI 자동화용 일회성 비밀번호; API runtime 설정에는 보관하지 않음 |

`staging`/`production` 설정은 짧거나 기본인 secret, 비 PostgreSQL DB, HTTP 공개 URL,
wildcard/HTTP CORS, schema 자동 생성, demo seed를 시작 단계에서 거부합니다.

## 운영 전 체크리스트

- PostgreSQL에 `alembic upgrade head`를 별도 release 단계에서 적용
- 충분히 긴 무작위 `FOFU_JWT_SECRET`과 secret manager 사용, demo seed 비활성화
- 환경별 Google OAuth client ID를 분리하고 `FOFU_GOOGLE_OAUTH_CLIENT_IDS`를 정확한 audience로 제한
- APNs `.p8`를 secret manager에서 mount하고 Team/Key/Bundle/environment 조합을 실제 기기로 검증
- push delivery 재시도·영구 실패·invalid token 수와 outbox 적체를 metric/alert로 구성
- API·웹 URL과 CORS origin을 실제 HTTPS host로 고정하고 ingress에서 HSTS 적용
- reverse proxy의 trusted forwarded-header 범위를 제한하고 QR/auth 경로 access log를 redaction
- QR/Google 인증 limiter를 gateway/Redis 기반 공유 limiter로 교체하고 password login·register·upload에도 정책 적용
- 사업자 서류를 암호화된 private object storage로 이동하고 malware scan·보존/삭제 정책 적용
- DB backup/PITR, migration rollback 절차, 구조화 로그·metric·trace·보안 alert 구성
- 정적 QR 분실·복제 대응을 위한 폐기/재발급 운영 절차와 scan anomaly alert 구성
- 개인정보 보존 기간, 사업자 문서 접근·삭제, 사용자 탈퇴 정책 확정
- Google Maps 키를 iOS bundle ID와 허용 API로 제한하고 웹/서버 키와 분리·교체
- 결제/POS를 추가한다면 현재 주문표와 분리된 onsite proof, 결제 사업자, webhook 검증,
  주문 상태 machine 및 별도 권한 모델을 설계

## 다음 클라이언트 작업

1. 웹 `/r/{slug}` 화면이 fragment를 교환하고 즉시 지운 뒤 bootstrap으로 메뉴를 그리게 합니다.
2. Android/웹 클라이언트를 추가할 때 OpenAPI에서 DTO와 API client를 생성해 iOS와 같은 계약을
   사용합니다.
3. 배포용 HTTPS API 주소, private object storage, WebSocket 메시지 갱신을 연결합니다.
4. 실제 기기 E2E에서 401 rotation, 409 menu revision/availability, QR 만료, cursor와 localization을
   반복 검증합니다.
