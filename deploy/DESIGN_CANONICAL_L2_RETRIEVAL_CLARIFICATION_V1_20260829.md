# Canonical L2·6경로 검색·재질의 상태 머신 통합 설계 v1

- 작성일: 2026-08-29
- 역할: Technical design only
- 기준 worktree: `E:\news_verification_1\worktrees\ec2-single-sha-20260829`
- 기준 Git SHA: `238a66bea876728a15198b7713d36801d292e27a`
- 분기 관계: `origin/develop`이 기준 SHA의 ancestor임을 확인함
- 문서 성격: 구현 전 설계. 이 문서 외 코드·테스트·manifest·동결 자산은 이 단계에서 변경하지 않는다.
- 목표 계약: `canonical-l2-retrieval-clarification-v1`

## 0. 판정과 최종 목표

현재 runtime에는 필요한 구성요소가 부분적으로 존재하지만 세 경계가 하나의 권위 계약으로 닫혀 있지 않다.

1. `run_l2_segmentation.py::run()`은 HCX 결과를 바로 span resolver에 통과시켜 raw model output과 canonical L2를 분리하지 않는다.
2. `l2_segmentation.py::resolve_hcx_model_span()`은 exact match 외에 quote/whitespace/morphological containment까지 허용하므로 source repair의 허용 범위가 목표보다 넓다.
3. `operational_retrieval_v2.py::build_query_register()`는 아직 `sentence:official`을 포함한 7경로를 생성하고, `retrieve_parallel()`은 한 path의 `future.result()` 예외가 전체 retrieval을 중단시킬 수 있다.
4. `failure_recovery_shadow_v1.py`와 `run_pipeline_operational_v2.py`에는 clarification·corrective retrieval·binding continuation 골격이 있지만, 누락 필드와 후보 부족을 아래 7개 상태로 단일 분류하지 않는다.

이 설계의 최종 실행 순서는 다음과 같다.

```text
기사 입력
  -> L1
  -> raw HCX L2
  -> canonical L2
  -> L3~L5
  -> missing-field coordinator
       |-- DIRECT_FIELD_MISSING -> Clarification Plan
       `-- 검색 입력 충분 -> versioned 6-path Round 0 retrieval
            -> canonical profile materialization
            -> Late Binding + Strict Validator
                 |-- RETRIEVAL_INSUFFICIENT -> Corrective Retrieval 최대 1회
                 |-- METADATA_PROFILE_INCOMPLETE -> 불완전 후보 제외/명시적 종료
                 |-- SELECTOR_CLARIFICATION_POSSIBLE -> profile 기반 재질의
                 |-- CORRECTIVE_RETRIEVAL_EXHAUSTED -> UNVERIFIABLE_FINAL
                 `-- QUERY_READY -> KOSIS Cell API -> deterministic 공식값 답변
```

최종 답변의 권위는 `공식 통계값·기간·단위·지역·표·release`이다. 단순 일치/불일치 라벨이나 HCX 문장은 셀·판정 권위를 갖지 않는다.

## 1. 전역 불변조건

구현과 리팩터링은 다음을 완화할 수 없다.

1. raw HCX는 관측 자료이며 downstream authority가 아니다.
2. canonical L2만 L3~L5와 retrieval에 들어간다.
3. source repair는 기사 원문에 존재하는 유일한 연속 substring을 증명할 때만 허용한다.
4. 숫자, 단위, 시점, 비교 연산자, 증감 방향은 repair하지 않는다.
5. 검색 hit는 후보 표 membership 권위만 갖는다. ITEM/DIMENSION hit를 selector 값 증거로 승격하지 않는다.
6. 사용자 답변은 `SemanticConstraint`이며 `table_key`, `axis_id`, `value_id`, `objL*`를 직접 확정하지 않는다.
7. 사용자 constraint를 모든 유효 후보에 다시 투영한 뒤 전역 유일성이 증명되어야 `QUERY_READY`가 된다.
8. `QUERY_READY` 이전 KOSIS Cell API 호출은 항상 0이다.
9. Corrective Retrieval은 target당 최대 1 round이며 셀, verdict, answer 생성 권한이 없다.
10. speculative retrieval 결과는 선택지 제안 권위만 갖고 최종 candidate membership으로 재사용하지 않는다.
11. historical receipt와 frozen asset bytes를 수정하지 않는다.
12. 특정 기사 문장, 특정 표 이름, 특정 수치에 대한 literal 예외를 구현하지 않는다.

## 2. 요구사항 추적표

| ID | 요구사항 | 구현 권위 위치 | 완료 증거 |
|---|---|---|---|
| L2-01 | raw HCX와 canonical L2 분리 | `src/develop/l2_segmentation.py`, packaged mirror | raw/canonical 별도 schema 및 SHA receipt |
| L2-02 | exact-only source repair | `resolve_prediction()` 전용 source canonicalizer | exact/0-hit/multi-hit 회귀 테스트 |
| L2-03 | 5개 canonical 상태 | `materialize_operational_l2()`, trace projection | 상태별 fixture와 downstream eligibility |
| L2-04 | 숫자·단위·시점 무복구 | canonicalizer와 L3~L5 strict gate | mutation 0, fail-closed 테스트 |
| FP-01 | 3 fingerprints | stage 02, retrieval, final evidence ledger | 반복 실행 fingerprint 비교 receipt |
| RET-01 | versioned 6-path register | packaged `operational_retrieval_v2.py` | register SHA와 path set 테스트 |
| RET-02 | path isolation | `retrieve_parallel()` | 단일 path 실패 시 partial audit, 전체 권위 보존 |
| RET-03 | differential gate | focused retrieval/evaluation tests | 신규 loss 0, 기존 pass 감소 0 |
| SM-01 | 7상태 coordinator | `failure_recovery_shadow_v1.py`, runtime orchestrator | 전이표 기반 단위·통합 테스트 |
| SM-02 | dependency-aware resume | backend checkpoint + runtime continuation | 재개 지점별 physical call receipt |
| SM-03 | candidate bundle sealing | checkpoint store | SHA mismatch invalidation 테스트 |
| SM-04 | corrective round 최대 1회 | recovery plan + checkpoint | round 2 진입 0 |
| CELL-01 | QUERY_READY 이후 Cell API | runtime + `fetch_exact_single_cell()` guard | pre-ready cell calls 0 |
| FE-01 | frontend 재질의 왕복 | `frontend/src/api.js`, `ChatApp.jsx` | token/plan/option invalidation E2E |
| CLO-01 | source/deploy closure | mirror map + runtime manifest | mirror parity 및 74-file closure |

