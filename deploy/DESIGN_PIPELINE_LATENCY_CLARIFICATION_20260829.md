# 파이프라인 지연·재질의 통합 설계 v1

- 작성일: 2026-08-29
- 역할: Technical design authority (`gpt-5.6-sol`, high)
- 기준 worktree: `E:\news_verification_1\worktrees\ec2-single-sha-20260829`
- 기준 Git SHA: `3a3d2627ebb3ab06967863880e14182d7a86a4a7`
- 대상 runtime: `kosis_canonical_20260821_full_r3_13ko_views` release-bound runtime
- 문서 성격: 구현 전 상세 설계. 이 문서 작성 단계에서는 실행 코드·테스트·manifest를 수정하지 않는다.

## 0. 결론과 구현 불변조건

이 변경의 목표는 HCX 호출 자체를 미세 최적화하는 것이 아니다. 셀을 특정하는 데 필요한 정보가
없는 요청을 비싼 live 뒤까지 보내는 현재 순서를 바꾸고, 최종 답변에서 필수가 아닌 HCX를 기본
critical path에서 제거하며, 같은 요청 안에서 반복되는 profile/query/retrieval 읽기를 재사용한다.

목표 흐름은 다음과 같다.

```text
기사
  -> L1
  -> HCX L2
  -> L3~L5
  -> pre-retrieval missing-slot gate
       |-- 검색 없이 확정 가능한 누락 -> 질문 + checkpoint
       |-- indicator 등 고영향 누락 -> bounded speculative retrieval -> 질문 + checkpoint
       `-- 전단 누락 없음
             -> release preflight
             -> retrieval
             -> profile materialization
             -> candidate-specific Late Binding
             -> post-binding clarification gate
                  |-- 사용자로 보완 가능한 공통 slot -> profile 기반 선택지 + checkpoint
                  |-- profile 불완전/비보완 failure -> UNVERIFIABLE
                  `-- globally unique QUERY_READY
                        -> KOSIS Cell API
                        -> deterministic comparator
                        -> deterministic sealed renderer
                        -> 즉시 응답
```

다음 불변조건은 구현 편의를 이유로 완화하지 않는다.

1. 검색 결과는 표 후보 membership만 정한다. ITEM/DIMENSION hit는 selector evidence가 아니다.
2. 사용자 답변은 의미 제약(`SemanticConstraint`)이며 특정 표의 `objL*`를 직접 확정하지 않는다.
3. 사용자 답변을 모든 후보에 다시 투영한 뒤 Strict Validator가 전역 유일성을 증명해야
   `QUERY_READY`가 된다.
4. `QUERY_READY` 이전 Cell API 호출은 항상 0이다.
5. HCX answer verbalizer는 verdict, 공식값, 표, selector, limitation을 바꿀 권한이 없다.
6. PostgreSQL/OpenSearch/Qdrant는 읽기 전용이다. redis-session은 인증 세션 외 용도로 쓰지 않는다.
7. 별도 background store가 정해지기 전에는 요청 수명 밖에서 계속 실행되는 job을 만들지 않는다.
8. 동결 gold, 채점 산식, historical receipt의 bytes를 변경하지 않는다.

## 1. 현재 코드에서 확인한 병목과 삽입 경계

### 1.1 현재 실행 순서

`backend/develop_verify_service.py::verify_article_develop()`은 최초 요청에서
`l1 -> l2 -> layers -> live`를 모두 실행한 뒤 `_pending_article_date_from_live()`,
`_pending_clarification()`, `_pending_article_date_from_routed()` 순서로 질문을 찾는다. 따라서
L3~L5 산출물만으로 article date 누락을 알 수 있어도 첫 live를 이미 지불한다.

resume은 현재도 L1/L2를 재실행하지 않는다. checkpoint의 `resume_from_stage`가 `layers`이면
`layers -> live`, `live`이면 `live`를 수행한다. 이 경계는 유지하되, 아래 invalidation matrix에
따라 더 정확히 고른다.

### 1.2 현재 live의 동기 HCX 호출

`deploy/pipeline_runtime/src/news_verification/runtime/run_pipeline_operational_v2.py`의
`run_new_articles_v2()` 내부 `answer_for()`는 target마다 `generate_guarded_answer()`를 부른다.
QUERY_READY뿐 아니라 L2 불가, L5 gate, retrieval/reranker 실패, no-candidate, projection hold에도
동일하다. `run_live_from_files()`는 `deterministic_answer_only=False`인 기본 경로에서
`CountingAnswerer(Hcx007AnswerClient(...))`를 생성하므로 target별 HCX-007 호출이 순차 critical
path가 된다.

이미 `operational_answer_v2.py::deterministic_fallback()`과 `_deterministic_draft()`가 sealed
packet validator를 통과하는 결정론 답변을 만들 수 있다. 신규 renderer를 따로 발명하지 않고 이
경계를 기본 권위로 승격한다.

### 1.3 현재 profile 중복 조회

release-bound `CanonicalMetadataProfileProvider.prefetch()`는 `self(key)`를 호출해 결과를 반환하지만
보존하지 않는다. 이어지는 `resolve_top50*()`가 같은 provider를 다시 호출한다. provider의 각
`__call__()`은 PostgreSQL read-only 연결과 release attestation/profile materialization을 다시 한다.
`resolve_top50*()`가 pinned raw/projection profile을 이미 결과에 보존하는 점은 유지한다.

### 1.4 현재 clarification option의 한계

`src/develop/failure_recovery_shadow_v1.py::_question_for_multiple()`은 성공한 assignment의
`profile_label`만 모은다. 모든 후보가 같은 누락 axis에서 abstain한 경우에는 assignment가 없으므로
실제 profile inventory 선택지를 만들 수 없다. options도 단순 문자열 배열이라 어떤 후보와 어떤
axis에 적용되는지 checkpoint에 결박되지 않는다.

## 2. 변경 파일과 함수별 책임

신규 runtime Python 파일을 추가하지 않는다. 현재 release verifier가 고정한 74-file closure를
유지하고, 아래 기존 파일의 size/SHA만 manifest에 갱신한다. 구현자가 임의로 다른 경로에 유사
구현을 추가하지 않는다.

