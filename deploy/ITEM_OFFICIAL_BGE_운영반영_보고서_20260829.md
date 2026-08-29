# ITEM 공식검색·BGE reranker 운영 Retriever 반영 보고서 (20260829)

## 결론

후보 생성 단계에서 ITEM 공식검색을 추가하면 동결 57 target의 후보 Recall이 A0보다
일관되게 증가했고 `NO_CANDIDATES`도 감소했다. 사용자가 허용한 범위에 따라
`ITEM 공식검색 + bounded suffix`와 attested `BGE-reranker v2`를 운영 Retriever에
반영했다. 운영 API의 실제 기사 E2E가 HTTP 200/`completed`로 끝났고, 새 reranker의
`POST /rerank` 200 access log도 확인했다.

판정은 **GO_CANDIDATE_GENERATION / 운영 반영 완료**다. 다만 이 문서는
후보 생성 효과를 승인하는 결과이며, 동결 57건 전체에 대한 rerank 전·후 Recall
재채점은 사용자 지시에 따라 별도 후속 과제로 남긴다.

독립 Sol task는 생성하지 않았다. 사용자가 단일 세션 자체 재검토를 명시적으로
허용했으므로 승인 주체는 `USER_AUTHORIZED_SINGLE_SESSION_SELF_REVIEW`이며,
독립 승인으로 주장하지 않는다.

## 범위와 변경

- 운영 checkout·`develop`·기존 dirty handoff checkout은 수정하지 않았다.
- clean worktree에서 구현하고 commit `049c2c84a6be0fa421812fdb8cd9daa273da8211`을
  push한 뒤, 같은 SHA의 immutable release worktree를 EC2에 배치했다.
- 추가 기능은 `item_official` 후보 채널, ITEM 정규화/bounded suffix, provenance receipt,
  내부 전용 BGE reranker v2 연결, reranker 입력용 PostgreSQL read-only profile projection이다.
- ITEM 검색 결과는 `table_key` 후보 유입 근거만 제공한다. 셀·축·값 확정 권한,
  Late Binding/Strict Validator 권한, 공개 table catalog의 기존 hybrid RRF 경로는
  바꾸지 않았다.

## 동결 shadow 결과

실행 receipt: `data/develop/ec2_item_official_recall_shadow_20260829/runs/run_20260829T122345Z_item_official_v1/`

분모는 D57(주 분석 57)와 O48(현재 L2 준비 48)로 분리했으며 M9(9건)는 L2 미가용으로
검색 불가하여 Recall 분모에서 제외했다. 아래는 `후보 Recall = 정답 target이
candidate union 안에 든 수 / 분모`다.

| cohort/arm | Recall@20 | Recall@50 | Recall@100 |
|---|---:|---:|---:|
| D57 A0 | 10/57 (17.54%) | 17/57 (29.82%) | 17/57 (29.82%) |
| D57 A1 | 12/57 (21.05%) | 20/57 (35.09%) | 20/57 (35.09%) |
| D57 A2 | 12/57 (21.05%) | 20/57 (35.09%) | 21/57 (36.84%) |
| D57 A3 | 12/57 (21.05%) | 21/57 (36.84%) | 21/57 (36.84%) |
| O48 A0 | 7/48 (14.58%) | 12/48 (25.00%) | 12/48 (25.00%) |
| O48 A1 | 8/48 (16.67%) | 14/48 (29.17%) | 14/48 (29.17%) |
| O48 A2 | 8/48 (16.67%) | 15/48 (31.25%) | 16/48 (33.33%) |
| O48 A3 | 9/48 (18.75%) | 16/48 (33.33%) | 16/48 (33.33%) |

A1은 ITEM 원문 질의, A2는 A1 저품질/실패 시 bounded suffix, A3는 동일 receipt의
RRF 재계산이다. D57 기준 A2의 A0 대비 증가는 +2/+3/+4건(Recall@20/@50/@100),
O48 기준 A2의 증가는 +1/+3/+4건이다. `NO_CANDIDATES`는 D57 A2에서
47→45(@20), 40→37(@50), 40→36(@100), O48 A2에서 41→40, 36→33, 36→32로
감소했다.

ITEM-only provenance는 50 receipt rows, 5 unique target/table pairs로 봉인되었다.
대표 table key는 `301:DT_200Y141`, `301:DT_200Y101`, `101:DT_1J22112`,
`101:DT_1J22005`, `133:TX_13301_A011`이며 각 row에 physical request, path,
slot, raw/table rank, RRF contribution이 남아 있다. 검색 결과의 rank나 CONTENTS
일치만으로 셀을 확정한 row는 없다.

## 비용·오류