## 3. Raw HCX와 Canonical L2 계약

### 3.1 현재 경계와 변경 지점

현재 경로는 다음과 같다.

```text
call_hcx_l2_segmentation()/call_hcx_l2_split()
  -> resolve_prediction()
  -> run_l2_segmentation.run()
  -> materialize_operational_l2()
  -> project_trace_operational_l2()
```

`call_hcx_l2_segmentation()`과 `call_hcx_l2_split()`이 이미 resolved prediction을 반환하므로 raw와 canonical의 byte 경계가 없다. 구현에서는 새 파일을 만들지 않고 기존 `l2_segmentation.py` 안에서 책임을 다음처럼 분리한다.

```text
call_hcx_l2_raw(...)
  -> RawL2Envelope
canonicalize_l2_prediction(article_text, RawL2Envelope)
  -> CanonicalL2Envelope
```

기존 public 함수는 compatibility wrapper로 유지하되 내부적으로 위 두 단계를 호출한다. 구현자가 raw 응답을 canonical object 위에 덮어쓰면 안 된다.

### 3.2 RawL2Envelope

Raw object는 HCX가 반환한 model JSON을 수정하지 않은 형태로 보존한다.

```json
{
  "contract_version": "raw-hcx-l2-v1",
  "article_idx": "...",
  "model": "HCX-007",
  "generation_config_sha256": "...",
  "raw_prediction": {},
  "raw_prediction_sha256": "...",
  "attempt": 1,
  "transport_status": "OK"
}
```

다음 값은 raw fingerprint 입력에서 제외한다.

- request ID
- 호출 시각
- latency
- token usage
- transport 재시도 대기시간

원문, API key, HTTP header, raw transport body는 public receipt에 노출하지 않는다. 내부 stage artifact에는 parsed model JSON만 두고 secret scan을 통과해야 한다.

### 3.3 CanonicalL2Envelope

Canonical object는 raw object를 참조하되 downstream이 사용할 수 있는 필드만 가진다.

```json
{
  "contract_version": "canonical-l2-v1",
  "article_idx": "...",
  "status": "L2_READY",
  "reason_code": null,
  "raw_prediction_sha256": "...",
  "resolver_version": "exact-source-resolver-v1",
  "repair_reason_code": null,
  "predictions": [],
  "canonical_l2_sha256": "..."
}
```

Canonical prediction의 offset은 반드시 `article_idx + sentence_id + article body SHA`에 결박한다. 모든 `source_char_start/end`는 원문 byte가 아니라 Python string character offset이며, 해당 slice가 `source_span_text`와 정확히 일치하는지 다시 검사한다.

### 3.4 Exact-only source repair

source repair 대상은 `source_region.source_span_text`와 source attribution pointer뿐이다. 다음 순서 외의 복구는 금지한다.

1. raw span이 현재 sentence의 정확한 substring인지 검사한다.
2. 없으면 article sentence inventory 전체에서 동일한 raw span의 정확한 substring occurrence를 센다.
3. 정확히 1개면 해당 원문 slice와 sentence ID를 사용해 `REPAIRED_SOURCE_EXACT`로 승격한다.
4. 0개면 `HOLD_NOT_FOUND`이다.
5. 2개 이상이면 `HOLD_AMBIGUOUS`이다.

`exact`의 의미는 원문 character sequence가 동일하다는 뜻이다. source repair에서는 다음을 허용하지 않는다.

- 형태소 prefix/containment
- semantic similarity
- embedding 또는 BM25 검색
- KOSIS 표·기관 metadata 참조
- LLM 재질의로 source를 추정
- quote glyph, whitespace, punctuation을 무시한 채 model span 자체를 canonical source로 저장

표기 정규화가 후보 위치를 제안하는 데 사용되더라도 최종 repair는 원문 exact slice의 유일성과 ownership을 별도로 증명해야 한다. 제안된 위치가 두 개 이상이거나 source ownership이 둘 이상이면 `HOLD_AMBIGUOUS`이다. 현재 `resolve_hcx_model_span()`의 `MORPHOLOGICAL_CONTAINMENT`는 source repair 경로에서 사용하지 않는다. indicator scope의 기존 호환 처리는 source repair와 분리하고, source 결과를 승격하는 근거가 될 수 없다.

### 3.5 Canonical 상태

| 상태 | 의미 | downstream 진입 |
|---|---|---|
| `L2_READY` | raw의 모든 필수 source span이 원문 exact slice와 이미 일치 | 허용 |
| `REPAIRED_SOURCE_EXACT` | 유일한 원문 exact source span으로만 복구됨 | 허용 |
| `HOLD_NOT_FOUND` | source span의 exact occurrence가 없음 | 금지 |
| `HOLD_AMBIGUOUS` | exact occurrence 또는 source ownership이 복수 | 금지 |
| `L2_UNAVAILABLE` | HCX transport/schema/receipt 오류 또는 canonical object 생성 불가 | 금지 |

현재 `materialize_operational_l2()`가 article 단위로 `L2_READY/L2_UNAVAILABLE`만 생성하고, `run_article_body_pipeline_trace_v1.py`와 operational runner가 `L2_READY`만 allowlist에 넣는 위치를 함께 변경해야 한다. downstream allowlist는 정확히 다음 두 상태다.

```python
DOWNSTREAM_L2_ELIGIBLE = {"L2_READY", "REPAIRED_SOURCE_EXACT"}
```