### 2.1 Backend/API

| 파일 | 함수/구조 | 정확한 변경 |
|---|---|---|
| `backend/app.py` | `DevelopVerifyRequest` | `clarification_answers`를 `ClarificationAnswerRequest` typed model 목록으로 변경한다. `option_id`를 optional로 받되 OPTIONS 질문에서는 필수 검증한다. |
| `backend/app.py` | 신규 `ClarificationOptionsRequest` | `resume_token`, `question_id`, optional `query`, optional `cursor`, `limit(1..50, default 20)`만 허용한다. |
| `backend/app.py` | 신규 `POST /api/v1/verify/develop/options` | active checkpoint의 봉인된 option bundle만 조회한다. pipeline·검색·HCX·Cell API를 호출하지 않는다. CSRF를 그대로 적용한다. |
| `backend/develop_verify_service.py` | `_validate_clarification_answers()` | `question_id == clarify-{role}` 가정을 제거한다. checkpoint가 가진 pending question ID/role/input mode/options SHA와 대조한다. 직접 입력 허용 여부와 option token을 검증한다. |
| 위 파일 | 신규 `_pre_live_clarification_plan()` | `03_routed.jsonl`과 article/date/context를 읽어 검색 전 확정 가능한 missing slot을 판정한다. `layers` 직후 호출한다. |
| 위 파일 | `_clarification_response()` | 아래 clarification-plan-v2 public schema로 투영한다. options는 object 배열이며 initial page만 반환한다. |
| 위 파일 | `_pending_clarification()` | live ledger의 legacy 질문을 직접 반환하지 않고 `clarification_plan` receipt를 읽는다. legacy 형식은 feature migration 동안 fail-closed `CLARIFICATION_PLAN_INVALID`로 처리한다. |
| 위 파일 | `verify_article_develop()` | stage별 monotonic timing을 기록한다. `layers` 직후 전단 gate를 호출한다. 질문이면 live를 실행하지 않고 checkpoint를 만든다. resume 시 invalidation matrix의 `resume_from_stage`와 reusable artifact SHA를 검증한다. |
| 위 파일 | `_target_receipts()` | target timing, logical/physical cache call, clarification receipt를 public allowlist로 투영한다. secret/path/vector/raw profile은 제외한다. |
| 위 파일 | 신규 `get_clarification_options()` | checkpoint store의 read-only page 함수 호출 및 public page 응답만 담당한다. |
| `backend/verification_checkpoint_store.py` | `create()` | v2 metadata와 sealed artifacts를 기록한다. `clarification_plan.json`, `option_bundle.json`, optional `speculative_bundle.json`을 hash/size와 함께 결박한다. |
| 위 파일 | `consume()` | pending question, answer, candidate/profile/speculative SHA, runtime/config SHA를 검증한 뒤에만 CONSUMED로 전환한다. 검증 실패 때 active checkpoint를 훼손하지 않는다. |
| 위 파일 | 신규 `read_option_page()` | token을 소비하지 않고 option bundle 전체에서 pagination/search를 수행한다. 반환은 label/설명/opaque option ID뿐이다. |
| 위 파일 | `update_context()` | `clarification-context-v2`, `semantic_constraints`, `changed_roles`, `invalidated_stages`, `resume_generation`을 원자 기록한다. |
| `.env.example`, `deploy/runtime.env.example` | 환경변수 문서 | 5절과 8절에 정한 feature gate/limit의 안전한 기본값을 추가한다. secret은 추가하지 않는다. |

### 2.2 Runtime orchestration/adapters

