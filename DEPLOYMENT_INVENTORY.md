# EC2 통합 개발 환경 배포 인벤토리 — 20260827

## 적용 기준

- 승인 설계: `deploy/DESIGN_EC2_INTEGRATED_DEV_ENV_V1_20260827.md`
- 승인 설계 SHA-256: `56368E2AE144B1DC2104566000D1F40D7CFFF4365AEC7B2107C92995AD9FA602`
- named-vector 초기 설계 대체 기록: `deploy/DESIGN_SUPERSESSION_BGE_QUERY_ENCODER_V2_20260827.md`
- 모델 closure manifest: `deploy/bge-model-closure-7074d66a.json`
- 모델 closure SHA-256: `FD2D4A2ECB5443F856B6BB991D7D6D4DDA2B03DBE0FA428F6B9A6DA51ED3312F`
- 모델: `dragonkue/BGE-m3-ko@7074d66aa46562342193ca4feb3d89bf9dad71b4`
- EC2 preflight receipt SHA-256: `e092f65d5520f374e30c647f6f02d8203b4ddc6ddfd5d064acfd87f6bb28dff7`
- 검증된 encoder image content ID: `sha256:666dea4633e530cda4959c2b5682920ff408e8754b58fe728d787256bae9beb3`
- image receipt: `deploy/encoder-image-receipt-ec2-20260827.json`

## 이번 변경 파일

- `deploy/compose.yaml`: `api`, `nginx`, `bge-query-encoder` 세 application service만 선언한다.
  기존 PostgreSQL, OpenSearch, Qdrant, redis-session, redis-cache, reranker, DiffuRank service와
  data volume은 선언하지 않는다.
- `deploy/nginx.conf`: server-only self-signed development certificate를 read-only mount하는
  HTTPS `8443` endpoint와 HTTP `8080` HTTPS redirect를 제공한다. `/api` prefix는 upstream에
  그대로 전달한다.
- `.env.example`, `deploy/runtime.env.example`: EC2 service pin과 server-only source 경로를 기록한다.
- `backend/query_encoder.py`, `backend/query_encoder_service.py`: 내부 token, 고정 model/receipt,
  CUDA·1024차원·정규화 계약의 query encoder client/service.
- `backend/search_adapter.py`, `backend/table_catalog_service.py`: OpenSearch BM25와 unnamed-vector
  Qdrant dense read adapter, release fail-closed, equal-weight RRF 후보 검색.
- `backend/app.py`, `backend/develop_verify_service.py`, `backend/runtime_gate.py`: packaged pipeline
  article 경로와 미완성 입력/기능의 명시적 PENDING gate.
- `deploy/pipeline_runtime/`: operational manifest가 고정한 deploy runtime closure.
- `deploy/encoder.Dockerfile`, runtime lock, model closure 및 image receipt: GPU image 재현 계약.
- `tests_backend/test_ec2_integration_*.py`, `test_query_encoder_service_v2.py`: 통합 계약 회귀 테스트.
- `backend/README.md`: 통합 개발 환경의 auth/search/pipeline 경계와 PENDING 목록을 문서화한다.

## Compose 경계

서비스는 정확히 다음 세 개다.

```text
api                 kosis_shadow_internal + encoder_internal
nginx               kosis_shadow_internal only
bge-query-encoder   encoder_internal only, internal network, host ports 없음
```

`api`는 encoder secret을 읽고 encoder health가 `READY`일 때 시작한다. encoder는 GPU 1개,
read-only 전체 Hugging Face repository, read-only preflight receipt, read-only model closure
manifest를 사용한다. 모델 repository는 symlink closure를 보존하기 위해 snapshot 하위 디렉터리가
아니라 다음 전체 경로를 mount한다.

```text
host /srv/news_verification/gpu_model_cache/models--dragonkue--BGE-m3-ko
     -> /models/repository:ro
host /srv/news_verification/gpu_preflight/receipts/query_encoder_preflight_20260827.json
     -> /etc/bge-query-encoder/receipt/query_encoder_preflight_20260827.json:ro
host /srv/news_verification/application-overlay/manifests/bge-model-closure-7074d66a.json
     -> /etc/bge-query-encoder/closure/bge-model-closure-7074d66a.json:ro
```