HOLD는 서버 오류가 아니라 안전한 결과다. API는 generic 502가 아니라 target/article별 limitation을 반환한다.

### 3.6 숫자·단위·시점 무복구

Canonical L2는 다음 값을 새로 생성하거나 교정하지 않는다.

- L1 숫자 span과 값
- 단위
- 절대·상대 시점
- 비교 기준 시점
- 증가/감소 방향
- 비율/증가량/수준 measurement basis

raw와 원문/L1 evidence가 맞지 않으면 기존 L1/L3/L4/L5 strict validator로 전달하거나, 필수 canonical field 생성이 불가능하면 `L2_UNAVAILABLE`과 bounded reason code로 중단한다. 검색 후보나 사용자 선택을 이용해 과거 L2 값을 소급 수정하지 않는다.

### 3.7 L2 receipt 필드

stage 02 operational manifest에 article별 다음 allowlist를 추가한다.

```text
raw_contract_version
canonical_contract_version
raw_prediction_sha256
canonical_l2_sha256
resolver_version
repair_reason_code
raw_status
canonical_status
source_exact_match_count
source_owner_sentence_id
source_span_sha256
unresolved_span_count
unresolved_span_details
downstream_eligible
```

`repair_reason_code`는 닫힌 vocabulary를 사용한다.

```text
NONE
SOURCE_EXACT_LOCAL
SOURCE_EXACT_CROSS_SENTENCE
SOURCE_EXACT_NOT_FOUND
SOURCE_EXACT_AMBIGUOUS
SOURCE_OWNERSHIP_AMBIGUOUS
RAW_SCHEMA_INVALID
HCX_CALL_FAILED
NON_SOURCE_REPAIR_FORBIDDEN
```

기존 `UNRESOLVED_SPANS`의 `count == len(details)` 계약은 유지한다. raw detail과 canonical disposition을 분리해 생산자/소비자 format drift가 다시 502로 전파되지 않게 한다.

## 4. 세 Fingerprint 계약

모든 fingerprint는 UTF-8, key sort, compact separator를 사용하는 canonical JSON의 SHA-256이다.

### 4.1 `canonical_l2_sha256`

포함:

- canonical contract version
- article body SHA
- article/sentence IDs
- canonical status/reason
- indicator/source exact spans와 offsets
- source ownership pointer
- period context의 raw evidence
- L1 value span references

제외:

- raw request ID, timestamps, latency, token usage
- retry count
- 로그 문구

### 4.2 `retrieval_semantic_sha256`

포함:

- canonical L2 SHA
- L3~L5 normalized retrieval fields
- article-date provenance SHA
- user semantic constraints와 answer SHA
- query register contract/version/SHA
- role별 normalized query text SHA
- release binding SHA
- path budgets, RRF k, path/union top-k
- corrective round number와 corrective plan SHA

제외:

- transport latency
- physical worker completion order
- raw vector와 검색 원문 public projection

### 4.3 `final_evidence_sha256`

포함:

- retrieval semantic SHA
- candidate membership SHA
- selected table key와 release ID
- canonical/projection profile SHA set
- query-ready receipt와 query-plan SHA
- Cell API request/response SHA
- 공식 `DT`, unit, period, region/selector display evidence
- deterministic comparator result와 answer packet SHA

Cell 조회가 없는 HOLD/UNVERIFIABLE은 `final_evidence_sha256=null`이고 terminal reason receipt SHA를 별도로 기록한다. HCX verbalizer 결과는 fingerprint authority에 포함하지 않는다.

### 4.4 안정성 gate

동일 article body, 동일 user constraints, 동일 release/config에서 반복 실행한다.

- raw HCX SHA는 달라도 허용한다.
- canonical L2 SHA는 동일해야 한다.
- retrieval semantic SHA는 동일해야 한다.
- 공식 셀까지 도달한 경우 final evidence SHA는 동일해야 한다.

성공 선언 문구는 “raw HCX가 동일하다”가 아니라 “raw 변동에도 canonical L2와 downstream evidence가 동일하다”로 제한한다.

## 5. Versioned 6-path Query Register

### 5.1 기본 register

`deploy/pipeline_runtime/src/news_verification/runtime/operational_retrieval_v2.py::build_query_register()`의 기본 path set을 다음으로 변경한다.

```text
indicator:official
indicator:bm25
indicator:dense
item:bm25
item:dense
sentence:dense
```

`sentence:official`만 제거한다. `sentence:dense`와 `indicator:official`은 유지한다. sentence query의 숫자 제거 규칙도 유지한다.

계약 version은 `operational-retrieval-v3-six-path`로 올리고 register receipt에는 다음을 기록한다.

```json
{
  "query_register_version": "six-path-v1",
  "enabled_paths": [],
  "disabled_paths": [
    {"path": "sentence:official", "reason": "DISABLED_ZERO_YIELD_BASELINE"}
  ],
  "query_register_sha256": "..."
}
```

source/report context path와 corrective path는 기본 6경로의 분모에 섞지 않는다. 이들은 각각 별도 `context-register-v1`, `corrective-register-v1` round receipt로 기록한다. Corrective path가 official을 사용하는 것은 허용하지만 기본 `sentence:official`을 되살리는 방식이어서는 안 된다.

### 5.2 Path isolation

현재 `retrieve_parallel()`은 `future.result()` 예외를 그대로 올릴 수 있다. 구현 후에는 각 path가 다음 결과 중 하나를 반드시 남긴다.

```text
OK
EMPTY
FAILED_TIMEOUT
FAILED_TRANSPORT
FAILED_CONTRACT
```

격리 규칙:

1. timeout/transport 실패는 해당 path만 제외하고 다른 성공 path를 fusion한다.
2. release ID/SHA mismatch, 허용되지 않은 field, malformed hit는 `FAILED_CONTRACT`이며 해당 target을 fail-closed 처리한다.
3. 성공 path가 하나도 없으면 `RETRIEVAL_INSUFFICIENT` + `ALL_PATHS_FAILED`로 분류하되 Corrective Retrieval을 실행하지 않는다. 이는 semantic 후보 부족이 아니라 retryable transport failure다.
4. 일부 path 성공이면 `PARTIAL_PATH_FAILURE` audit와 함께 계속하되 candidate authority는 성공 path union만 가진다.
5. path failure를 빈 결과로 위장하지 않는다.
6. physical completion order가 RRF 결과에 영향을 주지 않도록 path key와 table key로 재정렬한 뒤 fusion한다.

### 5.3 Differential gate

구현 전 현재 HEAD의 7경로 결과를 immutable baseline receipt로 새 파일에 봉인하고, 동일 입력·release·profile·reranker 설정에서 6경로를 비교한다. historical receipt는 수정하지 않는다.

고정 조건:

```text
same article/target set
same release_id/release binding SHA
same normalized query fields
same path_top_k/union_top_k/RRF_K
same reranker mode and top_k
same profile snapshot
```

필수 비교:

- `sentence:official` unique gold rescue 수
- candidate recall@K 분자/분모
- candidate membership loss/gain
- RRF/reranker rank 변화
- `QUERY_READY` 수와 false-ready 수
- official search physical calls
- retrieval p50/p95
- partial/all-path failure 수

승인 조건:

1. `sentence:official` unique rescue가 0이다.
2. 기존 정답 candidate loss가 0이다.
3. 기존 `QUERY_READY` 감소가 0이다.
4. false-ready 증가는 0이다.
5. 기존 통과 감소 0, 신규 실패 0이다.

하나라도 실패하면 6경로 promotion은 중단한다. 성능 개선만으로 정확성 손실을 승인하지 않는다.

## 6. Clarification·Corrective Retrieval 상태 머신

### 6.1 상태 정의

| 상태 | 진입 조건 | 허용 action | 금지 action |
|---|---|---|---|
| `DIRECT_FIELD_MISSING` | 원문/L1~L5만으로 필수 semantic field 누락 확정 | Clarification Plan, bounded speculative planning | Cell/verdict, 후보 자동 확정 |
| `RETRIEVAL_INSUFFICIENT` | semantic 입력은 충분하지만 Round 0 후보/호환 후보 부족 | eligibility 확인 후 Corrective Retrieval 1회 | 사용자 selector를 임의 추정 |
| `METADATA_PROFILE_INCOMPLETE` | 후보 profile 축/값 inventory가 불완전 | 해당 후보 제외, 대체 후보 탐색 여부 판정 | 불완전 profile에서 option 생성 |
| `SELECTOR_CLARIFICATION_POSSIBLE` | complete 후보들이 동일 필수 selector slot에서 막힘 | profile 기반 option 질문 | 선택값을 셀 좌표로 직접 확정 |
| `CORRECTIVE_RETRIEVAL_EXHAUSTED` | Round 1 후에도 호환 후보 없음 | terminal reason 생성 | Round 2, Cell API |
| `QUERY_READY` | 모든 후보 재검증 후 전역 유일 query plan | Cell API 1회 및 comparator | HCX가 query plan 변경 |
| `UNVERIFIABLE_FINAL` | 안전한 추가 action이 없거나 corrective 소진 | 공식 limitation 답변 | completed/verified로 표시 |

`NO_COMPATIBLE_SERIES`는 상태가 아니라 진단 reason이다. 이 reason만으로 Clarification 또는 Corrective Retrieval을 고르지 않는다.

### 6.2 단일 coordinator

`failure_recovery_shadow_v1.py::plan_failure_recovery()`를 임시 shadow action selector에서 상태 판정 coordinator로 승격한다. 새 파일을 추가하지 않고 다음 typed 결과를 반환한다.

```json
{
  "contract_version": "retrieval-clarification-state-v1",
  "state": "RETRIEVAL_INSUFFICIENT",
  "reason_code": "NO_CANDIDATES",
  "target_id": "...",
  "retrieval_round": 0,
  "allowed_next_actions": ["CORRECTIVE_RETRIEVAL"],
  "state_sha256": "..."
}
```

권한은 다음처럼 분리한다.

- Clarification Planner: 무엇을 사용자에게 물을지 결정
- Corrective Retrieval Planner: 후보를 한 번 더 찾을지 결정
- Late Binder/Strict Validator: `QUERY_READY` 결정
- Cell fetcher: `QUERY_READY` receipt가 있는 단일 셀만 조회
- deterministic renderer: sealed evidence를 사용자 문장으로 투영

### 6.3 실행 순서

#### Gate A — Retrieval 이전

1. indicator/item처럼 검색 membership을 크게 바꾸는 field가 없으면 `DIRECT_FIELD_MISSING`이다.
2. 상대 시점 해석에 article date가 실제로 필요할 때만 `article_date`를 묻는다.
3. indicator/item 누락의 speculative retrieval은 선택지 준비용이다.
4. 사용자가 indicator/item을 보완하면 speculative membership은 폐기하고 full 6-path retrieval을 다시 실행한다.

#### Round 0 — Retrieval과 profile

1. 입력이 충분하면 6-path retrieval을 실행한다.
2. 후보 profile을 release-pinned PostgreSQL에서 읽는다.
3. 불완전 profile은 option source에서 제외하고 exclusion receipt를 남긴다.
4. complete 후보로 Late Binding과 Strict Validator를 실행한다.

#### Corrective Retrieval

다음 조건을 모두 만족할 때만 실행한다.

- state가 `RETRIEVAL_INSUFFICIENT`
- indicator/item/period 등 검색 semantic input이 충분함
- Round 0 transport가 최소 한 path에서 정상 완료됨
- deterministic corrective terms가 존재함
- retry budget `used=0`, `limit=1`

Round 1 결과는 `Round 0 ∪ Round 1`로 lossless merge하고 전체 후보를 다시 profile/Late Binding/Strict Validator에 통과시킨다. Round 1이 후보 membership을 바꾸면 기존 clarification plan과 option bundle은 무효다.