| 파일 | 함수/구조 | 정확한 변경 |
|---|---|---|
| `deploy/pipeline_runtime/src/news_verification/runtime/run_pipeline_operational_v2.py` | `_CLARIFICATION_ROLES`, `_load_articles_for_clarification()`, `_merge_user_clarifications()` | v2 semantic constraint를 읽고 role별 invalidation을 확인한다. opaque question ID를 허용하되 answer SHA와 checkpoint-provided question binding을 검증한다. |
| 위 파일 | `run_live_from_files()` | request-scoped execution context를 하나 만들고 preflight, adapters, cache, timing을 그 context에 결박한다. default answer mode는 deterministic이다. |
| 위 파일 | `run_new_articles_v2()` | `answer_for()`를 renderer policy로 바꾸고 모든 answer 경로에 deterministic default를 적용한다. target loop의 retrieval/binding/cell/answer span을 기록한다. |
| 위 파일 | target loop의 `resolve_top50*()` 직후 | Cell API 전에 `build_post_binding_clarification_plan()`을 호출한다. plan이 있으면 answer/Cell API 없이 ledger를 `CLARIFICATION_REQUIRED`로 종료한다. |
| 위 파일 | `retrieve_parallel()` 호출부 | request retrieval cache를 통한다. speculative 결과는 selector-only resume에서만 아래 identity 조건을 모두 만족하면 재사용한다. |
| `deploy/pipeline_runtime/src/news_verification/runtime/operational_live_adapters_v2.py` | `CountingAdapter`, `CountingEncoder`, `CountingAnswerer`, `FailClosedCellFetcher` | thread-safe monotonic duration, logical calls, physical calls를 분리한다. public audit에는 duration aggregate만 둔다. |
| 위 파일 | 신규 `RequestScopedProfileProvider` | physical provider를 감싸 positive/negative profile cache를 제공한다. `prefetch()`와 resolver가 같은 dict를 사용한다. |
| 위 파일 | 신규 `RequestScopedEncoder` | 동일 request에서 동일 normalized query/model revision의 vector를 1회만 생성한다. vector는 receipt에 기록하지 않는다. |
| 위 파일 | 신규 `RequestScopedRetrievalCache` | query register/release/channel/top-k identity가 같은 retrieval 결과와 audit를 immutable copy로 재사용한다. |
| `deploy/pipeline_runtime/src/news_verification/runtime/release_bound_live_adapters_v1.py` | `ReleaseBoundRuntime.channels()` | request-scoped encoder를 dense channel에 주입한다. 기존 release/model/1024/normalized preflight는 변경하지 않는다. |
| 위 파일 | `CanonicalMetadataProfileProvider.prefetch()` | 외부 wrapper가 없으면 기존 동작을 유지한다. caching authority는 provider 전역이 아니라 request wrapper에만 둔다. |
| `backend/query_encoder.py` | `BGEQueryEncoderClient` | 이미 사용하는 `normalize_encoder_query()`를 request cache key 함수로 노출한다. encode payload와 vector contract는 변경하지 않는다. |
| `deploy/pipeline_runtime/src/news_verification/runtime/operational_answer_v2.py` | 신규 `render_answer()` 또는 `generate_guarded_answer()` mode 인자 | `DETERMINISTIC_ONLY`면 HCX 객체를 만지지 않고 `deterministic_fallback()`을 호출한다. `HCX_SHADOW_SYNC`는 sealed packet으로만 호출하고 결과를 shadow receipt에 두며 사용자 answer는 deterministic 결과를 유지한다. |
| `deploy/pipeline_runtime/src/news_verification/runtime/r4c1_projection_v2.py` | `CandidateProjection` | 기존 필드에 `slot_diagnostics`를 추가한다. 기존 assignments/abstained/hold 이유의 의미는 변경하지 않는다. |
| 위 파일 | `project_candidate_v2()`, `project_candidate_monthly_v2j()` | item/period/unit/dimension별 missing/ambiguous/conflict/profile-incomplete diagnostics와 option inventory provenance를 생성한다. |
| 위 파일 | `validate_target_v2()`, `validate_target_monthly_v2h()` | 기존 QUERY_READY 판단은 그대로 유지하고, 전체 candidate 진단의 canonical SHA만 audit에 추가한다. clarification planner가 validator를 우회하지 못하게 한다. |
| `deploy/pipeline_runtime/src/develop/failure_recovery_shadow_v1.py` | `_question_for_missing()`, `_question_for_multiple()`, `plan_failure_recovery()` | 문자열 options 생성기를 폐기하고 pre/post binding clarification planner와 option bundle builder를 구현한다. corrective retrieval 1회 계약은 별도 유지한다. |
| `src/develop/failure_recovery_shadow_v1.py` | 같은 함수 | packaged shadow 구현과 byte-identical하게 유지한다. 두 파일 drift는 테스트에서 실패시킨다. |
| `deploy/pipeline_runtime/src/develop/run_article_body_pipeline_trace_v1.py` | `_apply_clarification_context()` | clarification-context-v2를 읽고 article date provenance와 semantic constraints를 보존한다. L1/L2 manifest payload는 변경하지 않는다. |
| `deploy/pipeline_runtime/src/news_verification/runtime/r4c1_claim_core_v2.py` | `_attach_user_clarification()`, `build_claim_core_v2()` | semantic constraint provenance를 atom에 붙인다. table/axis/value ID를 Claim Core에 넣지 않는다. |
| `deploy/pipeline_runtime/manifest.json` | 74개 record | 경로 수는 74로 유지하고 변경 파일의 size/SHA와 source bundle manifest SHA만 갱신한다. historical release receipt는 수정하지 않는다. |

### 2.3 Frontend

| 파일 | 함수/구조 | 정확한 변경 |
|---|---|---|
| `frontend/src/api.js` | `verifyArticleDevelop()` | structured clarification answer의 `option_id`를 보존한다. |
| 위 파일 | 신규 `fetchClarificationOptions()` | `/v1/verify/develop/options`에서 page/search 결과를 가져온다. credentials include/CSRF/release check는 기존 `apiFetch()`를 사용한다. |
| `frontend/src/ChatApp.jsx` | `requestClarification()` | question/option page metadata/candidate scope SHA를 pending state에 보존한다. |
| 위 파일 | clarification UI | DATE/FREE_TEXT/OPTIONS/SEARCHABLE_OPTIONS를 분리한다. option prefill/자동 선택은 금지한다. 검색·다음 페이지로 모든 값에 도달 가능하게 한다. |
| 위 파일 | `handleSend()` | OPTIONS는 선택된 `option_id + displayed value`를 보낸다. 직접 입력 허용 질문만 free text를 허용한다. 취소 시 서버 job은 없으므로 local pending state만 제거한다. |

## 3. 외부 API·receipt·checkpoint 스키마

### 3.1 역할 enum

v2의 role은 아래 closed enum이다.

```text
article_date, period,
indicator, item, unit, source, population,
region, sex, age, classification,
measurement_basis
```

`source`는 KOSIS 제공기관/조사 맥락 제약이며 기사 출처 region과 혼동하지 않는다.
`classification`은 축 의미를 region/sex/age/population으로 안전하게 분류하지 못한 일반 분류축이다.
`measurement_basis`는 증가폭이 절대차인지 변화율인지 구분하는 연산 의미이고 selector ID가 아니다.

### 3.2 요청 answer

```json
{
  "question_id": "cq-opaque-identifier",
  "role": "region",
  "value": "전국",
  "option_id": "co-opaque-identifier"
}
```

- `question_id`와 `option_id`는 checkpoint가 발급한 opaque 값이다.
- OPTIONS/SEARCHABLE_OPTIONS는 `option_id` 필수다.
- DATE/FREE_TEXT는 `option_id`가 없어야 한다.
- 표시 문자열 `value`도 함께 보내며 서버의 option bundle label과 byte-normalized equality를 확인한다.
- 최대 3회 계약은 유지한다. 같은 role의 충돌값은 `CLARIFICATION_CONFLICT`다.

### 3.3 `needs_user_input` 응답

