# 배포 인벤토리 — 20260827

## 이번 구현 범위

이번 변경은 기존 EC2 데이터 계층을 생성·초기화·변경하지 않는 application overlay 연결 코드다.

- `backend/metadata_repository.py`: `KOSIS_METADATA_DATABASE_URL` 전용 PostgreSQL read-only metadata reader. `default_transaction_read_only=on`과 정본 `statistics_table` 13개 컬럼만 사용하며 DDL/DML은 없다.
- 기존 auth 구현: `001_application_auth` 정본의 local provider/provider_user_id/provider_email/Argon2id 정합화와 Redis session lifecycle 구현 완료. auth에 남은 유일한 운영 검증은 live EC2 E2E다.
- `backend/search_adapter.py`: `standard-v1` 단일 OpenSearch BM25 read adapter와 내부 전용 Qdrant dense read adapter. mapping, release, source/hash, vector/payload authority를 fail-closed 검증한다.
- `backend/search_adapter.py`: OpenSearch exact mapping 뒤 release `_count`, Qdrant concrete collection inventory·alias 거부·release count, PostgreSQL metadata public read의 선행 release attestation을 모두 통과해야 결과를 반환한다.
- `backend/table_catalog_service.py`: 빈 query의 PostgreSQL browse와 비어 있지 않은 query의 BM25 후보 검색·metadata hydration을 분리한다. 최종 통계값·진위 판정은 반환하지 않는다.
- `backend/runtime_gate.py`, `backend/app.py`: `/api/v1/tables`만 metadata/BM25 경로를 사용하고, conversations/favorites는 `APPLICATION_PRODUCT_STATE_PENDING`, analyze/verify/image는 `PIPELINE_RUNTIME_PENDING`으로 닫는다. unsafe route는 CSRF 검사를 먼저 수행한다.
- `backend/app.py`: `/api/v1/tables` response model에 release, pagination, organization relation과 candidate 최소 필드를 명시한다.
- `deploy/runtime.env.example`, `.env.example`: EC2 연결 환경변수 이름과 release/analyzer/index/collection pin 예시를 기록한다.
- `tests_backend/test_search_adapters.py`, `tests_backend/test_overlay_runtime.py`, `tests_backend/test_overlay_contract_static.py`, `tests_backend/test_auth_schema_alignment.py`: adapter, release fail-closed, 세션 lifecycle과 capability gate 회귀 테스트.

## 승인된 설계

- `deploy/DESIGN_EC2_READONLY_ADAPTER_AUTH_V1_20260827.md`
- SHA-256: `3D1F9D70A781930EF007D4660B617CD1B03BD299F0407670E6C150D8D63DFC15`
- 독립 설계 승인: `gpt-5.6-sol`, agent `01a04175-92ef-7cb3-9f7b-9de00d8688de`, `2026-08-27T13:27:17+09:00`

## OpenAPI provenance

- upstream handoff ZIP의 `auth-session-v1.yaml`: `85DFD7B51AABA1166AC48417C86F341BD32694D2DAF3854A5B8ADCD37528CF28`
- 저장소 runtime `contracts/auth-session-v1.yaml`의 실제 Git tree blob(LF) SHA-256:
  `589A0E24282BBDEC86D40F5CD3CFD8667B7537E448CB422C419AEE6FDE4FC357`
- Windows working tree 또는 `git archive`의 checkout 변환 결과(CRLF) SHA-256:
  `A8605566EBFA77BBB87BA6F045AEDE701CB8B678A99CDBCB5A151288C0B141F8`
- 테스트는 Windows checkout(CRLF)과 EC2/Linux checkout(LF)에서 동일한 Git 계약 내용을
  검증하기 위해 LF로 정규화한 뒤 실제 Git tree blob SHA를 확인한다.
- 두 계약의 exact delta는 runtime 사본에 signup `403 Forbidden` 응답을 추가한 것뿐이다.

## 기존 EC2 연결 환경변수

실제 secret은 Git에 넣지 않고 EC2 server-only runtime env에 주입한다.

```text
APPLICATION_DATABASE_URL              application DB runtime role
APPLICATION_SCHEMA_STATUS=VERIFIED
APPLICATION_SCHEMA_REVISION=001_application_auth
KOSIS_METADATA_DATABASE_URL           kosis_metadata reader role
REDIS_SESSION_URL                     redis-session only
OPENSEARCH_URL
KOSIS_RELEASE_ID=kosis_canonical_20260821_full_r3_13ko_views
KOSIS_BM25_ANALYZER=standard-v1
KOSIS_BM25_INDEX                      concrete standard-v1 index
QDRANT_URL
QDRANT_COLLECTION                    existing dense collection
QDRANT_VECTOR_SIZE=1024
QDRANT_RECEIPT_SHA256                 operator receipt pin
KOSIS_REDIS_CACHE_ENABLED=false
BGE_QUERY_ENCODER_ENABLED=false
BGE_RERANKER_ENABLED=false
```

`KOSIS_OPENSEARCH_INDEX` fallback은 사용하지 않는다. `standard-v1`과 `whitespace-v1` 결과를 합치지 않으며 alias/current pointer도 변경하지 않는다.

## 구현 및 운영 경계

- PostgreSQL metadata, OpenSearch, Qdrant는 읽기 전용 adapter로만 접근한다.
- OpenSearch 결과는 `table_key`, `release_id`, `source`, `score`, evidence를 포함한 후보다.
- Qdrant는 public text route가 없고 `search_by_vector(vector, fields, limit)` 내부 인터페이스만 제공한다.
- Compose 기동, migration 실행, 데이터 적재·재색인·재임베딩, release 활성화, BGE runtime enablement는 수행하지 않았다.
- 기존 EC2 PostgreSQL/OpenSearch/Qdrant/redis-session/EBS와 external network `kosis_shadow_internal`은 보존한다.

## PENDING

- `002_application_product_state`: conversations/messages/favorites migration과 runtime 연결
- HTTPS/domain/certificate 및 Secure cookie 실서비스 검증
- BGE encoder/reranker runtime enablement (`BGE_*_ENABLED=false` 유지)
- redis-cache 비교실험 및 활성화 여부 (`KOSIS_REDIS_CACHE_ENABLED=false` 유지)
- retrieval-contract v2: OpenSearch receipt/settings·mapping attestation, Qdrant receipt 원문/sample attestation, dense encoder 연결
- 실제 EC2 PostgreSQL/OpenSearch/Qdrant/Redis E2E readiness

## 검증

- Python backend import/compileall: 통과
- backend focused tests: `33 passed / 1 warning`.
- all `tests_backend`: `52 passed / 1 warning`.
- frontend `npm run check`: 성공, production build `32 modules`, 기존 Fast Refresh warning 1건.
- Compose config: `api`, `nginx` only; data volumes `0`; external network `kosis_shadow_internal`.
- runtime legacy fallback `0`, storage write methods `0`, secret matches `0`.
- `git diff --check`: 통과.

`IMPLEMENTATION_COMMIT_SHA=RECORDED_AFTER_COMMIT`