- shadow physical search calls: 417; ITEM incremental physical calls: 32;
  ITEM logical paths: 112; unique ITEM query: 34; baseline 재사용: 2.
- endpoint error rate: KOSIS 0/136, OpenSearch 0/119, Qdrant 0/162;
  전체 search error rate 0.0, silent fallback 0, authority violation 0.
- 관측된 평균(p95) 지연: KOSIS 683.3ms(856.9ms), BM25 405.5ms(411.3ms),
  dense 546.1ms(552.4ms). ITEM physical call만의 별도 latency field는 receipt
  계약에 없으므로 추정하지 않았다.
- 실패 taxonomy: `ITEM_INELIGIBLE` 59, `L2_UNAVAILABLE` 9(M9),
  `OFFICIAL_ZERO_RESULT` 9, `RRF_CUTOFF` 16, `SOURCE_EXHAUSTED_LT5` 32,
  `RERANKER_LOSS` 0.
- 기존 shadow 실행 시점에는 reranker가 unavailable하여 전체 rerank Recall은
  `BLOCKED_RERANKER_UNAVAILABLE`로 기록되었다. 운영 반영 후에는 새 서비스의 직접
  strict-client 호출, Retriever smoke, 기사 E2E 및 access log로 rerank 경로를
  별도 검증했으며, 57건 재채점 수치로 바꾸어 쓰지 않았다.

## EC2 운영 반영 증거

### release와 설정

- EC2 release: `/srv/news_verification/application-overlay/releases/049c2c84a6be0fa421812fdb8cd9daa273da8211`
- release SHA: `049c2c84a6be0fa421812fdb8cd9daa273da8211`; release worktree dirty count 0.
- source repo(`ec2/release-bound-live-stage`)는 `bc8b27175d7c06a53101104441b22afe7c6a887f`/clean으로
  관측되었고, release는 그와 별도의 immutable worktree다.
- `BGE_RERANKER_ENABLED=true`, URL `http://bge-reranker:8102`,
  model `dragonkue/bge-reranker-v2-m3-ko`, model manifest
  `c139f85e7e44c2dd511bb4ca369edb4ce6fbff16cf1b7c76d91462821facd334`,
  service contract `bge-reranker-v2-m3-ko-service-v2`, max candidates 50.
- runtime/server env 원본은 mode 600으로 backup했다:
  `backups/runtime.env.before-item-bge-20260829`,
  `backups/server.env.before-item-bge-20260829`.
- 불완전 clone은 삭제하지 않고
  `/srv/news_verification/application-overlay/backups/049c2c84a6be0fa421812fdb8cd9daa273da8211.incomplete-20260829`
  로 이동했다.

### 컨테이너 전후

API와 Nginx는 새 release로 recreate했고, reranker만 새로 기동했다. encoder와 data
layer는 재생성하지 않았다. 아래 ID는 after receipt의 전체 ID다. 모든 after
`restart_count=0`이다.

| service | after container ID | image digest | 비고 |
|---|---|---|---|
| nginx | `c75b4af2fe4062266d92c2ef1318afb1b01db48fdc4f220d5284e0199e1ea8d5` | `sha256:5e4bdd1053830252daad619592ffd31bb5cf5c1031e33b40395d5e4db2562ed8` | release label 049c2c84 |
| api | `f6ec370c26c733fe74955d5f70d2d5f5849b990dae46ff6f7398e274198129bc` | `sha256:3c6a60bf8a211ead4ff1e14f2b88f8ec720471fb9c4fbe92322f9d02c2b5315e` | release label 049c2c84 |
| bge-reranker | `725f9b2f5ff0addcf56c6716ddf8d72fdd5c157f9e8f2d51279a827c1387ebc4` | `sha256:b9a096ea460c1bb8b047bacdd36291458b88adf08b32563a81ac8bb995993f0d` | healthy, internal-only |
| bge-query-encoder | `288d4c542e8220588910ee5787466f6bb615217b5396eb8fdc1a581ba0d130bd` | `sha256:666dea4633e530cda4959c2b5682920ff408e8754b58fe728d787256bae9beb3` | 기존 ID/시각 유지 |
| PostgreSQL | `04a93362057869d71e43537bbb1d9b965c5c2b87a38ef80ab0702a32e6079124` | `sha256:0933d60933003cb2d0e4f074ba8a83542fe203803fece22be97795e06ccbdfdc` | 기존 data layer |
| OpenSearch | `592b675a4c74c56088d842368b7e0d631decae5e24e9688b3fc55b7305fa7537` | `sha256:b6c3071dde7b170d85f3a44b9c4ef1cae2e7a23f47448ffd7a2538524476d864` | 기존 data layer |
| Qdrant | `43e4a947fa8b076b53f60840249f570964b84c1617ae26bc686ed3471f68f668` | `sha256:6c0652f8d6925b22f2f6f0e0a5365a6c9dbc8768bd6e70ccc1cdc14847e452a0` | 기존 data layer |
| redis-session | `b6f872a38289bec09e07dd8b4ab51cc8590fc7548eeeb28c2616d7f5e59a207a` | `sha256:1feed93082d872fa051fb6b5787a247d39e9c0d00357404c3a6554ba5e4a5315` | 기존 data layer |