encoder token은 `/run/secrets/bge_query_encoder_token`으로만 전달한다. 검증된 encoder image
content ID는 비밀값이 아니므로 Compose와 image receipt에 직접 고정했다. 따라서 mutable tag나
임의 image 환경변수로 이 경로를 열 수 없다. token 내용은 Git에 넣지 않는다. TLS source도 다음
server-only 경로만 사용한다.

```text
/srv/news_verification/application-overlay/secrets/bge_query_encoder_token
/srv/news_verification/application-overlay/tls/dev.crt
/srv/news_verification/application-overlay/tls/dev.key
```

## EC2 server-only 환경변수

아래 값은 Git에 비밀값을 넣지 않고 EC2에서 주입한다. URL의 password와 token 내용은 기록하지
않는다.

```text
APPLICATION_DATABASE_URL              application DB runtime role
REDIS_SESSION_URL                     redis-session only
KOSIS_METADATA_DATABASE_URL           kosis_metadata reader role
OPENSEARCH_URL                        existing OpenSearch service
QDRANT_URL                            existing Qdrant service
KOSIS_RELEASE_ID=kosis_canonical_20260821_full_r3_13ko_views
KOSIS_BM25_ANALYZER=standard-v1
KOSIS_BM25_INDEX=kosis_bm25_kosis-canonical-20260821-full-r3-13ko-views_standard-v1_dd4030a92b73
QDRANT_COLLECTION=kosis_dense_kosis-canonical-20260821-full-r3-13ko-views_dragonkue-bge-m3-ko_kosis-dense-input-document-v1_7074d66aa465_d5bd81613d81
QDRANT_VECTOR_SIZE=1024
QDRANT_VECTOR_NAME=
QDRANT_RECEIPT_SHA256                 existing collection receipt
BGE_QUERY_ENCODER_ENABLED=true
BGE_QUERY_ENCODER_URL=http://bge-query-encoder:8101
BGE_QUERY_ENCODER_MODEL_ID=dragonkue/BGE-m3-ko
BGE_QUERY_ENCODER_MODEL_REVISION=7074d66aa46562342193ca4feb3d89bf9dad71b4
BGE_QUERY_ENCODER_VECTOR_SIZE=1024
BGE_QUERY_ENCODER_MODEL_RECEIPT_SHA256=e092f65d5520f374e30c647f6f02d8203b4ddc6ddfd5d064acfd87f6bb28dff7
BGE_QUERY_ENCODER_TIMEOUT_SECONDS=3
BGE_QUERY_ENCODER_TOKEN_SOURCE=/srv/news_verification/application-overlay/secrets/bge_query_encoder_token
BGE_QUERY_ENCODER_MODEL_SOURCE=/srv/news_verification/gpu_model_cache/models--dragonkue--BGE-m3-ko
BGE_QUERY_ENCODER_MODEL_RECEIPT_SOURCE=/srv/news_verification/gpu_preflight/receipts/query_encoder_preflight_20260827.json
BGE_QUERY_ENCODER_CLOSURE_MANIFEST_SOURCE=/srv/news_verification/application-overlay/manifests/bge-model-closure-7074d66a.json
NGINX_TLS_CERT_SOURCE=/srv/news_verification/application-overlay/tls/dev.crt
NGINX_TLS_KEY_SOURCE=/srv/news_verification/application-overlay/tls/dev.key
PIPELINE_RUNTIME_ENABLED=true
PIPELINE_LIVE_STAGE_ENABLED=false
PIPELINE_NATURAL_QUERY_ENABLED=false
PIPELINE_IMAGE_ENABLED=false
PIPELINE_URL_ENABLED=false
BGE_RERANKER_ENABLED=false
KOSIS_REDIS_CACHE_ENABLED=false
```

