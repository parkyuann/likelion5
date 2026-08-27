# EC2 read-only adapter·auth 001 정합화 설계 — 20260827

## 1. 목적과 기준선

최신 `origin/develop` `066d10f941c178cb015c19fd73d52ffe5ca7f11c`에서 작업 브랜치를
만들고, 그 직계 후손이자 기존 독립 승인 완료된 application overlay 커밋
`abb9c5b7732bf5181d7e987c6ebbedb9eea9f03e`와 mock/legacy ERD guard 커밋
`b139149e93c883aee0a24d1870a660a05e4c9211`을 fast-forward로 복원했다.

이번 신규 변경은 다음만 수행한다.

1. `001_application_auth`와 local 계정 SQL을 정합화한다.
2. 기존 EC2 PostgreSQL metadata, OpenSearch, Qdrant를 읽기만 하는 adapter를 구현한다.
3. `/api/v1/tables`만 metadata/BM25 candidate search에 연결한다.
4. product-state 002와 전체 분석 pipeline은 별도 fail-closed gate로 유지한다.

EC2 접속, migration 실행, Compose 기동, metadata/index/collection/alias/pointer write는 하지 않는다.

## 2. 입력 증거와 정본 경계

- application migration: `001_application_auth`
- migration SHA-256:
  `99ACC4235A6E6E21B0802E6163EAA09B01C0BFE3534907D1AAD395404B523387`
- 첨부 upstream OpenAPI SHA-256:
  `85DFD7B51AABA1166AC48417C86F341BD32694D2DAF3854A5B8ADCD37528CF28`
- 저장소 runtime OpenAPI SHA-256:
  `A8605566EBFA77BBB87BA6F045AEDE701CB8B678A99CDBCB5A151288C0B141F8`
- 두 OpenAPI 차이는 signup의 `403 -> Forbidden` 한 항목뿐이다. signup도 DB mutation이므로
  CSRF 403을 유지하고, runtime 사본을 실행 계약으로 사용하며 upstream SHA와 exact delta를
  문서·테스트에 기록한다. upstream bytes를 재작성하거나 정본이라고 바꾸지 않는다.
- release: `kosis_canonical_20260821_full_r3_13ko_views`
- PostgreSQL `statistics_table` identity: `(snapshot_id, table_key)`
- OpenSearch retrieval record 필수 필드:
  `record_id,snapshot_id,table_key,field,text,text_sha256,source_id`
- Qdrant payload 필수 필드:
  `record_id,snapshot_id,table_key,field,source_id,text_sha256,authority`
- Qdrant vector name/size/distance: `dense`, `1024`, `Cosine`

첨부 인계서는 모든 store가 하나의 release로 적재됐다고 기록하고 사용자는 공통 식별자를
`release_id`로 확정했다. runtime에서는 `KOSIS_RELEASE_ID`를 store의 `snapshot_id` 필터값으로
사용한다. 실제 store가 이 계약과 다르면 조용히 변환하지 않고 `KOSIS_RELEASE_MISMATCH` 또는
`CROSS_STORE_RELEASE_MISMATCH`로 닫는다.

## 3. 인증 구현

### 3.1 local 계정

한 PostgreSQL transaction에서 다음을 저장한다.

```text
users.id                         = 새 UUID 문자열
users.primary_email              = 정규화 이메일
auth_accounts.user_id            = users.id
auth_accounts.provider           = "local"
auth_accounts.provider_user_id   = users.id
auth_accounts.provider_email     = 정규화 이메일
auth_accounts.password_hash      = Argon2id hash
```

로그인은 `provider='local' AND lower(provider_email)=lower(normalized_email)`로 계정을 찾고
`users.id=auth_accounts.user_id`를 결합한다. 자동 계정 병합과 OAuth route는 추가하지 않는다.

`database.schema_ready()`는 `APPLICATION_SCHEMA_STATUS=VERIFIED`와
`APPLICATION_SCHEMA_REVISION=001_application_auth`가 정확히 일치할 때만 auth repository를 연다.
migration ledger의 미공유 컬럼이나 SHA 저장 방식을 추정해 SQL로 조회하지 않는다. 전달받은 SHA는
배포 attestation 문서에 보존한다. `001` 파일·DDL은 작성하거나 실행하지 않는다.

SQLSTATE `23505`는 가입 conflict 409로 처리하되 constraint 세부 이름을 사용자에게 노출하지 않는다.
그 밖의 SQLSTATE는 503이다.