```json
{
  "type": "needs_user_input",
  "status": "awaiting_clarification",
  "reason": "CLARIFICATION_REQUIRED",
  "resume_token": "opaque",
  "resume_from_stage": "layers|retrieval|binding",
  "question": {
    "id": "cq-opaque-identifier",
    "role": "region",
    "target_ids": ["public-target-id"],
    "prompt": "어느 지역 기준인지 선택해 주세요.",
    "input_mode": "SEARCHABLE_OPTIONS",
    "allow_direct_input": true,
    "options": [
      {
        "id": "co-opaque-identifier",
        "label": "전국",
        "description": "현재 후보 통계표 3개에 적용 가능",
        "applicable_candidate_count": 3
      }
    ],
    "page": {
      "total": 484,
      "limit": 20,
      "next_cursor": "opaque-or-null",
      "search_supported": true,
      "options_complete": false
    }
  },
  "clarification_receipt": {
    "contract_version": "clarification-plan-v2",
    "plan_sha256": "64hex",
    "candidate_membership_sha256": "64hex-or-null",
    "profile_bundle_sha256": "64hex-or-null",
    "speculative": false,
    "cell_api_calls": 0,
    "hcx_answer_calls": 0
  },
  "timing": {}
}
```

`target_ids`는 기존 public target ID만 쓴다. table key/axis ID/value ID는 question/options에 노출하지
않는다. option description은 적용 후보 수와 사람에게 의미 있는 축 이름만 포함한다.

### 3.4 Option page API

요청:

```json
{
  "resume_token": "opaque",
  "question_id": "cq-opaque-identifier",
  "query": "서울",
  "cursor": null,
  "limit": 20
}
```

응답은 `question_id`, `options`, `page`만 반환한다. 다음 규칙을 지킨다.

- query가 없으면 canonical label sort 전체를 pagination한다.
- query가 있으면 NFKC+casefold+whitespace normalization substring 검색을 하되 결과 전체에
  pagination을 적용한다.
- `total`은 필터 후 전체 개수다.
- cursor는 question ID, query SHA, offset, option-bundle SHA에 결박한 opaque 값이다.
- token/question/bundle mismatch는 409 bounded code로 fail-closed 한다.
- 이 endpoint는 token을 consume하지 않는다.

### 3.5 내부 option bundle

`option_bundle.json`은 checkpoint 내부 전용이다.

```json
{
  "contract_version": "clarification-option-bundle-v2",
  "question_id": "cq-opaque-identifier",
  "role": "region",
  "candidate_membership_sha256": "64hex",
  "profile_bundle_sha256": "64hex",
  "options": [
    {
      "option_id": "co-opaque-identifier",
      "semantic_value": "전국",
      "normalized_value": "전국",
      "display_label": "전국",
      "applicability": [
        {
          "table_key": "101:...",
          "profile_sha256": "64hex",
          "axis_id": "hidden",
          "value_id": "hidden"
        }
      ]
    }
  ]
}
```

내부 mapping은 사용자 선택을 후보별 selector proposal로 번역하기 위한 것이며 Strict Validator의
결과가 아니다. 같은 label이라도 semantic role/axis meaning이 다르면 option을 합치지 않는다.

### 3.6 Checkpoint v2

`checkpoint.json`에 다음을 추가한다.

```json
{
  "contract_version": "verification-checkpoint-v2",
  "resume_generation": 1,
  "pending_question_id": "cq-opaque-identifier",
  "pending_role": "region",
  "resume_from_stage": "binding",
  "changed_roles": [],
  "invalidated_stages": [],
  "reusable_artifacts": ["l1", "l2", "layers", "retrieval", "profiles"],
  "clarification_plan_sha256": "64hex",
  "option_bundle_sha256": "64hex-or-null",
  "speculative_bundle_sha256": "64hex-or-null",
  "candidate_membership_sha256": "64hex-or-null",
  "profile_bundle_sha256": "64hex-or-null"
}
```

`clarification_context.json`은 v2에서 다음을 갖는다.

```json
{
  "contract_version": "clarification-context-v2",
  "article_id": "...",
  "article_body_sha256": "64hex",
  "clarification_answers": [],
  "semantic_constraints": [
    {
      "role": "region",
      "value": "전국",
      "source": "USER_CLARIFICATION",
      "question_id": "cq-opaque-identifier",
      "answer_sha256": "64hex",
      "option_bundle_sha256": "64hex"
    }
  ],
  "changed_roles": ["region"],
  "invalidated_stages": ["binding", "cell", "answer"]
}
```

article 원문과 L1/L2 bytes는 계속 immutable이다. context v2를 01/02 payload로 가장하거나 stage
manifest의 article SHA를 바꾸지 않는다.

## 4. Missing-slot 판정과 invalidation matrix

### 4.1 두 개의 판정 gate

#### Gate A: pre-retrieval

`03_routed.jsonl`만으로 아래 조건을 결정한다.

| role | missing 판정 | 행동 |
|---|---|---|
| `article_date` | relative period가 있고 absolute period 생성에 article date가 필요하며 유효 date/provenance가 없음 | 검색 없이 즉시 DATE 질문 |
| `period` | relative-date 문제가 아닌데 period raw/absolute/frequency가 비었거나 지원 문법으로 정규화되지 않음 | 검색 없이 FREE_TEXT 질문 |
| `indicator` | routed indicator가 비었거나 L2/L3 provenance가 UNKNOWN/AMBIGUOUS | bounded speculative retrieval 후 indicator 선택/직접 입력 질문 |
| `item` | indicator는 있으나 explicit item family가 필요한 measurement에서 item/indicator query가 공백 | bounded speculative retrieval 후 item 질문 |
| `measurement_basis` | `큰 폭` 등에서 rate와 absolute difference를 문장으로 유일하게 정할 수 없음 | 검색 없이 선택 질문(증가율/증가량) |
| 나머지 | profile을 보기 전 실제 축 존재 여부를 알 수 없음 | Gate A에서 질문하지 않고 retrieval로 진행 |

Gate A는 단순히 article date가 없다는 이유로 질문하지 않는다. 절대 period가 이미 있거나 period가
셀 선택에 필요하지 않은 target에는 date 질문이 없다.

#### Gate B: post-binding, pre-cell

각 `CandidateProjection.slot_diagnostics`를 전체 후보 범위에서 평가한다.

`CLARIFICATION_POSSIBLE`은 다음 조건을 모두 만족할 때만 낸다.