`api`와 `bge-query-encoder`는 공통 비특권 UID/GID `65532:65532`로 실행한다. 서버의
encoder token 파일은 이 UID/GID 소유, mode `0400`으로 두며 Compose에는 read-only
secret으로만 마운트한다.

## 허용·금지되는 외부 저장소 동작

인증 통합에 한해 application DB의 정본 `001_application_auth` 범위에서 `users`와
`auth_accounts`의 signup/login 관련 DML, 그리고 `redis-session`의 `auth:*` opaque session
key 쓰기만 허용한다. 001 migration 실행, `application_schema_migrations` 변경, product-state
테이블 생성, KOSIS metadata 쓰기, OpenSearch index/write/alias/current pointer 변경, Qdrant
upsert/delete/recreate/write, 재색인·재임베딩은 금지한다. redis-cache는 사용하지 않는다.

검색은 OpenSearch `standard-v1` BM25와 BGE query encoder의 1024차원 normalized vector를
기존 Qdrant collection에 읽기 전용으로 질의하고, 양 채널 결과를 equal-weight RRF(`k=60`)로
후보 Top-100에 결합한다. release_id와 model revision/receipt가 맞지 않으면 fail-closed 한다.
후보 API는 최종 통계값이나 진위 판정을 수행하지 않는다.

pipeline은 `PIPELINE_RUNTIME_ENABLED=true`로 application 경로를 열되, live 후단·natural query·
image·URL은 각각 false feature gate로 PENDING 503을 반환한다. fake/local/SQLite fallback은
사용하지 않는다.

## 검증 상태

EC2에서는 application overlay만 기동했다. migration, KOSIS metadata write, OpenSearch/Qdrant
write, 재색인·재임베딩, alias/current pointer 또는 release activation은 수행하지 않았다.
인증 E2E용 임시 local 계정 1개만 생성한 뒤 전체 session과 DB row를 삭제했다.

```text
Compose services: api, nginx, bge-query-encoder (forbidden datastore service 0)
Backend suite: 70 passed, 1 pre-existing Starlette/httpx deprecation warning
Frontend check: lint 0 errors / 1 Fast Refresh warning; production build passed
Local Compose config: passed; services exactly bge-query-encoder, api, nginx
Backend import + packaged pipeline module --help: passed
Packaged pipeline manifest: 72/72 file size and SHA-256 matched
Encoder image receipt source pins: 3/3 SHA-256 matched; model closure SHA matched
Staged git diff --check: passed; CRLF byte-sealed pipeline closure는 `.gitattributes`로
  text 변환과 trailing-space 진단을 끄고 index blob 72/72 manifest SHA로 별도 검증
HTTPS health: HTTP 200; HTTP 8080 -> HTTPS 8443 redirect 308
BGE standalone: READY, CUDA, 1024 dimensions, finite, L2 norm 1.00000001
Hybrid search after keep-alive fix, 5 runs: HTTP 200;
  0.838s / 0.825s / 0.823s / 0.823s / 0.826s
Encoder post-embed health: 35초 후 healthcheck 5/5 exit 0, healthy, failing streak 0
Hybrid Top-100: release match 100/100; BM25+dense evidence; source=hybrid_rrf
Auth: signup 201; login 6/6; oldest eviction; me/current logout/logout-all passed
Pipeline article after pin hardening: HTTP 200; 5.258s; structured_only; result 1
PENDING routes: natural query / URL / image / product-state all explicit HTTP 503
Datastore before/after: application 0/0/1, metadata 288393,
  OpenSearch 2121139, Qdrant 2121139/2121139 green, redis-session dbsize 0
commit SHA: RECORDED_IN_HANDOFF_RESPONSE
```

## PENDING

- production image registry push와 registry digest
- production HTTPS domain/certificate; 현재는 server-only self-signed development certificate
- pipeline live-stage 및 자연어·URL·이미지 입력 경로
- BGE reranker runtime enablement
- redis-cache 비교실험 및 활성화 여부
- `002_application_product_state` migration과 conversations/messages/favorites
- 검색 고도화에 따른 retrieval-contract v2
- DiffuRank
