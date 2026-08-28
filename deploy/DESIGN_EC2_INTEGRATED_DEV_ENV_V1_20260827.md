# EC2 통합 개발 환경 v1 설계 — 20260827

## 1. 목표와 범위

완전한 검색 품질이나 모든 product feature가 아니라, 기존 EC2 데이터 계층을 변경하지 않고
frontend·nginx·backend API·GPU query encoder·현재 pipeline을 함께 실행할 수 있는 개발 환경을 만든다.

- 외부 read/reference: PostgreSQL `kosis_metadata`/`application`, OpenSearch, Qdrant, redis-session
- application overlay: frontend static serving, nginx, API, `bge-query-encoder`
- candidate search: `standard-v1` BM25 + pinned BGE query vector + existing Qdrant dense + RRF
- article pipeline: backend가 `run_article_body_pipeline_trace_v1.run_trace()`를 요청별 임시 디렉터리에서 호출
- 미완성 기능은 fake/local fallback 없이 503 PENDING 또는 명시적 feature gate

기존 KOSIS datastore container·volume·EBS·index·collection은 생성·초기화·수정·삭제하지 않는다.
재색인·재임베딩·alias/current pointer 변경도 하지 않는다. 단, 로그인 검증에는 정본 001 범위의
users/auth_accounts DML과 Redis auth:* session key write가 필요하므로 이 두 write만 허용한다.
application_schema_migrations와 다른 DB/schema/table, OpenSearch, Qdrant에는 write하지 않는다.

## 2. EC2에서 확인된 고정 증거

- external network: `kosis_shadow_internal`, 기존 4개 datastore container 연결
- GPU: NVIDIA L4-12Q, 12,288 MiB
- base image: `news-verification-bge-preflight@sha256:b98d5ae3c07824c21e2f0242e9cf488c47563a6c0320e9876a4af245f7538adb`
- model repository: `/srv/news_verification/gpu_model_cache/models--dragonkue--BGE-m3-ko`
- snapshot revision: `7074d66aa46562342193ca4feb3d89bf9dad71b4`
- model file SHA-256: `da164fa90633e730db4ee91a79ebf99a0826fb31d6f002c4a8ec7952a286c4f4`
- receipt source: `/srv/news_verification/gpu_preflight/receipts/query_encoder_preflight_20260827.json`
- receipt SHA-256: `e092f65d5520f374e30c647f6f02d8203b4ddc6ddfd5d064acfd87f6bb28dff7`
- receipt result: model/revision, 1024 dimension, finite, normalized, repeat-exact, candidate-only authority

최종 encoder service image는 위 base image와 Git의 backend/query_encoder_service.py,
deploy/encoder.Dockerfile, deploy/encoder-runtime.lock.json으로 EC2에서 새로 build한다.
service는 Python stdlib HTTP server를 사용하므로 base image에 package를 추가 설치하지 않는다.
encoder-runtime.lock.json은 base digest와 Python/torch/transformers/sentence-transformers/
huggingface-hub/numpy exact version을 startup에서 검증한다. source·Dockerfile·lock SHA와 image ID,
probe 결과를 deploy/encoder-image-receipt.example.json 계약으로 발행한다.
integration-dev에서는 build 결과의 content-addressed image ID를 server-only
`BGE_QUERY_ENCODER_IMAGE=sha256:<64hex>`로 사용한다. registry 배포는 PENDING이다.

## 3. Encoder v2

`backend/query_encoder_service.py`는 시작 시 다음을 모두 검증한 뒤에만 READY가 된다.

1. receipt 파일 byte SHA가 `BGE_QUERY_ENCODER_MODEL_RECEIPT_SHA256`과 일치
2. receipt status/model/revision/dimension/finite/normalized/repeat_exact/authority 일치
3. Git의 deploy/bge-model-closure-7074d66a.json
   (SHA-256 fd2d4a2ecb5443f856b6bb991d7d6d4dda2b03dbe0fa428f6b9a6da51ed3312f)에 기록된
   11개 파일 path/size/SHA 전체 일치
4. CUDA 사용 가능, pinned local snapshot만 로드, download/alternate/CPU fallback 없음
5. 고정 probe가 정확히 1024 finite, `0.999 <= L2 <= 1.001`

`POST /v2/query-embeddings`는 Docker secret token, batch 1, request/model pin, no truncation,
input SHA/token count/vector/L2 attestation 계약을 사용한다. 외부 port는 없고 API와 encoder만
`encoder_internal` network에 붙는다. symlink closure가 보존되도록 snapshot 단독이 아니라 전체
models--dragonkue--BGE-m3-ko repository를 /models/repository:ro에 mount하고 고정 revision 하위만
로드한다. preflight receipt와 Git model closure manifest도 별도 read-only mount한다.

server-only secret source는
`/srv/news_verification/application-overlay/secrets/bge_query_encoder_token`으로 고정한다.
배포 준비 단계에서 mode 0600, non-empty random value로 생성하며 내용·해시는 Git/로그/응답에 남기지 않는다.

## 4. Search와 pipeline의 분리

### 4.1 Candidate search

`GET /api/v1/tables?q=...`는 승인된 hybrid contract를 사용한다.

- BM25와 encoder를 병렬 시작하고 encoder 성공 후 Qdrant grouped dense read
- 양 채널 성공 필수, partial/local/legacy fallback 없음
- same release, 1024 normalized vector, grouped unique Top-100, equal-weight RRF k=60
- 후보 생성만 수행하며 cell/value/verdict authority 없음

