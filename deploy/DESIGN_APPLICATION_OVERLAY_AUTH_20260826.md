# Application overlay 및 인증 통합 설계 — 20260826

상태: 구현 전 승인 대상

## 1. 범위와 비범위

이번 변경은 API, Nginx, frontend 정적 배포로 구성한 application overlay와 인증 경로 정리만 포함한다. PostgreSQL, OpenSearch, Qdrant, redis-session 및 EBS volume은 기존 EC2 자산을 외부 Docker network `kosis_shadow_internal`에서 참조한다. Compose에서 이 데이터 서비스를 선언하거나 초기화하지 않는다.

전체 pipeline source, PostgreSQL metadata reader, OpenSearch BM25 adapter, Qdrant dense adapter, encoder, reranker는 새로 추정해 구현하지 않는다. 운영 모드에서 기존 로컬 SQLite/v5/sample 검색으로 폴백하지 않고 `SEARCH_ADAPTER_PENDING`으로 fail-closed한다.

## 2. 배포 토폴로지

- `api`: FastAPI application image. 외부 network `kosis_shadow_internal`에 연결한다.
- `nginx`: multi-stage frontend build artifact를 정적으로 제공하는 Nginx image. `/api/`를 `api`로 reverse proxy하며 prefix를 보존한다. Compose service는 `api`, `nginx` 두 개뿐이고 frontend build는 `nginx` image build stage다.
- 별도 데이터 서비스, named data volume, migration job은 두지 않는다.
- API는 비 root 사용자, read-only root filesystem, tmpfs `/tmp`, `no-new-privileges`로 실행한다.
- Nginx는 외부에 노출되는 유일한 컨테이너다. API port는 `expose`만 사용한다.

## 3. 인증 계약

공개 경로는 `/api/auth/signup`, `/api/auth/login`, `/api/auth/me`, `/api/auth/logout`, `/api/auth/logout-all`이다. 기존 비인증 application API는 `/api/v1/*`로 옮겨 Nginx가 `/api` prefix를 제거하지 않고 전달한다.