1. candidate membership이 비어 있지 않다.
2. profile이 `METADATA_PROFILE_INCOMPLETE`가 아니다.
3. QUERY_READY가 아니다.
4. 하나 이상의 role이 모든 생존 후보에서 `MISSING` 또는 `AMBIGUOUS`다.
5. 그 role에 대해 profile inventory 기반 option 또는 direct input 경로가 있다.
6. candidate/profile membership SHA를 계산할 수 있다.

후보 profile 자체가 불완전하거나 semantic axis role을 안전하게 분류할 수 없고 direct input도
candidate별로 검증할 수 없으면 질문하지 않고 `UNVERIFIABLE/METADATA_PROFILE_INCOMPLETE`로
끝낸다.

### 4.2 `SlotDiagnostic` 내부 계약

```json
{
  "role": "region",
  "status": "RESOLVED|MISSING|AMBIGUOUS|CONFLICT|UNSUPPORTED|PROFILE_INCOMPLETE",
  "table_key": "hidden internally",
  "profile_sha256": "64hex",
  "axis_semantic_role": "region",
  "axis_inventory_path": "dimensions[0]",
  "option_inventory": [
    {"label": "전국", "axis_id": "hidden", "value_id": "hidden"}
  ],
  "reason": "REGION_UNBOUND"
}
```

axis semantic role은 closed deterministic mapping으로만 정한다.

- region: axis label에 `행정구역`, `지역`, `시도`
- sex: `성별`, `성`
- age: `연령`, `연령별`, `나이`
- population: L4 population atom 또는 axis label의 대상/계층 의미가 기존 bounded lexical rule과 일치
- 그 밖: `classification`

두 role에 동시에 맞거나 어느 role인지 불명확하면 `classification`으로 내리고 자동 합치지 않는다.
이 mapping을 바꾸는 것은 설계 변경이며 implementer가 새 동의어를 임의 추가하지 않는다.

### 4.3 질문 선택 순서

여러 누락이 동시에 있으면 다음 deterministic tuple을 최소화한다.

```text
(gate_priority, retrieval_impact_priority, -candidate_partition_gain,
 role_priority, target_order)
```

- Gate A가 Gate B보다 먼저다.
- retrieval impact: `indicator, item, unit, source, population`이 후보 membership을 바꿀 수 있는
  고영향 role이다.
- period impact: `article_date, period`.
- selector-only: `region, sex, age, classification`.
- operation-only: `measurement_basis`.
- 같은 등급은 `indicator -> item -> unit -> source -> population -> article_date -> period ->
  region -> sex -> age -> classification -> measurement_basis` 순서다.
- `candidate_partition_gain`은 option별 candidate applicability partition 수로만 계산하며 search score나
  Cell 값은 사용하지 않는다.

### 4.4 Field invalidation matrix

| changed role | 재사용 가능 | 반드시 무효화 | resume_from_stage |
|---|---|---|---|
| `article_date` | L1, L2 | layers, query register, retrieval compatibility, binding, cell, answer | `layers` |
| `period` | L1, L2 | layers, query register의 period path, retrieval cache, binding, cell, answer | `layers` |
| `indicator` | L1, L2 | layers의 해당 target normalization부터, 모든 retrieval channel/ranking, profile scope, binding, cell, answer | `layers` |
| `item` | L1, L2, 해당 target의 비-item context | query register, 모든 retrieval channel/ranking, profile scope, binding, cell, answer | `retrieval` |
| `unit` | L1, L2 | query register, retrieval, item/unit compatibility binding, cell comparison, answer | `retrieval` |
| `source` | L1, L2 | source query register, official/BM25 retrieval 및 candidate union 전체, binding, cell, answer | `retrieval` |
| `population` | L1, L2 | query register, retrieval, profile scope, binding, cell, answer | `retrieval` |
| `region` | L1, L2, layers, candidate membership, pinned profiles | binding, validator, cell, answer | `binding` |
| `sex` | L1, L2, layers, candidate membership, pinned profiles | binding, validator, cell, answer | `binding` |
| `age` | L1, L2, layers, candidate membership, pinned profiles | binding, validator, cell, answer | `binding` |
| `classification` | L1, L2, layers, candidate membership, pinned profiles | binding, validator, cell, answer | `binding` |
| `measurement_basis` | L1, L2, candidate membership/profile only if same indicator family | operation projection, period-only evidence, comparator, answer | `binding` |

`indicator/item/unit/source/population` 보완 뒤 speculative candidate를 final candidate로 합치지 않는다.
새 query register로 retrieval을 전부 다시 실행한다. region/sex/age/classification은 checkpoint에 봉인된
candidate membership과 profile SHA가 모두 일치할 때만 binding부터 재개한다. 하나라도 다르면
`RESUME_ARTIFACT_STALE`로 fail-closed하며 새 전체 제출로 조용히 fallback하지 않는다.

## 5. HCX answer gate와 timing spans

### 5.1 Answer mode

환경변수는 다음 하나를 정본으로 사용한다.

```text
PIPELINE_ANSWER_RENDER_MODE=DETERMINISTIC_ONLY|HCX_SHADOW_SYNC
```

- default/production: `DETERMINISTIC_ONLY`
- `HCX_SHADOW_SYNC`: 명시적 성능·문장 품질 실험 전용. HCX 결과는 `answer_shadow` receipt에만
  기록하고 사용자 답변은 여전히 deterministic renderer 결과다.
- 그 밖의 값: startup/preflight에서 `ANSWER_RENDER_MODE_INVALID`로 차단한다.
- 기존 `deterministic_answer_only` 함수 인자는 CLI/technical canary 호환 shim으로만 남기고,
  `True`는 `DETERMINISTIC_ONLY`로 번역한다. 서로 충돌하면 fail-closed 한다.
- background store가 없으므로 이번 범위에서 비동기 HCX answer job을 만들지 않는다.

HCX shadow timeout은 아래로 제한한다.

```text
PIPELINE_HCX_ANSWER_TIMEOUT_SECONDS=3.0   # allowed 0.5..10.0, shadow mode only
```

