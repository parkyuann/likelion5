# BGE query encoder·BM25+dense hybrid retrieval v2 설계 — 20260827

## 1. 결정과 현재 사실

이 설계는 `BGE_QUERY_ENCODER_ENABLED=true`를 비어 있지 않은 검색의 필수 조건으로 바꾸고,
OpenSearch BM25와 `encoder -> Qdrant dense`를 같은 request에서 실행해 후보를 합친다.

현재 `develop`의 사실은 다음과 같다.

- `/api/v1/tables?q=...`는 OpenSearch BM25만 호출한다.
- `QdrantDenseAdapter.search_by_vector()`는 1,024차원 vector를 받을 수 있지만 text query와 연결되지 않았다.
- 기존 정본에는 `dragonkue/BGE-m3-ko@7074d66aa46562342193ca4feb3d89bf9dad71b4`,
  1,024차원, L2 정규화, candidate-only authority를 검증하는 encoder service v1 코드가 있다.
- handoff Compose에는 `api`, `nginx`만 있고 encoder service는 없다.

따라서 이 변경은 단순 flag 전환이 아니라 encoder service 계약, hybrid orchestration, fusion,
Compose 내부 연결을 하나의 retrieval-contract v2로 고정하는 작업이다. 품질 향상이나 live readiness는
이 설계만으로 주장하지 않는다.

## 2. 절대 경계

- PostgreSQL, OpenSearch, Qdrant, redis-session, redis-cache와 기존 EBS volume은 생성·초기화·변경하지 않는다.
- Qdrant collection write/upsert/delete/recreate, 재임베딩, OpenSearch 재색인·alias 변경을 구현하지 않는다.
- encoder output과 검색 hit는 candidate membership authority만 가진다.
- 최종 통계값, dimension binding, cell 선택, verdict authority를 갖지 않는다.
- `BGE_RERANKER_ENABLED=false`를 유지하고 reranker service·client·score는 이번 경로에 넣지 않는다.
- 실제 EC2 Compose 기동, GPU smoke/load test, model download는 별도 승인 전 수행하지 않는다.

## 3. 환경변수와 pin

```text
BGE_QUERY_ENCODER_ENABLED=true
BGE_QUERY_ENCODER_URL=http://bge-query-encoder:8101
BGE_QUERY_ENCODER_CONTRACT=bge-m3-ko-query-encoder-service-v2
BGE_QUERY_ENCODER_MODEL_ID=dragonkue/BGE-m3-ko
BGE_QUERY_ENCODER_MODEL_REVISION=7074d66aa46562342193ca4feb3d89bf9dad71b4
BGE_QUERY_ENCODER_VECTOR_SIZE=1024
BGE_QUERY_ENCODER_MODEL_RECEIPT_SHA256=<64hex>
BGE_QUERY_ENCODER_TIMEOUT_SECONDS=3
BGE_QUERY_ENCODER_TOKEN_FILE=/run/secrets/bge_query_encoder_token
BGE_QUERY_ENCODER_IMAGE=<registry/image>@sha256:<digest>
BGE_QUERY_ENCODER_MODEL_PATH=<content-addressed-EBS-path>
KOSIS_HYBRID_PATH_TOP_K=100
KOSIS_HYBRID_FUSION_TOP_K=100
KOSIS_HYBRID_RRF_K=60
BGE_RERANKER_ENABLED=false
```

필수값 누락, bool 문자열이 정확한 `true`가 아님, URL에 credential/query/fragment가 포함됨,
URL host가 `bge-query-encoder`가 아님, image가 digest pin이 아님, receipt가 64 hex가 아니면
nonempty search는 503으로 닫는다. URL은 Docker 내부 HTTP만 허용한다.

## 4. Encoder service v2 계약

기존 v1의 model/revision/vector 검증을 유지하고 snapshot receipt, request pin, 무절단 검증을 추가한다.

### 4.1 readiness

`GET /health`는 다음 필드를 모두 반환해야 한다.