- 사용자 정본: 기존 PostgreSQL `application` DB의 `users`, `auth_accounts`.
- 비밀번호: 최소 12자, Argon2id. 평문·PBKDF2 신규 생성 금지.
- 세션: `redis-session`의 opaque random session ID. 브라우저에는 `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, 고정 7일 `Max-Age` cookie만 두며 `Domain` 속성을 설정하지 않는다.
- 세션 TTL: 발급 시점부터 고정 604800초. 요청 시 연장하지 않는다.
- 사용자당 최대 세션: 5개. Redis Lua 단일 연산으로 새 session key와 사용자별 sorted set을 만들고, 6번째 발급 시 score가 가장 작은 session을 제거해 동시 로그인에서도 최신 5개만 남긴다.
- CSRF: 운영의 모든 POST/PUT/PATCH/DELETE에서 `Origin`을 우선 검증하고 없으면 `Referer` origin을 검증한다. 둘 다 없거나 허용 origin과 다르면 403이다. `Sec-Fetch-Site`는 `same-origin` 또는 `same-site`만 허용하며 헤더가 없거나 다른 값이면 403이다. 로그인·회원가입도 cookie를 발급/계정 생성하므로 검사한다. 테스트는 ASGI client에서 실제 헤더를 보낸다.
- `/me`는 cookie 세션만 읽는다. Authorization/Bearer는 인증 수단으로 해석하지 않는다.
- 회원가입은 계정만 만들며 자동 로그인하지 않는다. 로그인만 세션 cookie를 발급한다. 응답 user는 `id`, `primary_email`, `display_name`, `status`, `created_at`, `last_login_at` 계약을 따른다.
- 로그아웃은 현재 session을 제거하고 cookie를 만료시킨다. `logout-all`은 해당 사용자 세션을 모두 제거한다.
- 정지/탈퇴 계정은 로그인 및 기존 세션 사용을 거부한다.
- localStorage 사용자/토큰 snapshot, URL query token 전달은 제거한다. frontend는 모든 API 요청에 `credentials: "include"`를 사용한다.
- auth 오류는 OpenAPI대로 `{code, message}`만 반환한다. auth 422도 FastAPI 기본 detail 배열 대신 같은 DTO로 변환하며 user/session 응답에 계약 외 필드를 넣지 않는다.

전달된 OpenAPI에는 signup의 CSRF 403 응답이 누락돼 있지만 상위 런타임 계약은 모든 POST 검사를 요구한다. 원본 전달 파일은 변경하지 않고 `contracts/auth-session-v1.yaml`로 vendor한 사본에 signup 403을 추가해 구현 계약으로 사용하며, 차이를 문서화한다.

OAuth callback 값과 provider 운영 계약은 미확정이다. 기존 OAuth router 등록과 frontend 버튼 동작을 제거하고, query-token 및 자동 이메일 병합 구현은 삭제한다. `PENDING_OAUTH_CALLBACK_CONTRACT`가 해소될 때 state를 server session에 결박한 별도 설계로 다시 추가한다.

## 4. PostgreSQL 및 migration 경계

API runtime은 schema를 만들거나 migration을 실행하지 않는다. migration source와 별도 실행 절차만 Git에 둔다. `application_runtime`은 DML만 수행하며 DDL 권한을 요구하지 않는다.

현재 공유 자료가 기존 정본 migration의 전체 컬럼을 확정하지 않았으므로 실행 가능한 DDL을 추정해 만들지 않는다. Git에는 요구 컬럼·제약·권한과 `information_schema` 대조 절차를 담은 migration source README 및 read-only preflight query만 포함한다. 정본 migration을 전달받은 뒤 additive migration 번호를 배정하는 작업은 `PENDING_APPLICATION_SCHEMA_RECONCILIATION`이다.

애플리케이션 SQL은 PostgreSQL placeholder와 JSONB를 사용한다. SQLite 파일 생성, SQLite schema bootstrap, SQLite auth session table, SQLite `rowid` 의존은 제거한다. 대화·메시지·즐겨찾기도 같은 PostgreSQL repository를 사용하며, schema reconciliation 전에는 해당 repository가 명시적으로 503을 반환한다.

## 5. 검색 및 release 보존 계약

- 선택 가능한 BM25 기준은 환경변수 `KOSIS_OPENSEARCH_INDEX` 하나이며 초기값은 기존 `standard-v1` shadow index다.
- `standard-v1`과 `whitespace-v1` 결과를 합치지 않는다.
- OpenSearch analyzer, index document schema, Qdrant payload/vector schema, BM25/dense/reranker fusion과 평가 기준은 검색 담당자가 확정한다.
- 기존 index/collection을 수정·삭제·재색인·재임베딩하지 않는다.
- 새 산출물은 새 이름으로 만들고 `release_id`, document contract revision, analyzer/model revision, manifest 및 receipt를 연결해야 한다.
- 최종 선택 전 alias/current pointer를 변경하지 않는다.
- API가 계약 미구현 상태에서 로컬 검색으로 폴백하지 않도록 운영 모드의 `/tables`, `/analyze`, `/analyze/image`, `/verify/develop`, `POST /favorites` table lookup 등 모든 분석·검색 진입점은 503 fail-closed한다. OCR-only 공개 API는 이번 범위에서 별도로 만들지 않는다.

연결 지점은 `backend/search_adapter.py`의 명시적 interface로 제한한다. 이번 변경에는 실제 OpenSearch/Qdrant 호출 구현을 넣지 않는다. 공용 `backend/runtime_gate.py` dependency를 검색 의존 route에 직접 부착하고, service 내부 `table_catalog_service`도 같은 gate를 호출하는 이중 경계로 우회를 막는다.

## 6. GPU와 cache

encoder/reranker는 기본 비활성이며 GPU 의존성은 API 기본 requirements에서 분리한다. 다음 상태를 그대로 기록한다.

- `PENDING_AWS_GRID_DRIVER_ACCESS`
- `PENDING_GPU_HOST_PREFLIGHT`
- `PENDING_ENCODER_RERANKER_ENABLEMENT`

`redis-cache`는 기본 비활성이며 `redis-session` URL/키 namespace와 공유하지 않는다.

## 7. 구현 파일 경계

- 인증/DB: `backend/app.py`, `backend/auth_dependencies.py`, `backend/auth_service.py`, `backend/database.py`, 관련 서비스 SQL 및 migration source.
- 검색 경계: `backend/runtime_gate.py` 공용 dependency를 `backend/app.py`의 `/api/v1/tables`, `/api/v1/analyze`, `/api/v1/analyze/image`, `/api/v1/verify/develop`, `POST /api/v1/favorites`에 부착하고 `backend/table_catalog_service.py`에도 service-level gate를 둔다. favorite 조회/삭제는 저장된 application 데이터만 사용하므로 검색 gate 대상이 아니다.
- frontend: `frontend/src/api.js`, `frontend/src/auth.jsx`, Vite `/api` proxy.
- 배포: `deploy/compose.yaml`, API/frontend Dockerfile, Nginx config, env example, README/inventory.
- 의존성: CPU API 기본 requirements와 optional GPU requirements 분리.
- 테스트: 외부 EC2 서비스 없이 fake PostgreSQL/Redis로 auth cookie, CSRF, TTL, 세션 제한, no-Bearer/no-query-token을 검증한다. 검색 우회별로 tables/analyze/analyze-image/verify/favorite-add가 모두 503이며 underlying service가 호출되지 않았음을 각각 검증한다.

## 8. 설계 역할 분리 기록

- 작성 task/context: `01a03291-2da6-7ad2-b952-11d983838f6a` (현재 주 task)
- challenger agent/task: `01a03e7c-2085-7023-b0cb-55b2bdc40a7c`
- 이전 반려 approval agent/task: `01a03e84-733e-7162-8b69-234632ca4414`, `01a03e89-41f7-7c93-ba2d-c71cfb802445`
- 유효 승인자는 위 ID들과 다른 새 agent/task여야 한다.

## 9. 승인 전 수용 기준

1. Compose config에 `api`, `nginx` 두 서비스만 있고 external network만 참조한다. data volume과 외부 데이터 service `depends_on`은 0건이다.
2. 저장소 운영 frontend/backend에서 localStorage token, URL query token, HTTP Bearer auth, SQLite 사용자·세션 경로가 0건이다. 외부 OAuth provider API 호출의 access token 헤더는 이 검사에서 별도 분류한다.
3. `/api/auth/*` 경로, 상태, `{code,message}` 오류, 제한된 응답 DTO, Set-Cookie가 OpenAPI 계약 테스트를 통과한다.
4. Redis 세션 7일 고정 TTL과 최대 5개 원자 제한 테스트가 통과한다.
5. Origin/Referer 및 Sec-Fetch-Site CSRF 허용/거부 테스트가 통과한다.
6. frontend lint/build가 통과한다.
7. Python compile/import 및 신규 backend 테스트가 통과한다.
8. 루트 canonical suite는 기존 기준선 `1409 passed / 1 skipped`와 비교하고, 이 delivery checkout에는 기존 tracked test가 없다는 사실을 별도로 기록한다. 변경 전 루트 기준선을 실측할 수 없으면 환경 실패를 숨기지 않는다.
9. migration, compose up, 데이터 적재/색인/임베딩, release activation, GPU runtime은 실행하지 않는다.