#### Gate B — Post-binding clarification

complete 후보들이 같은 semantic selector role에서 `MISSING/AMBIGUOUS`일 때만 `SELECTOR_CLARIFICATION_POSSIBLE`이다. profile마다 요구 role이 다르면 먼저 indicator/table family를 좁히는 상위 질문을 만든다.

사용자 선택은 sealed applicability와 대조한 뒤 `SemanticConstraint`로 저장한다. 그 뒤 모든 후보에 Late Binding + Strict Validator를 다시 실행한다. 한 후보의 `axis_id/value_id`를 직접 채워 `QUERY_READY`로 우회하면 안 된다.

### 6.4 Profile incomplete 처리

`METADATA_PROFILE_INCOMPLETE`는 자동으로 Corrective Retrieval을 의미하지 않는다.

1. 불완전 후보만 제외한다.
2. complete 후보가 남으면 해당 후보로 계속한다.
3. complete 후보가 없고 deterministic corrective terms로 다른 표를 찾을 가능성이 있을 때만 Round 1을 허용한다.
4. 그렇지 않으면 `UNVERIFIABLE_FINAL`이다.
5. 불완전 profile의 label/axis/value는 사용자 option에 절대 포함하지 않는다.

### 6.5 Corrective round 상한

retry budget은 runtime memory뿐 아니라 checkpoint와 target ledger에 봉인한다.

```json
{
  "corrective_round": 1,
  "retry_budget": {"used": 1, "limit": 1},
  "round0_membership_sha256": "...",
  "round1_membership_sha256": "...",
  "union_membership_sha256": "...",
  "corrective_plan_sha256": "..."
}
```

resume이나 재시도로 `used=1`인 target에 Round 2를 시작하면 `CORRECTIVE_RETRIEVAL_EXHAUSTED`로 종료한다.

## 7. Dependency-aware Resume

### 7.1 Invalidation matrix

| 보완 role | 재사용 | 무효화 | resume stage |
|---|---|---|---|
| `article_date`, `period` | L1, canonical L2 | period normalization 이후 | `layers` |
| `indicator`, `item` | L1, canonical L2, raw article | speculative/final retrieval 이후 전부 | `retrieval` |
| `unit`, `source`, `population` | L1, canonical L2 | retrieval/binding/cell/answer | `retrieval` |
| `region`, `sex`, `age`, `classification`, `measurement_basis` | L1/L2/layers + sealed candidate/profile bundle | binding/cell/answer | `binding` |

selector-only binding resume는 candidate bundle의 모든 SHA가 일치할 때만 허용한다. 불일치 시 조용히 retrieval을 재실행하지 말고 `RESUME_ARTIFACT_INVALIDATED`를 반환한 뒤 새 plan을 생성하는 명시적 경로를 사용한다.

### 7.2 Candidate bundle sealing

현재 `build_post_binding_clarification_plan()`과 `verification_checkpoint_store.create()`가 membership/profile/binding continuation 일부를 봉인한다. 이를 다음 contract로 확장한다.

```text
contract_version
target_scope_sha256
release_id
release_binding_sha256
runtime_manifest_sha256
query_register_version
query_register_sha256
retrieval_rounds = [0] or [0, 1]
round0_membership_sha256
round1_membership_sha256
candidate_membership_sha256
profile_sha_set
profile_bundle_sha256
projection_bundle_sha256
corrective_plan_sha256
clarification_plan_sha256
option_bundle_sha256
resume_from_stage
resume_generation
expires_at
```

`profile_sha_set`은 `table_key + release_id + profile_sha256` 정렬 배열의 SHA를 포함한다. candidate order가 바뀌어도 membership SHA는 정렬된 unique table key로 계산하고, ranking receipt는 별도 SHA로 둔다.

### 7.3 Invalidation rules

다음 중 하나라도 바뀌면 기존 plan/option/binding continuation을 재사용하지 않는다.

- release ID 또는 release binding SHA
- query register version/SHA
- retrieval rounds
- candidate membership SHA
- profile/projection SHA set
- corrective plan SHA
- target scope
- runtime manifest SHA
- checkpoint TTL/generation

Round 1이 추가되면 Round 0만으로 만든 option은 항상 폐기한다. 새 candidate bundle에서 새 clarification plan을 생성한다.

### 7.4 Resume call invariants

- 모든 resume에서 L1 physical call 0
- 모든 resume에서 HCX L2 physical call 0
- `layers` resume은 period normalization 이후만 재계산
- `retrieval` resume은 final retrieval physical call `>0`
- `binding` resume은 retrieval/BGE/OpenSearch/Qdrant physical call 0
- `binding` resume은 sealed profiles로 전체 후보 revalidation
- invalid bundle에서 Cell API 0

## 8. Cell API 권한 경계

현재 `run_new_articles_v2()`는 `top50.resolution.outcome == "QUERY_READY"`일 때 `fetch_exact_single_cell()`을 호출한다. 이 호출부 조건뿐 아니라 fetch 함수 자체에서도 권한을 검증하도록 변경한다.

권장 signature:

```python
fetch_exact_single_cell(
    query_plan,
    cell_fetcher,
    *,
    query_ready_receipt,
    candidate_bundle_sha256,
)
```

필수 검증:

- state == `QUERY_READY`
- query-ready receipt SHA 유효
- query plan SHA가 receipt와 동일
- selected table/release/profile SHA가 candidate bundle에 포함
- item, objL1..objL8, prdSe, period가 유일
- target call ledger의 기존 cell count가 0

실패하면 Cell API를 호출하지 않고 bounded error를 반환한다. 공식 셀 성공 뒤 deterministic comparator와 sealed renderer만 사용자 답변을 만든다.

## 9. Source/Deploy Duplicate Closure 동기화

### 9.1 현재 실제 import authority

`backend/develop_verify_service.py::_load_trace_runner()`는 `PIPELINE_RUNTIME_ROOT`를 `sys.path` 맨 앞에 두고 다음을 import한다.