```json
{
  "status": "READY",
  "contract": "bge-m3-ko-query-encoder-service-v2",
  "model_id": "dragonkue/BGE-m3-ko",
  "model_revision": "7074d66aa46562342193ca4feb3d89bf9dad71b4",
  "model_receipt_sha256": "<64hex>",
  "vector_dimension": 1024,
  "dtype": "float32",
  "normalize_embeddings": true,
  "authority": "CANDIDATE_GENERATION_ONLY",
  "device": "cuda",
  "cuda": "<non-empty>",
  "max_length": 1024
}
```

서비스는 시작 시 read-only model receipt의 SHA와 manifest 내부 model ID/revision/file path·size·SHA를
검증하고, 고정 probe를 encode해 finite 1024와 `0.999 <= L2 <= 1.001`을 확인한 뒤에만 READY가 된다.
`LOCAL_ENCODER_SNAPSHOT`을 사용하면서 receipt를 검증하지 않는 경로는 금지한다.

### 4.2 embedding

```json
POST /v2/query-embeddings
X-Internal-Service-Token: <Docker secret value>
{
  "contract": "bge-m3-ko-query-encoder-request-v2",
  "model_id": "dragonkue/BGE-m3-ko",
  "model_revision": "7074d66aa46562342193ca4feb3d89bf9dad71b4",
  "normalize_embeddings": true,
  "texts": ["정규화된 query"]
}
```

backend는 batch 1만 전송한다. 정규화 query는 1..200 Unicode code point, UTF-8 800 bytes 이하로
제한한다. service 최대 batch는 16이다. tokenizer special token 포함 1,024 token 초과는 413이며
절단하지 않는다.

```json
{
  "contract": "bge-m3-ko-query-encoder-response-v2",
  "model_id": "dragonkue/BGE-m3-ko",
  "model_revision": "7074d66aa46562342193ca4feb3d89bf9dad71b4",
  "model_receipt_sha256": "<64hex>",
  "vector_dimension": 1024,
  "dtype": "float32",
  "normalized": true,
  "truncated": false,
  "items": [{
    "index": 0,
    "input_sha256": "<sha256 of normalized UTF-8 query>",
    "token_count": 1,
    "l2_norm": 1.0,
    "vector": [0.0]
  }]
}
```

backend는 exact contract/model/revision/receipt/dimension/dtype/normalized/truncated, item count/index,
input SHA, token count, vector length·finite·L2를 모두 검증한다. 응답의 `vector` 예시는 형식 축약이며
실제 배열은 정확히 1,024개다. mismatch는 `QUERY_ENCODER_CONTRACT_MISMATCH` 503이다.

connect/read timeout은 0.5초/2.5초, 전체 encoder timeout은 3초다. 자동 retry는 0회다. 중복 GPU
작업과 tail latency를 피하기 위해 request path에서 retry하지 않는다.

## 5. 검색 실행 의미

### 5.1 empty query

NFKC·공백 정규화 후 빈 query는 기존처럼 PostgreSQL metadata browse만 실행한다. encoder,
OpenSearch, Qdrant, enable flag를 확인하거나 호출하지 않는다. metadata offset에는 hybrid Top-100
제한을 적용하지 않는다.

### 5.2 nonempty query

1. encoder config/readiness를 fail-closed 확인한다.
2. OpenSearch BM25 호출과 encoder 호출을 동시에 시작한다.
3. encoder 성공 후 반환 vector로 기존 Qdrant collection을 read-only 검색한다.
4. BM25와 dense 둘 다 성공해야 fusion한다.
5. disabled, timeout, HTTP 오류, contract/release/receipt mismatch 중 하나라도 있으면 partial 결과를
   반환하지 않고 503으로 닫는다. BM25-only, dense-only, stale cache fallback은 없다.
6. 한 채널의 정상 zero-hit는 실패가 아니다. 둘 다 정상 zero-hit이면 200 empty fused window다.

API 전체 hybrid deadline 기본값은 8초다. timeout 값은 초기 운영 기본값이며 EC2 load test 전에는
SLO로 주장하지 않는다.

## 6. 채널 fold와 deterministic fusion