timeout/validator rejection은 deterministic 사용자 answer를 바꾸지 않는다. HCX initial+repair 두 번
호출하는 기존 동작은 shadow에서도 제거한다. shadow는 정확히 1회 호출 후 acceptance 결과만 기록한다.

### 5.2 Timing contract

모든 duration은 `time.monotonic_ns()` 차이를 정수 millisecond로 반올림한 값이다. wall clock timestamp,
hostname, URL, path, token, query text, vector, DSN은 receipt에 넣지 않는다.

Top-level public timing:

```json
{
  "contract_version": "pipeline-timing-v1",
  "total_wall_ms": 12345,
  "stages": {
    "l1": {"wall_ms": 10, "calls": 1},
    "l2_hcx": {"wall_ms": 6430, "calls": 1},
    "layers": {"wall_ms": 10, "calls": 1},
    "live": {"wall_ms": 5200, "calls": 1}
  },
  "resume": {"used": false, "from_stage": null}
}
```

Target timing:

```json
{
  "preflight": {"wall_ms": 313, "physical_calls": 1},
  "retrieval_wall": {"wall_ms": 900, "logical_calls": 7, "physical_calls": 7},
  "official_search": {"sum_ms": 715, "physical_calls": 1},
  "bm25": {"sum_ms": 5, "physical_calls": 2},
  "query_encoder": {"sum_ms": 32, "logical_calls": 3, "physical_calls": 1, "cache_hits": 2},
  "qdrant_dense": {"sum_ms": 135, "physical_calls": 3},
  "metadata_binding": {"wall_ms": 40, "logical_lookups": 100, "physical_lookups": 50, "cache_hits": 50},
  "cell_api": {"sum_ms": 597, "physical_calls": 1},
  "answer_deterministic": {"sum_ms": 1, "calls": 1},
  "answer_hcx_shadow": {"sum_ms": 0, "calls": 0}
}
```

병렬 channel의 `sum_ms`를 합쳐 critical path라고 표시하지 않는다. `retrieval_wall.wall_ms`가 실제
parallel 구간 wall time이고 channel `sum_ms`는 attribution이다. timing recorder 실패나 음수 duration,
parent보다 불가능한 wall 값은 `TIMING_RECEIPT_INVALID`로 테스트에서 실패시킨다.

### 5.3 질문 응답 timing invariant

질문을 반환하는 실행은 반드시 다음을 증명한다.

```text
cell_api.physical_calls == 0
answer_hcx_shadow.calls == 0
answer_deterministic.calls == 0
```

단, speculative retrieval의 official/BM25/dense/profile 호출은 별도
`speculative_retrieval` span에 기록한다.

## 6. Request-scoped cache

캐시 수명은 `run_live_from_files()` 한 호출이다. module global, process global, Redis, disk cache를
추가하지 않는다. 모든 value는 요청 종료와 함께 해제한다.

### 6.1 Profile cache

키:

```text
(release_id, table_key)
```

규칙:

- 첫 physical provider 결과를 immutable deep copy 또는 `None`으로 저장한다.
- provider 예외는 cache하지 않고 bounded failure로 반환한다.
- `None`은 negative hit로 request 동안만 cache한다.
- `prefetch()`는 같은 dict를 채우며 resolver의 `__call__()`은 그 dict를 읽는다.
- cached profile을 반환할 때 `release_id`, `table_key`, `profile_sha256`를 재검증한다.
- public metrics는 logical/physical/hit/negative_hit를 분리한다.

### 6.2 Query-vector cache

키:

```text
(model_id, model_revision, vector_size, sha256(normalize_encoder_query(text)))
```

규칙:

- normalization은 `backend/query_encoder.py::normalize_encoder_query()`와 반드시 동일하다.
- 성공하고 1024차원·normalized receipt를 통과한 vector만 tuple로 저장한다.
- 실패/timeout/invalid vector는 cache하지 않는다.
- receipt에는 query SHA와 hit/miss만 남기고 vector와 query 원문을 남기지 않는다.

### 6.3 Retrieval cache

키 payload:

```json
{
  "release_binding_sha256": "...",
  "query_register_sha256": "...",
  "channels": ["bm25", "dense", "official"],
  "path_top_k": 20,
  "union_top_k": 100,
  "field_contract_sha256": "..."
}
```

- cached result는 `RrfCandidate` tuple과 bounded retrieval audit의 immutable copy다.
- hit target은 `logical_calls`를 증가시키되 channel physical counters를 증가시키지 않는다.
- dense boundary audit는 최초 physical receipt SHA를 `reused_from_receipt_sha256`로 참조한다.
- partial channel 성공/실패는 cache하지 않는다. 전체 `retrieve_parallel()` 성공만 cache한다.
- corrective round와 speculative round는 query-register SHA가 다르므로 서로 충돌하지 않는다.

### 6.4 Resume 간 재사용

request cache 자체는 HTTP 요청을 넘지 않는다. selector-only 질문에 한해 checkpoint에 봉인한
speculative/candidate/profile bundle을 resume input으로 읽을 수 있다. 이것은 cache가 아니라
immutable resumable artifact다. 4.4의 SHA 조건을 통과하지 못하면 사용하지 않는다.

## 7. Bounded synchronous speculative retrieval

별도 durable background job store가 없으므로 이번 범위에서 “백그라운드”는 실제 detached job이
아니다. 질문 응답을 만들기 전에 같은 요청 안에서 제한된 read-only 선행 작업을 수행한다. 요청이
끝난 뒤 실행을 지속하지 않으며 취소·다중 worker·재배포 일관성 문제를 만들지 않는다.

### 7.1 허용 조건

아래 모두를 만족할 때만 수행한다.

1. Gate A가 `indicator` 또는 `item` missing/ambiguous를 냈다.
2. sentence/source context는 존재한다.
3. 사용자 주장값 숫자를 제거한 query를 1개 이상 만들 수 있다.
4. 같은 request에서 speculative 실행을 아직 하지 않았다.

### 7.2 고정 예산