- `src.develop.run_article_body_pipeline_trace_v1`
- `src.news_verification.runtime.run_pipeline_operational_v2`

API image는 `deploy/api.Dockerfile`에서 `deploy/pipeline_runtime/src`와 `manifest.json`만 복사한다. 따라서 EC2 실행 권위는 repository root의 `src/develop`이 아니라 packaged closure다.

### 9.2 동기화 map

| 책임 | repository source | deploy closure | 규칙 |
|---|---|---|---|
| HCX/raw/canonical L2 | `src/develop/l2_segmentation.py` | `deploy/pipeline_runtime/src/develop/l2_segmentation.py` | 기능 byte 동기화. timeout 기본값도 caller 정책으로 통일 |
| L2 producer/receipt | `src/develop/run_l2_segmentation.py` | `deploy/pipeline_runtime/src/develop/run_l2_segmentation.py` | 기능 byte 동기화 |
| exact span primitive | `src/develop/l2_span_resolver.py` | `deploy/pipeline_runtime/src/develop/l2_span_resolver.py` | 기능 byte 동기화 |
| recovery/clarification policy | `src/develop/failure_recovery_shadow_v1.py` | `deploy/pipeline_runtime/src/develop/failure_recovery_shadow_v1.py` | byte-identical 유지 |
| operational retrieval | repository legacy reference가 아닌 packaged canonical을 권위로 사용 | `deploy/pipeline_runtime/src/news_verification/runtime/operational_retrieval_v2.py` | 실제 구현은 canonical package 한 곳; `deploy/.../src/develop/operational_retrieval_v2.py`는 shim 유지 |
| operational orchestrator | repository legacy reference가 아닌 packaged canonical을 권위로 사용 | `deploy/pipeline_runtime/src/news_verification/runtime/run_pipeline_operational_v2.py` | 실제 구현은 canonical package 한 곳; deploy `src.develop` shim 유지 |
| trace stage | `src/develop/run_article_body_pipeline_trace_v1.py` | `deploy/pipeline_runtime/src/develop/run_article_body_pipeline_trace_v1.py` | 현재 drift를 숨기지 말고 packaged 권위를 source mirror에 동기화하거나 source를 명시적 shim으로 전환 |

구현자는 같은 로직을 세 위치에 독립적으로 편집하면 안 된다. canonical package와 compatibility shim의 역할을 유지하고, mirror set만 명시적으로 동기화한다.

### 9.3 Closure 절차

1. source 변경을 완료한다.
2. mirror map에 따라 packaged closure를 동기화한다.
3. canonical package implementation을 수정한다.
4. `deploy/pipeline_runtime/src/develop/operational_*` shim은 로직 없이 그대로 둔다.
5. source/deploy parity test를 실행한다.
6. 최종 bytes가 확정된 뒤 `deploy/release_manifest.py::refresh_runtime_manifest()`를 한 번만 실행한다.
7. `verify_runtime_closure()`가 74/74를 증명한다.
8. API/frontend release SHA가 같은 commit인지 확인한다.

manifest를 중간 구현 단계마다 갱신하거나 historical manifest/receipt를 덮어쓰지 않는다.

## 10. Backend/API와 Frontend 계약

### 10.1 Backend public response

재질의 응답은 기존 `/api/v1/verify/develop` 경로를 유지하고 다음 필드를 반환한다.

```json
{
  "status": "awaiting_clarification",
  "pipeline_state": "SELECTOR_CLARIFICATION_POSSIBLE",
  "reason_code": "REGION_UNBOUND",
  "question": {
    "id": "...",
    "role": "region",
    "prompt": "...",
    "input_mode": "SEARCHABLE_OPTIONS",
    "allow_direct_input": false,
    "options": []
  },
  "resume_token": "...",
  "resume_from_stage": "binding",
  "clarification_plan_sha256": "...",
  "candidate_bundle_sha256": "..."
}
```

options 조회는 기존 `/api/v1/verify/develop/options`를 유지한다. 이 endpoint는 봉인된 bundle paging/search만 수행하며 retrieval, profile transport, HCX, Cell API를 호출하지 않는다.

### 10.2 Answer submission

frontend는 기존 `credentials: "include"`를 유지하고 다음을 함께 전송한다.

- 원문 text/title/date
- `resume_token`
- 누적 `clarification_answers`
- 새 답변의 `question_id`, `role`, `value`, 필요한 경우 `option_id`

backend는 pending question과 새 답변이 정확히 1:1인지 확인한다. option-only 질문은 현재 bundle의 option ID와 display label이 함께 일치해야 한다.

### 10.3 Frontend invalidation

`frontend/src/ChatApp.jsx`는 다음 상태를 분리한다.

- pending question
- resume token/stage
- plan SHA/candidate bundle SHA
- current option query/page
- selected option
- clarification history

새 plan SHA 또는 candidate bundle SHA가 오면 기존 option page와 selected option을 즉시 초기화한다. 현재 표시 bundle에 속하지 않은 option을 제출하지 않는다. 모든 값에 도달할 수 있도록 search/pagination을 유지하며 내부 `table_key/axis_id/value_id/profile_sha`는 화면에 노출하지 않는다.

### 10.4 User-visible terminal states

- `awaiting_clarification`: 사용자의 정보가 필요함
- `processing/retrying`: transport failure로 재시도 가능
- `completed`: 적어도 하나의 official cell evidence가 있음
- `unverifiable`: `UNVERIFIABLE_FINAL`이고 이유가 있음

Cell API 0인 결과를 `completed`로 표시하지 않는다.

## 11. 구현 파일별 상세 handoff

### 11.1 L2