### 3.2 세션

기존 승인된 Redis opaque session 구현을 유지한다.

- `redis-session`만 사용
- fixed TTL 604800, sliding renewal 없음
- 사용자당 newest 5, 여섯 번째 로그인에서 oldest 1개 원자 삭제
- current logout, logout-all
- raw session ID는 cookie 이외 JSON 응답에 포함하지 않음
- HttpOnly, Secure, SameSite=Lax, Path=/, no Domain

## 4. capability gate 분리

| capability | route | 상태 |
|---|---|---|
| auth 001 | `/api/auth/*` | exact schema attestation 후 활성 |
| metadata browse/BM25 | `GET /api/v1/tables` | adapter config와 store contract 검증 후 활성 |
| product state 002 | conversations/favorites | `APPLICATION_PRODUCT_STATE_PENDING` 503 |
| full pipeline | analyze/verify/image | `PIPELINE_RUNTIME_PENDING` 503 |
| dense search | Python 내부 vector API | Qdrant config/shape 검증 후 사용 가능, public text route 없음 |

unsafe route는 CSRF 검사를 capability gate보다 먼저 수행한다.

## 5. PostgreSQL metadata repository

새 `backend/metadata_repository.py`는 `KOSIS_METADATA_DATABASE_URL`만 사용하고 application DSN을
읽지 않는다. connection에는 `default_transaction_read_only=on`을 적용한다. DDL/DML/bootstrap은 없다.

제공 기능:

- `browse_tables(release_id, limit, offset, organization)`
- `hydrate_tables(release_id, ordered_table_keys)`
- exact count와 organization facet

조회 컬럼은 전달된 `statistics_table` 계약으로 제한한다. OpenSearch key가 같은 release의
PostgreSQL에 없거나 중복되면 결과를 버리지 않고 `CROSS_STORE_RELEASE_MISMATCH` 503으로 닫는다.
각 browse/hydrate/get 경로는 먼저 같은 read-only session에서
`SELECT 1 FROM statistics_table WHERE snapshot_id = %s LIMIT 1`로 configured release가 실제로
존재하는지 확인한다. 0건이면 정상 빈 결과로 해석하지 않고 `KOSIS_RELEASE_MISMATCH` 503으로 닫는다.
별도 release-manifest 테이블은 전달 계약에 없으므로 추정하지 않는다.

## 6. OpenSearch BM25 adapter

필수 환경변수:

```text
KOSIS_METADATA_DATABASE_URL
OPENSEARCH_URL
KOSIS_RELEASE_ID
KOSIS_BM25_ANALYZER=standard-v1
KOSIS_BM25_INDEX
```

`KOSIS_OPENSEARCH_INDEX` legacy 이름 fallback은 제거한다. analyzer는 이번 v1에서
`standard-v1`만 허용하고 whitespace 결과와 합치지 않는다.

preflight는 configured concrete index를 `GET /{index}`로 읽어 응답 key가 exact index인지,
`_source`가 disabled가 아닌지, 필수 properties가 존재하는지, ID/hash 필드는 keyword이고 `text`는
text인지 확인한다. 이어 같은 concrete index에 `snapshot_id=KOSIS_RELEASE_ID` term filter를 둔
read-only `_count`를 실행하고 count가 1 이상인지 확인한다. 0건이면 검색어별 빈 결과를 반환하기 전에
`KOSIS_RELEASE_MISMATCH` 503으로 닫는다. alias/current pointer를 변경하지 않는다. 현재 전달물에는 mapping/settings
receipt SHA가 없으므로 live readiness attestation을 주장하지 않는다.

검색은 `text` match 한 channel만 사용하고 `snapshot_id=KOSIS_RELEASE_ID`, 허용 field
`TITLE,CATEGORY,ITEM,DIMENSION_AXIS`를 filter한다. `table_key` collapse 후 `_score DESC,
table_key ASC`로 정렬한다. 각 hit는 필수 필드, release, field, `text_sha256`을 검증한다.

빈/NFKC·공백 정규화 후 빈 query는 OpenSearch를 호출하지 않고 PostgreSQL browse로 처리한다.
비어 있지 않은 query는 최대 1000개의 deduplicated candidate window만 읽는다. `offset+limit>1000`은
`SEARCH_WINDOW_EXCEEDED` 422다. `total_relation`은 window가 소진되면 `eq`, 잘리면 `gte`다.
organization filter와 facet은 이 candidate window를 metadata로 hydrate한 뒤 적용하며 relation을 함께
반환한다. index에 org 필드가 없어 완전한 server-side org filtering이 필요한 경우
retrieval-contract v2에서 새 shadow index 변경안으로 다룬다.