```text
PIPELINE_SPECULATIVE_RETRIEVAL_ENABLED=true
PIPELINE_SPECULATIVE_DEADLINE_MS=2500      # allowed 500..3000
PIPELINE_SPECULATIVE_MAX_TARGETS=1         # fixed 1 in v1
PIPELINE_SPECULATIVE_MAX_QUERIES=2         # sentence, source only
PIPELINE_SPECULATIVE_PATH_TOP_K=10
PIPELINE_SPECULATIVE_UNION_TOP_K=30
PIPELINE_SPECULATIVE_PROFILE_LIMIT=30
PIPELINE_SPECULATIVE_RETRY_LIMIT=0
```

대상은 4.3 질문 선택 tuple의 첫 target 하나다. `indicator`가 없으므로 indicator/item query를 모델로
생성하지 않는다. 숫자를 제거한 sentence query와 이미 provenance가 있는 source/report query만
사용한다. allowed channel은 existing official/BM25/dense이며 새 검색 서비스를 추가하지 않는다.

### 7.3 허용 산출과 authority

허용:

- query register 후보 생성
- BM25/dense/official table candidate search
- 최대 30개 canonical profile materialization
- profile ITEM/axis labels에서 clarification option proposal 생성
- `speculative_bundle.json` 봉인

금지:

- KOSIS Cell API
- comparator/verdict
- HCX answer
- HCX로 indicator 추정
- corrective retrieval round
- candidate를 final membership으로 승격
- metadata/index/collection write

사용자가 indicator/item/unit/source/population을 보완하면 speculative membership은 폐기하고 full
retrieval을 새 query register로 다시 수행한다. speculative option은 사용자에게 “후보 통계표에서
찾은 가능한 지표”라고 표시하며 자동 선택하지 않는다. speculative 실패/timeout이면 질문을
FREE_TEXT로 반환하고 전체 요청을 5xx로 만들지 않는다.

### 7.4 진짜 비동기화의 보류 경계

향후 detached background job은 별도 승인된 store가 생긴 뒤에만 구현한다. 필요한 최소 계약은
대화별 active job 1개, idempotency key, 5~10분 TTL, cancellation, release invalidation, worker 간
일관성, redis-session과 물리·논리적 분리다. 그 전에는 `asyncio.create_task`, daemon thread,
FastAPI BackgroundTasks로 요청 수명 밖 호출을 남기지 않는다.

## 8. 구현 feature gates와 안전 기본값

```text
PIPELINE_EARLY_CLARIFICATION_ENABLED=true
PIPELINE_CLARIFICATION_OPTIONS_ENABLED=true
PIPELINE_REQUEST_CACHE_ENABLED=true
PIPELINE_SPECULATIVE_RETRIEVAL_ENABLED=true
PIPELINE_TIMING_RECEIPT_ENABLED=true
PIPELINE_ANSWER_RENDER_MODE=DETERMINISTIC_ONLY
PIPELINE_HCX_ANSWER_TIMEOUT_SECONDS=3.0
PIPELINE_SPECULATIVE_DEADLINE_MS=2500
```

boolean은 정확히 `true|false`, enum/숫자는 startup에서 범위를 검사한다. invalid value는 silent default가
아니라 preflight failure다. 성능 회귀 rollback은 API image/feature gate로만 수행하고 데이터 계층을
변경하지 않는다.

## 9. 테스트와 승인 게이트

### 9.1 구현 전 baseline 봉인

현재 SHA에서 먼저 다음을 기록한다.

- focused tests의 passed/failed/skipped와 failed node ID 전체
- 현재 worktree에 존재하는 `tests_backend` 전체 결과
- 저장소 전체 suite를 실행할 수 있는 정본 checkout에서는 AGENTS 명령의 전체 결과
- 기존 실패는 node ID로 봉인한다. 신규 실패 0, 기존 통과 감소 0 differential gate를 적용한다.

과거 보고 숫자를 이번 구현 baseline으로 그대로 복사하지 않고 현재 SHA에서 재측정한다.

### 9.2 신규 회귀 테스트 파일

| 파일 | 필수 시나리오 |
|---|---|
| `tests_backend/test_pipeline_timing_receipts_v1.py` | monotonic stage/target span, parallel wall 대 sum 구분, secret/query/vector 미노출, 질문 시 cell/answer 0 |
| `tests_backend/test_early_clarification_v2.py` | 상대 period+date 없음은 layers 후 질문/live 0, absolute period는 date를 묻지 않음, indicator missing speculative budget |
| `tests_backend/test_dependency_aware_clarification_v2.py` | 공통 missing region option 생성, 후보별 selector mapping, 선택 후 전체 후보 revalidation, 비유일이면 cell 0 |
| `tests_backend/test_clarification_option_paging_v2.py` | 484개 전체 pagination 도달, search, cursor mismatch, option token tamper, prefill 없음 |
| `tests_backend/test_request_scoped_cache_v1.py` | profile prefetch/resolver physical 1회, negative hit, encoder normalized query hit, retrieval hit receipt, request 간 공유 0 |
| `tests_backend/test_speculative_retrieval_v1.py` | deadline/target/query/top-k 제한, retry 0, Cell/HCX answer 0, high-impact answer 후 full retrieval 재실행 |
| 기존 `test_resumable_clarification_annual_v1.py` | 기대 호출을 최초 `l1,l2,layers` 후 질문, resume `layers/live`로 갱신하고 L1/L2 0을 유지 |
| 기존 `test_article_multi_target_v1.py` | target별 timing/cache/clarification receipt 및 결과 순서 결정론 |
| 기존 `test_runtime_closure_v1.py` | 74 paths 유지 및 변경 SHA 전부 일치 |

### 9.3 필수 정확성 gate