| 파일 | 함수 | 설계 작업 |
|---|---|---|
| `src/develop/l2_segmentation.py` + mirror | `call_hcx_l2_segmentation`, `call_hcx_l2_split` | raw envelope와 canonicalization 분리 |
| 위 파일 | `resolve_prediction` | source 전용 exact-only resolver 적용, 형태소 source repair 제거 |
| 위 파일 | 신규 내부 canonicalizer helpers | 5상태 및 reason receipt 생성 |
| `src/develop/run_l2_segmentation.py` + mirror | `run` | raw/canonical artifacts·manifest·fingerprint 기록 |
| packaged `run_pipeline_operational_v2.py` | `_trace_safe_error`, `materialize_operational_l2`, `project_trace_operational_l2` | canonical 상태 소비와 bounded projection |
| packaged `run_article_body_pipeline_trace_v1.py` | `run_l2_stage`, `run_layers_stage` | stage 02 artifact/manifest와 eligible status allowlist 갱신 |

### 11.2 Retrieval

| 파일 | 함수 | 설계 작업 |
|---|---|---|
| packaged `operational_retrieval_v2.py` | `CONTRACT_VERSION`, `build_query_register` | 6경로 register와 disabled path receipt |
| 위 파일 | `retrieve_parallel` | per-path result isolation, deterministic fusion, partial audit |
| packaged `run_pipeline_operational_v2.py` | `_retrieve_with_request_cache` 및 call ledger projection | register version/SHA를 cache/checkpoint identity에 포함 |
| `tests_backend/test_article_multi_target_v1.py` | channel count expectation | official 1, BM25 2, dense 3 기본 경로 계약 반영 |

### 11.3 State machine/resume

| 파일 | 함수 | 설계 작업 |
|---|---|---|
| `src/develop/failure_recovery_shadow_v1.py` + mirror | `plan_failure_recovery` | 7상태 coordinator와 max-one-round 판정 |
| 위 파일 | `build_post_binding_clarification_plan` | complete profile 공통 slot, bundle SHA, exclusions |
| packaged `run_pipeline_operational_v2.py` | `_speculative_clarification_plan` | planning-only 권위와 6경로 register identity |
| 위 파일 | `_merge_user_clarifications`, `run_new_articles_v2` | state-driven transition, full candidate revalidation |
| `backend/develop_verify_service.py` | `_pre_live_clarification_plan`, `verify_article_develop` | Gate A/B coordinator 결과를 checkpoint/public response로 투영 |
| `backend/verification_checkpoint_store.py` | `create`, `consume`, `update_context` | candidate bundle seal/invalidation, round budget, generation |
| `backend/app.py` | request/response models | pipeline state와 sealed IDs의 strict schema |
| `frontend/src/api.js`, `ChatApp.jsx` | verify/options/submit flow | plan-aware invalidation과 dependency-aware resume UX |

## 12. 테스트 설계

### 12.1 Canonical L2 focused tests

신규 `tests_backend/test_canonical_l2_v1.py`:

1. raw exact source -> `L2_READY`
2. 다른 sentence의 유일 exact source -> `REPAIRED_SOURCE_EXACT`
3. exact source 0개 -> `HOLD_NOT_FOUND`
4. exact source 복수 -> `HOLD_AMBIGUOUS`
5. ownership 복수 -> `HOLD_AMBIGUOUS`
6. HCX call/schema 실패 -> `L2_UNAVAILABLE`
7. quote/whitespace/morphology만 유사하고 exact substring이 없으면 HOLD
8. 숫자·단위·시점 mutation 0
9. raw SHA가 달라도 canonical semantic input이 같으면 canonical SHA 동일
10. `REPAIRED_SOURCE_EXACT`만 추가 eligible이고 HOLD는 layers 진입 0

기존 `UNRESOLVED_SPANS` producer/consumer receipt regression도 유지한다.

### 12.2 6-path retrieval tests

신규 `tests_backend/test_operational_retrieval_six_path_v1.py`:

1. 기본 path set이 정확히 6개
2. `sentence:official` job submission 0
3. `sentence:dense` 유지
4. source/corrective register가 기본 6경로와 별도 receipt
5. 한 path timeout 후 다른 path 후보 유지
6. malformed/release mismatch는 fail-closed
7. completion order가 달라도 candidate/RRF SHA 동일
8. all-path transport failure는 corrective가 아니라 retryable insufficiency
9. differential fixture에서 candidate loss 0

### 12.3 State machine/resume tests

신규 `tests_backend/test_retrieval_clarification_state_machine_v1.py`와 기존 테스트 보강:

1. indicator missing -> `DIRECT_FIELD_MISSING`, speculative planning만, Cell 0
2. indicator answer -> full retrieval physical call > 0
3. selector missing -> `SELECTOR_CLARIFICATION_POSSIBLE`
4. incomplete profile option 0, exclusion receipt 존재
5. Round 0 insufficiency -> Round 1 정확히 1회
6. Round 1 후 후보 없음 -> `CORRECTIVE_RETRIEVAL_EXHAUSTED` -> `UNVERIFIABLE_FINAL`
7. Round 1 후 selector missing -> 새 plan 생성, 이전 plan invalid
8. selector answer + valid bundle -> binding resume, retrieval physical call 0
9. bundle SHA mismatch -> binding/Cell 0
10. global unique가 아니면 Cell 0
11. `QUERY_READY`일 때만 Cell 1
12. resume 재시도에서도 corrective Round 2는 0

기존 다음 테스트를 유지·보강한다.

- `test_dependency_aware_clarification_v2.py`
- `test_binding_resume_scope_v1.py`
- `test_speculative_retrieval_v1.py`
- `test_operational_clarification_context_v1.py`
- `test_clarification_option_paging_v2.py`
- `test_resumable_clarification_annual_v1.py`
- `test_article_multi_target_v1.py`
- `test_runtime_closure_v1.py`
- `test_canonical_import_closure_v1.py`
- `test_frontend_release_contract_v1.py`

### 12.4 Differential baseline

구현 시작 직전에 SHA `238a66b`의 focused/full suite 결과를 새 receipt로 봉인한다. 과거 보고된 실패 수를 현재 baseline으로 복사하지 않는다. 비교는 node ID 단위로 수행한다.

```text
new_failures = current_failures - sealed_baseline_failures
lost_passes = sealed_baseline_passes - current_passes
```

