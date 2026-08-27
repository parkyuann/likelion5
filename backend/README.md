# Backend — EC2 통합 개발 환경

이 overlay는 기존 EC2 data plane을 새로 만들지 않고 참조한다. Compose가 선언하는 application
service는 `api`, `nginx`, `bge-query-encoder`뿐이며 PostgreSQL, OpenSearch, Qdrant,
redis-session, redis-cache는 `kosis_shadow_internal`의 외부 서비스다.

## 인증

- application PostgreSQL의 정본 `001_application_auth`를 사용한다.
- local account는 `provider=local`, `provider_user_id=users.id` 문자열,
  정규화 email의 `provider_email`/`primary_email`, Argon2id `password_hash`를 사용한다.
- Redis는 `redis-session`만 사용하며 opaque HttpOnly/Secure/SameSite=Lax cookie, 7일 TTL,
  사용자당 최대 5개 session lifecycle을 적용한다.
- 운영 경로에 SQLite 사용자·세션, Bearer token, localStorage access token, OAuth query token,
  raw session ID 응답은 없다.
- 인증 DML은 `users`·`auth_accounts`와 Redis `auth:*` session key로 한정한다. migration,
  schema/index/collection 변경과 KOSIS 데이터 쓰기는 금지한다.

## Hybrid 후보 검색

`GET /api/v1/tables?q=...`의 비어 있지 않은 query는 다음 경계를 따른다.

1. OpenSearch의 `standard-v1` BM25 Top-100과 내부 `bge-query-encoder` 호출을 병렬 시작한다.
2. encoder가 `dragonkue/BGE-m3-ko@7074d66aa46562342193ca4feb3d89bf9dad71b4`의
   1024차원 finite normalized vector와 receipt/model attestation을 반환하면 기존 Qdrant
   unnamed collection에 grouped dense 후보를 읽는다.
3. 두 채널 모두 성공해야 equal-weight RRF(`k=60`) Top-100을 반환한다. partial, local,
   SQLite, legacy pickle, 과거 Qdrant fallback은 없다.

release_id, analyzer/index, collection/vector dimension, model revision/receipt가 정본과 다르면
503 fail-closed 한다. 응답은 `table_key`, `release_id`, `source`, `score`, evidence를 가진
후보만 반환하며 최종 통계값·cell 검증·진위 판정을 하지 않는다.

encoder는 외부 port 없이 `encoder_internal`에서 API와만 통신한다. token은 Docker secret으로만
전달하고 query/vector/token은 public response나 일반 로그에 넣지 않는다. reranker는
`BGE_RERANKER_ENABLED=false`로 유지한다.

## Pipeline feature gates

`PIPELINE_RUNTIME_ENABLED=true`일 때 구조화 기사 입력의 application 경로만 준비한다. 현재
완성되지 않은 live 후단은 `PIPELINE_LIVE_STAGE_ENABLED=false`, natural query는
`PIPELINE_NATURAL_QUERY_ENABLED=false`, image/URL은 각각 false로 둔다. 닫힌 경로는 fake
결과나 local 검색으로 내려가지 않고 명시적 PENDING 503을 반환한다.

다음 기능은 통합 환경 이후 순차 작업이다.

- live-stage와 hybrid retrieval contract v2 결합
- natural query/canonical pipeline 연결
- image/URL 정식 extractor 연결
- `002_application_product_state`
- BGE reranker, redis-cache, DiffuRank

## Migration 및 운영 금지사항

이 저장소의 application migration은 실행 절차와 정본 경계만 문서화한다. 이번 overlay 기동 시
migration을 실행하지 않는다. KOSIS PostgreSQL metadata, OpenSearch index/alias, Qdrant
collection과 EBS volume은 read-only 참조 대상이며 재적재·재색인·재임베딩·current pointer
변경을 수행하지 않는다.

실제 server-only 경로와 pin은
`DEPLOYMENT_INVENTORY.md` 및 `deploy/runtime.env.example`에 기록되어 있다. image digest,
secret file, TLS certificate/key의 실제 내용은 Git에 포함하지 않는다.