- BM25: 기존 concrete `standard-v1` index와 release filter를 유지하고 고유 table Top-100을 사용한다.
- Dense는 ungrouped document Top-K 후 dedup하지 않는다. Qdrant
  `query_points_groups(group_by="table_key", group_size=1, limit=101)` read API를 사용한다.
  `table_key`는 단일 non-empty string payload여야 하고 group key와 representative hit payload가
  정확히 같아야 한다. grouped query를 지원하지 않는 client/server에는 ungrouped fallback하지 않고
  `QDRANT_GROUP_QUERY_UNAVAILABLE` 503으로 닫는다.
- Dense grouped query는 `using="dense"`, 기존 release/field filter, `with_payload=true`,
  `with_vectors=false`, `SearchParams(exact=true)`를 사용한다. exact search의 EC2 latency는 아직
  확인하지 않았으므로 load test 전 SLO를 주장하지 않는다.
- Dense group을 `score DESC, table_key ASC, point_id ASC`로 다시 정렬한다. score는 finite여야 한다.
  101개 group이 반환되고 정렬 후 100번째와 101번째 score가 수치상 정확히 같으면 cutoff set을
  임의 선택하지 않고 `DENSE_BOUNDARY_TIE_UNRESOLVED` 503으로 닫는다. 다르면 101번째를 버리고
  고유 table Top-100을 확정한다. 100개 이하면 전부 유지한다.
- BM25는 OpenSearch `collapse=table_key`로 고유 table을 만든 뒤 기존 결정론 정렬을 유지한다.
- 두 채널은 교집합이 아니라 union이다. 한 채널에만 나타난 candidate도 유지한다.
- raw BM25 score와 cosine score는 직접 합하지 않는다.
- equal-weight RRF를 사용한다.

```text
fusion_contract = hybrid-bm25-dense-rrf-v1
rrf_k = 60
fusion_score(table) = sum(1 / (60 + channel_rank))
sort = fusion_score DESC, best_channel_rank ASC, table_key ASC
```

`channel_rank`는 위 채널별 결정론 정렬 결과의 1-based 순위다. 동일 table은 채널당 최대 한 rank만
기여한다. BM25 또는 dense가 100개 cap에 도달했으면 해당 channel relation은 `gte`다. dense가
101개를 반환했다면 boundary tie가 없는 경우에도 corpus 뒤가 더 있을 수 있으므로 `gte`다.

최종 fused Top-100만 공개한다. nonempty query는 `offset + limit <= 100`; 초과는
`SEARCH_WINDOW_EXCEEDED` 422다. `total`은 corpus 전체가 아니라 fused window 크기다. 어느 채널이
Top-100 cap에 닿으면 `total_relation=gte`, 아니면 `eq`다.

candidate envelope:

```text
source=hybrid_rrf
score=<RRF score>
evidence.fusion.contract/rank/rrf_k/best_channel_rank
evidence.channels[].source/rank/raw_score/record_id/field/source_id/text_sha256
evidence.channels[].index_or_collection/release_id
evidence.encoder.model_id/model_revision/model_receipt_sha256/vector_dimension/normalized
```

query 원문, query vector, internal token은 응답과 일반 로그에 넣지 않는다. OpenSearch의 indexed text도
hybrid public envelope에서는 제외하고 hash와 record provenance만 보존한다.

## 7. metadata hydration과 authority

fused order의 table key 전체를 같은 `KOSIS_RELEASE_ID` PostgreSQL metadata로 hydrate한다. 누락,
중복, cross-store release mismatch는 503이다. organization filter/facet은 기존 bounded fused window
후처리이며 exhaustive corpus filter라고 주장하지 않는다.

ITEM/DIMENSION_AXIS hit와 encoder output은 table candidate 신호일 뿐 dimension value evidence,
binding assignment, completeness 또는 final value 근거가 아니다.

## 8. Compose와 보안

application overlay에 `bge-query-encoder` service를 추가한다. PostgreSQL/OpenSearch/Qdrant/Redis
service는 여전히 추가하지 않는다.