gate는 `new_failures=0`, `lost_passes=0`이다. frozen scorer와 기존 실패 receipt는 수정하지 않는다.

## 13. E2E 설계

특정 기사 literal을 fixture나 runtime에 넣지 않고 다음 의미 범주의 합성/실제 입력을 사용한다.

1. 모든 필드가 있는 단일 수치 주장
2. 상대 시점이나 article date가 실제로 필요한 주장
3. region/sex/age/classification selector가 누락된 주장
4. indicator/item이 누락되거나 모호한 주장
5. Round 0 후보가 없고 Round 1에서 후보가 생기는 주장
6. Round 1 후에도 후보가 없는 주장
7. 한 기사에 여러 numeric target이 있는 경우
8. source exact repair가 성공/실패/모호한 경우
9. 한 retrieval path만 실패하는 경우
10. profile 일부/전체가 불완전한 경우

E2E receipt는 target마다 다음을 증명한다.

- canonical L2 status와 3 fingerprints
- query register version과 path 결과
- retrieval rounds와 membership SHA
- profile exclusions와 bundle SHA
- state transition history
- resume stage와 physical call count
- `QUERY_READY` 전 Cell 0
- 공식 셀 이후 final evidence SHA와 deterministic answer
- release/model revision/vector 1024 normalized 계약

EC2 E2E는 frontend/API가 같은 commit/image SHA일 때만 수행한다. PostgreSQL/OpenSearch/Qdrant/Redis 데이터 계층을 변경하지 않는다.

## 14. 비범위

이번 설계와 후속 구현의 비범위는 다음과 같다.

- BGE reranker 활성화 또는 모델 변경
- redis-cache 활성화
- DiffuRank
- 002 application product-state migration
- PostgreSQL schema/data write
- OpenSearch index 생성·재색인·alias/current 변경
- Qdrant collection write·재임베딩·삭제
- KOSIS canonical release 변경
- detached background worker/job store
- HCX가 verdict/cell/selector를 결정하는 기능
- 사람 입력 prefill 또는 옵션 절단
- 새로운 채점 산식 또는 threshold 변경
- 특정 기사/통계표/수치 literal 예외

## 15. Frozen assets 불변

다음은 읽기만 가능하며 수정하지 않는다.

- `data/develop/r0_blind_retrieval_gold_20260804/**`
- `data/develop/r3_full_l2_gold_20260804/**`
- `data/develop/article_hcx_holdout_20260729/evaluation/l2_gold_human_98_20260731.jsonl`
- `data/develop/r17_stratum_freeze_20260731/**`
- `r4b_unmatched_meaning_gold_*.jsonl`
- `r4b_gold_profiles_merged_*.jsonl`
- `evaluate_r3_gate_b.py`, `evaluate_six_fields.py`의 채점 산식
- `src/source_scope_classifier.py`
- 기존 `*_measurement_final_*`, `period1b/c/e` 등 동결 산출물
- historical SHA receipt
- application migration 001/002

Holdout을 신규 gate에 다시 사용하지 않는다. 이미 소진된 holdout 결과를 개발 중 튜닝 근거로 재사용하지 않는다.

## 16. 구현 순서와 역할 handoff

이 문서 승인 뒤 구현자는 다음 순서를 지킨다.

1. HEAD `238a66b`에서 baseline·7경로 receipt를 봉인한다.
2. Raw/Canonical L2 분리와 exact-only source repair를 구현한다.
3. 5상태 projection과 3 fingerprints를 stage 02/trace에 연결한다.
4. 6-path register와 per-path isolation을 구현한다.
5. 7상태 coordinator와 max-one-round Corrective Retrieval을 구현한다.
6. candidate bundle seal/invalidation과 dependency-aware resume을 연결한다.
7. Cell API self-guard와 final evidence fingerprint를 연결한다.
8. backend strict schema와 frontend plan-aware UX를 갱신한다.
9. source/deploy mirror를 동기화하고 최종 manifest를 한 번 갱신한다.
10. focused -> differential -> full suite -> frontend build -> read-only E2E 순으로 확인한다.

설계 변경이 필요한 경우 구현자가 임의로 메우지 않고 이 문서의 새 revision을 요청한다. 특히 다음은 변경 승인 대상이다.

- 5개 L2 상태와 eligible allowlist
- exact-only 정의
- 6-path register
- 7개 pipeline state
- role별 invalidation matrix
- corrective round 상한
- candidate bundle identity
- `QUERY_READY`/Cell API 권한 경계
- frozen asset/데이터 계층 비수정 경계

## 17. 완료 정의

후속 구현은 다음이 모두 증명될 때만 완료다.

1. raw HCX가 변동해도 canonical L2와 downstream evidence fingerprint가 안정적이다.
2. source는 유일한 exact 원문 span일 때만 복구되며, not-found/ambiguous는 HOLD다.
3. 숫자·단위·시점·비교 의미는 repair되지 않는다.
4. 기본 retrieval은 6경로이며 `sentence:official` physical call이 0이다.
5. 7경로 대비 candidate/QUERY_READY loss 0, false-ready 증가 0이다.
6. 한 path 실패가 독립 성공 path를 폐기하지 않으며 contract failure는 fail-closed다.
7. 누락 필드, 후보 부족, profile 불완전, selector 질문이 서로 다른 상태로 처리된다.
8. Corrective Retrieval은 target당 최대 1회다.
9. selector-only 답변은 유효한 candidate bundle에서 binding부터 재개한다.
10. candidate/profile/query register/release가 바뀌면 기존 option plan은 무효화된다.
11. 모든 후보 재검증으로 전역 유일한 `QUERY_READY`가 나오기 전 Cell API는 0이다.
12. 최종 사용자는 공식값·기간·단위·지역·표 근거가 포함된 deterministic 답변을 받는다.
13. source/deploy closure와 runtime manifest가 일치하고 frontend/API release SHA가 같다.
14. frozen assets, historical receipts, EC2 데이터 계층 bytes가 변하지 않는다.

이 상태가 목표의 최종 해결이다.