reranker는 `deploy_encoder_internal`에만 연결되고 published host port가 없다.
모델 mount는 `RW=false`/`ro`, root filesystem read-only, `cap_drop=ALL`,
`no-new-privileges`다.

### health와 실제 동작

- SNI HTTPS `/health`: HTTP 200 `{"status":"ok"}`.
- SNI HTTPS `/api/version`: HTTP 200, release SHA가 위 `049c2c84...`와 일치,
  runtime manifest SHA `30981c89ca734ad03771b12abb6bb93e98913e3c2f876ffdfadcaa667663417f`.
- release-bound preflight: `READY`, `ranking_mode=MODEL_RERANKER`, `read_only=true`;
  PostgreSQL `transaction_read_only=on`, OpenSearch/Qdrant concrete release와 count 일치.
- 새 API 컨테이너 안 strict `HttpRerankerClient` health/identity/rerank test:
  v2 contract, expected model/revision, 2 candidates/2 results, top `org:1`.
- Retriever smoke: candidate union 100, rerank input 50, `item_official` status `OK`,
  path error 0, contract `operational-retrieval-v5-item-official`, query register
  `six-path-v3-item-official-optin`.
- 실제 public `POST /api/v1/verify/develop` smoke:
  HTTP 200, `status=completed`, `live=true`, `result_count=1`.
  해당 후 API log의 요청과 대응하여 reranker log에 `POST /rerank` HTTP 200이
  반복 기록되었다. smoke verdict는 `mismatch`였으며, 이는 reranker 경로가
  실행되었다는 증거이지 후보 효과의 별도 정답률로 해석하지 않았다.
- public `/api/v1/tables`는 HTTP 200/20 items/total 100이며 source는 의도대로
  기존 `hybrid_rrf`다. 이 endpoint를 BGE 경로로 바꾸지 않았다.

## 무변경·품질 검증

- 동결 gold 100 rows, scorer, preregistration, historical receipt는 shadow
  manifest의 `validation_pass=true`와 frozen asset SHA 비교로 보존되었다.
- EC2 PostgreSQL/OpenSearch/Qdrant/redis/encoder의 ID·image·start 시각·restart
  count는 배포 전후 동일하다. index/collection/alias/release pointer/data 적재/
  Cell API 쓰기는 수행하지 않았다.
- clean worktree focused suite: **52 passed**; compileall PASS; `git diff --check` PASS.
  전체 backend suite는 기존 전역 module-stub collection 충돌과 기존 503 봉인 테스트가
  있어 전체 기준선 통과로 주장하지 않는다. 이번 변경의 focused regression에는
  신규 실패가 없었다.
- 원래 dirty handoff(`github_handoff_likelion5`)는 읽거나 덮어쓰지 않았고, 사용자가
  제공한 예비 SHA `8d395...`와 실행 전 fresh source 관측 `bc8b271...`의 차이는
  사실로 기록만 했다.

## 후속 한계

1. 후보 생성 GO는 D57/O48 수치와 운영 smoke로 충분히 확인했지만, BGE 활성 상태의
   D57/O48 전체 rerank Recall@20/@50/@100 재채점은 아직 없다.
2. ITEM physical call의 독립 latency field가 shadow receipt에 없어 endpoint별
   평균으로만 비용을 보고했다.
3. rerank 이후 최종 답변 품질과 candidate miss 외 오류 분포는 별도 평가가 필요하다.

따라서 이번 배포는 **후보 생성 개선을 운영에 반영한 상태**로 마감하고,
후속 rerank 전·후 비교를 완료하기 전까지 `GO_FULL_RERANK_QUALITY`로 확대하지 않는다.

봉인 receipt SHA-256: `568e82ecdb1fd802789a384a6f96c047bc5750284716f0cc5473aaee0036451`.
EC2 server-only receipt 경로는
`/srv/news_verification/application-overlay/receipts/049c2c84a6be0fa421812fdb8cd9daa273da8211/RECEIPT_ITEM_OFFICIAL_BGE_PROMOTION_20260829.json`이다.