### 4.2 Article pipeline

`PIPELINE_RUNTIME_ENABLED=true`일 때 `/api/v1/verify/develop`가
`backend.develop_verify_service.verify_article_develop()`을 호출한다. API image는 `src/`와 `configs/`를
포함해야 한다. 요청별 `/tmp` 디렉터리를 사용하고 완료 후 삭제한다.

pipeline source는 현재 정본에서 scripts/materialize_deployment_bundle.py로 다시 생성한 closure만
사용한다. 20260827 재생성 증거는 runtime module 72개, bundle manifest SHA-256
63251f70d95933492e23c4b978ecb1fa8f283329bd946c8f2e0630409891a51c, trace SHA-256
449f52511210f00bab7d870332fe93b21eff4f5d0f37f29897870e58cddc8cbb, operational runtime SHA-256
e00362c8595456d7a7a314cc1a89f900d4063934f95111bf17288a6702db7f91이다. 이 closure에는
FILTERED_NON_NUMERIC 보완이 포함된다. materialized manifest와 복사 후 file SHA를 Git에 기록한다.

현재 operational live 후단은 local catalog/profile assets와 reranker를 요구하므로
`PIPELINE_LIVE_STAGE_ENABLED=false`를 유지한다. 이 상태에서 L1, L2, deterministic layers까지 실행하고
응답은 `structured_only`로 명시한다. BM25+dense candidate API가 정상이어도 이를 기존 live pipeline에
임의 주입하지 않는다. 두 계약의 결합은 retrieval-contract v2 후속이다.

`/api/v1/analyze`는 이번 v1에서 input_type=article text만 위 article verifier에 넘긴다. natural KOSIS query가 legacy
`src.run_pipeline` local 검색으로 내려가지 않도록 `PIPELINE_NATURAL_QUERY_ENABLED=false`이면
`PIPELINE_NATURAL_QUERY_PENDING` 503으로 호출 전에 닫는다. legacy src/run_pipeline.py는 API image
closure에서 제외한다. URL extractor는 archive 의존이므로 PIPELINE_URL_PENDING 503, image route도
PIPELINE_IMAGE_PENDING 503이며 두 경로 모두 extractor/model call 0을 검증한다.

실제 EC2 Qdrant collection은 named vector가 아니라 unnamed 1024 cosine vector다. adapter는
QDRANT_VECTOR_NAME=(empty)을 승인값으로 받고 using을 전송하지 않으며, preflight도 unnamed vector
schema를 검증한다. named dense fallback은 금지한다.

## 5. Compose 경계

- 포함: api, nginx(frontend static 포함), bge-query-encoder
- 금지: postgres, opensearch, qdrant, redis-session, redis-cache, reranker, DiffuRank
- API: `kosis_shadow_internal` + `encoder_internal`, encoder secret mount
- encoder: `encoder_internal` only, no `ports`, GPU 1, read-only model repository/receipt, secret mount
- nginx: `kosis_shadow_internal` only, `/api` prefix 보존

auth browser 통합을 위해 Nginx는 server-only self-signed development certificate를 read-only mount해
8443 TLS endpoint를 제공한다. 이는 integration smoke 전용이며 production domain/certificate 승인이
아니다. __Host-kosis_session Secure cookie는 이 TLS endpoint에서만 E2E 검증한다.

## 6. 검증과 승인

1. backend import/full tests, auth session lifecycle, pipeline route/feature-gate tests
2. encoder service contract unit tests and EC2 image build, source/lock/image receipt
3. EC2 encoder health/embed: exact revision/receipt/1024/finite/L2/input hash
4. Compose config: only three application services, encoder public port 0, data volume declaration 0
5. read-only/static scan: datastore write/reindex/reembed/legacy fallback 0
6. frontend production build, secret scan, `git diff --check`
7. 통제된 EC2 Compose up으로 frontend/API/encoder를 기존 external network에 연결
8. datastore 전후 schema/index/collection/count 불변, auth test user/session 생성·정확한 cleanup receipt
9. TLS frontend-auth, hybrid search, structured-only article, 각 PENDING route 503·zero-call E2E
10. independent Sol final review after all implementation and evidence

검증 실패 또는 실제 server-only pin 불일치는 commit/push를 막는다. application overlay의 통제된
docker compose up과 위 auth 전용 write만 허용한다. migration, KOSIS datastore write, 데이터 적재,
release activation은 금지한다.

20260827 read-only latency probe에서 동일 EC2의 model load 2.404초, warm encode 0.290초,
Qdrant unnamed exact grouped Top-101 1.058초(HTTP 200, 101 groups, rank100/101 비동률),
OpenSearch Top-100 0.282/0.058/0.026초였다. 따라서 8초는 현재 integration 기본 deadline으로 유지하되
3회 hybrid E2E 중 하나라도 초과하면 측정 p99 기반으로 보수적으로 상향하고 새 설계 SHA 승인을 받는다.

## 7. PENDING

- `PIPELINE_LIVE_STAGE_ENABLED`: hybrid adapter와 operational live pipeline contract 통합
- `PIPELINE_NATURAL_QUERY_ENABLED`: legacy local search 제거 후 candidate/cell pipeline 연결
- `PIPELINE_IMAGE_ENABLED`
- BGE reranker
- redis-cache
- `002_application_product_state`
- DiffuRank
- HTTPS/domain/certificate/registry
