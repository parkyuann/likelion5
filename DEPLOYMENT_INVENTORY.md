# 배포 인벤토리 — 20260827

## 이번 commit 포함 범위

- FastAPI `/api/auth/*` cookie session 계약 및 PostgreSQL/Redis 연결 코드
- frontend의 same-origin `/api` 호출과 `credentials: include`
- API + Nginx(frontend multi-stage build) application overlay
- 외부 Docker network `kosis_shadow_internal` 참조 설정
- CPU 기본 requirements와 optional GPU requirements 분리
- vendored auth OpenAPI, migration read-only preflight 및 적용 절차
- 분석·검색 route의 `SEARCH_ADAPTER_PENDING` fail-closed gate

## 보존 및 금지

Compose는 PostgreSQL, OpenSearch, Qdrant, Redis service나 data volume을 선언하지 않는다. 기존 EBS, index, collection, release pointer를 수정하지 않는다. 이번 작업에서 Compose 기동, migration, 적재, 재색인, 재임베딩, release activation, GPU 실행을 하지 않는다.

## 검증 결과

- 승인 설계 SHA-256: `99A715C565625C7E25369584F9C9EA10080A51330B134FBCE38F1D0A3EA55576` 유지.
- backend contract tests: `25 passed` (동일 timestamp 6회 로그인 순서 회귀 포함).
- Python `compileall backend`: 통과.
- frontend `npm ci`: 25 packages, known vulnerability 0.
- frontend `npm run check` (cmd 경유): lint warning 1건, production build 통과(32 modules).
- `docker compose -f deploy/compose.yaml config`: 통과. service는 `api`, `nginx`뿐이며 external `kosis_shadow_internal`만 사용.
- API image에는 `backend/`만 포함하며 미연결 legacy `src/`를 포함하지 않는다. 운영 source에서 auth Bearer/access token, SQLite auth/catalog fallback, localhost Qdrant는 0건이다.
- `git diff --check`: 통과.
- 변경 전 canonical suite: `1886 passed / 19 failed / 1 skipped` (719.71초). 기존 19건은 runtime pin, audit runtime, L2 canary dependency SHA 관련이며 이 checkout 변경 전 존재했다.
- 변경 후 동일 canonical suite: `1893 passed / 12 failed / 1 skipped` (293.43초). 신규 실패 0건, 기존 통과 감소 0건이며 7개 audit-runtime timing 사례가 이번 재실행에서는 통과했다. 남은 12건은 변경 전에도 존재한 runtime pin/approval preflight/L2 canary dependency SHA 실패다.

검증 commit SHA는 commit 생성 후 기록한다. 실제 PostgreSQL/Redis/EC2 E2E는 이번 범위에서 실행하지 않았다.

## PENDING

- `PENDING_APPLICATION_SCHEMA_RECONCILIATION`: 기존 정본 migration/컬럼 미공유. 현재 DDL 없음.
- `PENDING_OAUTH_CALLBACK_CONTRACT`: OAuth route와 query-token flow 제거. callback/domain 미확정.
- `SEARCH_ADAPTER_PENDING`: PostgreSQL metadata reader/OpenSearch/Qdrant adapter 미공유.
- `PENDING_AWS_GRID_DRIVER_ACCESS`
- `PENDING_GPU_HOST_PREFLIGHT`
- `PENDING_ENCODER_RERANKER_ENABLEMENT`
- `PENDING_HTTPS_DOMAIN_CERTIFICATE`
- `PENDING_IMAGE_REGISTRY_DIGEST`

## 이후 연결 위치

- application 정본 schema 수령: `backend/database.py` repository SQL과 `backend/migrations/` 대조 후 additive migration 추가
- 검색 설계 확정: `backend/runtime_gate.py` gate 해제 조건과 별도 search adapter 구현
- 전체 pipeline source 수령: `/api/v1/analyze`, `/api/v1/verify/develop` service wiring
- 새 검색 산출물: 새 index/collection 이름, `release_id`, document contract, analyzer/model revision, manifest·receipt를 함께 기록하고 승인 전 alias/current pointer를 유지

현재 `standard-v1`, `whitespace-v1`, 기존 Qdrant collection은 비교 기준으로 보존하며 결과를 합치지 않는다.