- encoder는 `image: ${BGE_QUERY_ENCODER_IMAGE}`만 사용하며 digest가 필수다.
- `ports`는 선언하지 않고 API와 encoder만 붙는 `encoder_internal` network를 `internal: true`로 둔다.
- API는 data tier용 `kosis_shadow_internal`과 `encoder_internal`에 연결한다.
- Nginx는 `encoder_internal`에 연결하지 않으며 encoder route를 proxy하지 않는다.
- model snapshot과 receipt는 content-addressed EBS path에서 read-only mount한다.
- `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `local_files_only=true`,
  `trust_remote_code=false`를 강제한다.
- non-root, `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, bounded `/tmp` tmpfs,
  Docker socket/host PID/privileged/write volume 금지.
- GPU reservation은 NVIDIA 1개, worker 1, inference concurrency 1, bounded queue 8이다.
- API service는 encoder `service_healthy` 후 시작한다.
- shared token은 Docker secret file로만 전달하고 env·Git·로그에 넣지 않는다.

`BGE_QUERY_ENCODER_IMAGE`, model path/receipt 및 secret source의 실제 EC2 값은 Git에 넣지 않는다.

## 9. reranker 경계

`BGE_RERANKER_ENABLED=false`를 유지한다. Compose에 reranker service를 추가하지 않고 API는
`BGE_RERANKER_URL`을 읽거나 호출하지 않는다. 향후 활성화는 same-candidate 평가, latency/VRAM 시험,
별도 설계·승인 후 진행한다. reranker는 누락된 candidate를 복구할 수 없으므로 encoder/hybrid
candidate adapter 안정화보다 먼저 켜지 않는다.

## 10. 구현 파일 경계

- `backend/query_encoder.py`: strict internal v2 client와 vector attestation
- `backend/search_adapter.py`: dense Top-100 fold에 필요한 deterministic ordering; write API 금지 유지
- `backend/table_catalog_service.py`: BM25+encoder/dense orchestration, RRF, hybrid pagination
- `backend/app.py`: hybrid candidate evidence response 계약
- `deploy/compose.yaml`: internal encoder service/network/secret/GPU security
- `.env.example`, `deploy/runtime.env.example`: required encoder/fusion pin과 reranker false
- `DEPLOYMENT_INVENTORY.md`, `backend/README.md`: 구현 범위와 PENDING 갱신
- tests: encoder contract, channel failure, RRF/tie/page, Compose no-port/no-write 정적 계약

encoder server image/source는 별도 공급 artifact다. 이 repository는 임의 model download나 mutable
GPU dependency image를 만들지 않고 digest-pinned image contract만 참조한다.

## 11. 검증 계약과 PENDING

구현 후 필요한 검증은 다음과 같지만, 사용자 지시에 따라 이번 구현 턴에서는 실행하지 않는다.

- encoder request/response·receipt·dimension·finite·L2·no-truncation 음성 테스트
- empty browse 3개 외부 호출 0, nonempty partial failure 0-result/503
- dense fold, RRF, 모든 tie, pagination 반복 안정성
- response/log에서 query/vector/token/원본 예외 부재
- Compose encoder public port 0, internal network, GPU 1, read-only model, digest image
- Qdrant/OpenSearch/metadata write method 0
- 실제 EC2 GPU cold/warm smoke, concurrency/OOM/latency
- 고정 retrieval set의 BM25-only 대비 hybrid candidate Recall/MRR·rescue/loss 평가

PENDING:

- `PENDING_BGE_QUERY_ENCODER_IMAGE_DIGEST`
- `PENDING_BGE_QUERY_ENCODER_MODEL_RECEIPT`
- `PENDING_BGE_QUERY_ENCODER_SECRET_SOURCE`
- `PENDING_EC2_ENCODER_LIVE_SMOKE_AND_LOAD`
- `PENDING_HYBRID_RETRIEVAL_QUALITY_EVALUATION`
- `PENDING_BGE_RERANKER_ENABLEMENT`

이 PENDING은 코드 구현을 막지 않지만 실제 Compose 기동과 live readiness 승격을 막는다.