1. 질문 반환 전 Cell API 0, HCX answer 0, deterministic answer 0.
2. resume 후 L1 0, HCX L2 0.
3. indicator/item/unit/source/population 보완 후 full retrieval physical call `> 0`.
4. selector-only 보완은 membership/profile SHA가 같을 때 retrieval physical call 0, binding call `> 0`.
5. option 선택 후 globally unique QUERY_READY가 아니면 Cell API 0.
6. profile incomplete 후보에서 fabricated option 0.
7. 처음부터 완전한 입력과 재질의 완료 입력의 query plan/cell/verdict/table/send_de가 동일.
8. deterministic renderer의 공식값·기간·단위·지역·table/send_de는 sealed evidence와 일치.
9. HCX shadow output이 사용자 answer/verdict/cell을 변경한 사례 0.
10. PostgreSQL/OpenSearch/Qdrant write path와 redis-session job/cache 사용 0.

### 9.4 E2E gate

동일 Git/image SHA의 EC2 API와 frontend에서 다음을 수행한다.

1. 상대 기간, 날짜 없는 복합 출생 기사 -> article_date 질문.
2. 날짜 보완 -> checkpoint 재개 -> 공식 셀 답변.
3. region 누락이고 후보 profile에 여러 지역이 있는 기사 -> SEARCHABLE_OPTIONS.
4. 21개 이상, 별도 484개 fixture 축 -> UI 검색/페이지로 마지막 값까지 도달.
5. indicator 누락 기사 -> speculative indicator options 또는 bounded FREE_TEXT fallback.
6. indicator 선택 -> initial speculative 후보를 authority로 재사용하지 않고 full retrieval.
7. 완전한 기사 -> 질문 없이 공식 셀 답변.

각 실행 receipt에서 release ID/model revision/vector size 1024/normalized/query ready/cell call/timing/cache
hit를 확인한다. 데이터 계층 container ID와 count는 전후 동일해야 한다.

### 9.5 성능 gate

같은 release SHA, 같은 기사, 동일 네트워크 조건에서 cold/warm을 분리해 각 흐름 최소 20회 측정한다.
warmup은 지표에서 제외하고 p50/p95/실패율을 분자·분모와 함께 보고한다.

| 흐름 | 1차 목표 |
|---|---:|
| 날짜 없는 첫 질문 | p95 <= 10초 |
| 날짜 보완 후 공식값 | p95 <= 15초 |
| 두 요청 서버 합계 | p95 <= 25초 |
| 처음부터 완전한 기사 | p95 <= 15초 |
| timing instrumentation overhead | 동일 fake-adapter test p95 <= 2% 또는 5ms 중 큰 값 |

성능 목표를 못 맞춰도 정확성 gate를 완화하지 않는다. target 병렬화는 이 설계 범위 밖이며 P0 제거와
cache 측정 뒤 별도 설계로 진행한다.

## 10. 동결 자산과 데이터 계층 비수정 경계

수정 금지:

- `data/develop/r0_blind_retrieval_gold_20260804/**`
- `data/develop/r3_full_l2_gold_20260804/**`
- `data/develop/article_hcx_holdout_20260729/evaluation/l2_gold_human_98_20260731.jsonl`
- `data/develop/r17_stratum_freeze_20260731/**`
- `r4b_unmatched_meaning_gold_*.jsonl`, `r4b_gold_profiles_merged_*.jsonl`
- `evaluate_r3_gate_b.py`, `evaluate_six_fields.py` 채점 산식
- `src/source_scope_classifier.py`
- 기존 historical SHA receipt와 동결 measurement 산출물
- migration 001/002

데이터 계층 금지:

- PostgreSQL canonical/application write, schema/migration 실행
- OpenSearch index 생성/수정/재색인/alias-current 변경
- Qdrant collection write/재임베딩/삭제
- Redis session namespace에 checkpoint/cache/job 저장
- 데이터 계층 container/volume/EBS 재생성·재시작·변경

허용되는 외부 작업은 release-pinned PostgreSQL profile read, OpenSearch/BM25 read, BGE query encode,
Qdrant dense read, KOSIS official search, QUERY_READY 이후 KOSIS Cell read뿐이다.

## 11. 구현 순서와 handoff 기준

1. Luna(high)는 timing recorder와 deterministic answer default부터 구현한다.
2. pre-retrieval Gate A와 checkpoint v2를 구현한다.
3. request-scoped profile/query/retrieval cache를 구현한다.
4. SlotDiagnostic과 Gate B option planner를 구현한다.
5. option paging API/frontend를 구현한다.
6. bounded synchronous speculative retrieval을 구현한다.
7. manifest SHA/size를 갱신하고 focused+differential+E2E+performance gate를 수행한다.
8. Sol(medium)은 이 문서의 requirement ID별 누락을 검사하고 refactor plan만 작성한다.
9. Terra(high)는 승인된 refactor plan만 적용한다.
10. Sol(medium)은 동일 bytes/SHA에서 최종 검사한다.

구현자가 임의 판단으로 메우면 안 되는 항목은 다음과 같다.

- role enum과 invalidation matrix
- option을 합치는 semantic 기준
- speculative budget과 authority
- answer mode의 기본값
- timing public allowlist
- cache key와 수명
- QUERY_READY/Cell API 권한 경계
- 74-file closure 유지

이 중 변경이 필요하면 구현을 멈추고 이 설계 문서의 새 revision 승인을 받아야 한다.

## 12. 완료 정의

최종 해결은 아래가 모두 참일 때만 완료다.

- 날짜/필수 필드가 없을 때 비싼 live·Cell·answer를 수행하기 전에 필요한 필드만 질문한다.
- Late Binding 후 공통 missing selector는 실제 candidate profile 전체 inventory에서 안전한 선택지를
  제공하며 모든 값에 도달할 수 있다.
- 사용자 선택은 semantic constraint로 모든 후보에 재투영되고 Strict Validator를 통과한 유일
  assignment만 Cell API를 호출한다.
- high-impact field 보완은 full retrieval을 재실행해 speculative self-reinforcement를 차단한다.
- 기본 사용자 답변은 deterministic sealed renderer로 즉시 반환되고 HCX answer는 critical path가 아니다.
- 단계/target별 timing과 logical/physical cache receipt로 지연 원인을 추정이 아닌 측정으로 설명한다.
- 기존 공식 cell/query plan/verdict 정확성이 유지되고 frozen/data-layer bytes는 바뀌지 않는다.