후보 item 최소 필드:

```text
table_key, release_id, source, score,
org_id, tbl_id, org_name, tbl_name, status, send_de, kosis_url,
evidence.channel, evidence.analyzer, evidence.index,
evidence.document_id, evidence.record_id, evidence.field,
evidence.source_id, evidence.text, evidence.text_sha256
```

후보는 candidate generation authority만 가지며 최종 통계값·진위·binding을 반환하지 않는다.

## 7. Qdrant dense read adapter

필수 환경변수:

```text
QDRANT_URL
KOSIS_RELEASE_ID
QDRANT_COLLECTION
QDRANT_VECTOR_SIZE=1024
QDRANT_RECEIPT_SHA256
```

공개 HTTP text route와 encoder 호출은 만들지 않는다. 내부
`search_by_vector(vector, fields, limit)`만 제공한다.

- input은 finite float 1024개
- read-only collection inventory에서 configured 이름이 실제 collection으로 존재하는지 확인해 alias를 거부
- exact collection의 green status, named vector `dense`, size 1024, Cosine 확인
- exact collection에 `snapshot_id=KOSIS_RELEASE_ID` filter를 둔 read-only count가 1 이상인지 확인;
  0건이면 `KOSIS_RELEASE_MISMATCH` 503
- query filter는 `snapshot_id=KOSIS_RELEASE_ID`와 허용 field
- payload의 필수 필드/release/authority를 검증
- write/upsert/delete/recreate API 없음

`QDRANT_RECEIPT_SHA256`은 64자리 config pin으로 검증하고 모든 candidate evidence에 보존한다.
Qdrant 서버가 receipt SHA를 자체 반환하지 않으므로 이것만으로 live collection bytes가 증명된다고
주장하지 않는다. receipt 원문·sample attestation은 retrieval-contract v2 PENDING이다.

## 8. 배포 구조

Compose service는 `api`, `nginx`만 둔다. frontend는 `deploy/nginx.Dockerfile` multi-stage build로
Nginx image에 포함되므로 별도 data/service dependency가 아니다. PostgreSQL, OpenSearch, Qdrant,
redis-session, redis-cache service/volume은 선언하지 않는다. 외부 network
`kosis_shadow_internal`만 사용하고 Nginx는 `/api` prefix를 보존한다.

BGE encoder/reranker와 redis-cache는 false/disabled로 유지한다.

## 9. 검증

- auth SQL arguments와 local provider contract
- Argon2id, signup/login/me/logout/logout-all, fixed newest-five session
- runtime OpenAPI hash와 upstream SHA/delta 기록
- product-state/pipeline gate 분리와 CSRF 우선순위
- metadata read-only DSN 분리, release-existence attestation, browse/hydrate ordering, cross-store mismatch
- OpenSearch mapping/index release count/hit release/hash 검증, empty query no-call, collapse/rank/window
- Qdrant exact collection inventory/alias rejection/release count/vector/config/payload validation과 write method 부재
- production SQLite/Bearer/localStorage/query access-token/legacy catalog fallback scan
- backend import/pytest, frontend production build
- Compose config/service/network/volume 확인, Nginx prefix 확인
- secret scan, `git diff --check`
- full repository differential gate: 신규 실패 0, 기존 통과 감소 0

실제 EC2 endpoint, migration, data, index, collection은 호출·변경하지 않는다.

## 10. 문서와 PENDING

`DEPLOYMENT_INVENTORY.md`에 포함 파일, 환경변수, 구현 범위, 검증 결과, 최종 commit SHA를 기록한다.
commit SHA는 commit 생성 뒤 후속 문서 commit을 만들지 않도록 `git rev-parse` 결과와 inventory의
`COMMIT_SHA_AT_DELIVERY` 항목을 배포 보고에서 함께 제시하며, 문서 내부에는 `RECORDED_AFTER_COMMIT`
표시를 허용한다.

계속 PENDING:

- `002_application_product_state`
- HTTPS/domain/certificate
- BGE encoder/reranker runtime enablement
- redis-cache 비교실험 및 활성화
- retrieval-contract v2: OpenSearch mapping/settings receipt, exact org field/facet,
  Qdrant receipt 원문·sample attestation, dense text-query encoder 연결
