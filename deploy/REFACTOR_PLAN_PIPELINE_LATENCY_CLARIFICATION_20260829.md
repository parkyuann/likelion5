# 파이프라인 지연·재질의 구현 중간 리뷰 및 리팩터 계획 (20260829)

## 1. 판정

**NEEDS_REFACTOR — 현재 구현은 구성요소 단위 기능은 존재하지만 설계한 서비스 종단 계약은 아직 닫히지 않았다.**

확인 범위는 `deploy/DESIGN_PIPELINE_LATENCY_CLARIFICATION_20260829.md`와 현재 working tree diff이다. 코드는 수정하지 않았다. 제공된 전체 테스트 결과는 **150개 중 139 passed / 11 failed**이며, 그중 search adapter 5건은 현재 Anaconda 환경의 `psycopg` 미설치로 인한 실패다. 이번에 추가된 테스트는 주로 helper 단위 검증이므로 실제 API → checkpoint → runtime → 재개 종단 연결의 증거로는 충분하지 않다.

## 2. 확인된 구현 상태

| 항목 | 현재 상태 | 판정 |
|---|---|---|
| Gate A | `verify_article_develop()`이 layers 다음, live 호출 전에 `_pre_live_clarification_plan()`을 호출한다. 상대 시점+발행일 누락은 live 없이 질문한다. | 부분 연결 |
| Gate B | runtime이 post-binding `build_post_binding_clarification_plan()`을 호출하고 질문 ledger를 남기며 Cell API를 호출하지 않는다. | 부분 연결 |
| speculative retrieval | runtime helper는 질의 최대 2개, Top-30, profile 30개, retry 0, Cell/HCX 0의 동기 probe를 구현했다. | 서비스 흐름 미연결 |
| deterministic renderer | 기본값은 `DETERMINISTIC_ONLY`이고 HCX 객체를 호출하지 않는다. `HCX_SHADOW_SYNC`도 사용자 답변을 바꾸지 않는다. | 핵심 경로 제거 완료에 가까움 |
| request cache | profile/query vector/retrieval request-local cache와 audit 객체가 추가됐다. | runtime 연결됨, receipt 보완 필요 |
| timing | monotonic recorder, service stage timing, target wall/retrieval timing이 추가됐다. | 세부 span/public 투영 미완성 |
| option API/UI | options endpoint, 봉인 bundle paging/search, `option_id + value`, frontend credentials 경로가 추가됐다. | 왕복 골격 연결, 종단 검증 부족 |
| checkpoint v2 | plan/option/speculative artifact SHA와 semantic constraints가 추가됐다. | invalidation 기반 재개 미완성 |

## 3. P0 — 다음 구현 전에 반드시 닫을 항목

### P0-1. Gate A와 speculative retrieval을 하나의 실제 서비스 경로로 통합

현재 backend Gate A는 indicator 누락 시 `speculative=true`인 **빈 FREE_TEXT 질문을 즉시 반환**한다. 이 때문에 runtime 내부 `_speculative_clarification_plan()`까지 도달하지 않아, 사용자가 요구한 “재질의 중 선제적 통계표 검색 및 후보 지표 선택지 생성”이 실제 API 흐름에서는 실행되지 않는다. 또한 backend Gate A는 `item` 누락을 판정하지 않는다.

리팩터 목표:

1. layers 직후 하나의 Gate A coordinator가 모든 routed target을 검사한다.
2. 상대 시점에 article date가 필요한 경우에는 외부 호출 없이 즉시 질문한다.
3. `indicator/item` 누락·모호성에는 기존 runtime speculative helper를 호출하는 planning-only 진입점을 사용한다.
4. probe 결과의 option/speculative bundle을 checkpoint에 봉인한 뒤 질문을 반환한다.
5. probe는 final candidate authority가 아니며 사용자 응답 후 새 query register로 full retrieval을 수행한다.
6. helper 직접 호출 테스트가 아니라 `/api/v1/verify/develop`에서 retrieval call `> 0`, Cell/HCX answer call `= 0`, options 반환을 검증한다.

### P0-2. dependency-aware resume를 `layers/live` 재실행에서 `layers/retrieval/binding` 재개로 변경

현재 service는 checkpoint의 `resume_from_stage`를 `layers` 또는 `live`만 허용한다. Gate B 선택도 service에서 `live`로 재개되어 검색부터 다시 수행한다. 설계의 invalidation matrix와 달리 selector-only 응답이 후보/profile을 재사용해 binding부터 이어지지 않는다.

리팩터 목표:

1. runtime continuation과 service allowlist에 `retrieval`, `binding`을 명시적으로 추가한다.
2. `article_date/period`는 layers부터, `indicator/item/unit/source/population`은 full retrieval부터 재개한다.
3. `region/sex/age/classification` 등 selector-only 보완은 봉인된 candidate membership/profile bundle의 release ID·SHA가 모두 일치할 때만 binding부터 재개한다.
4. mismatch이면 조용히 재사용하지 말고 fail-closed 또는 retrieval 재실행 중 하나의 명시적 bounded code를 반환한다.
5. L1/L2는 모든 재개에서 0회, selector-only 재개에서는 retrieval physical call 0회임을 종단 receipt로 증명한다.

### P0-3. Gate B의 역할·후보 처리 범위를 설계 계약과 일치

backend는 12개 clarification role을 허용하지만 runtime `_CLARIFICATION_ROLES`는 `article_date/period/region/population/indicator/unit` 6개뿐이다. 따라서 `item/source/sex/age/classification/measurement_basis` 응답은 runtime 병합 단계에서 `CLARIFICATION_INVALID`가 된다.

또한 post-binding planner는 후보 중 하나라도 `PROFILE_INCOMPLETE`이면 전체 plan을 포기한다. 설계 계약은 불완전 profile 후보만 제외하고, 완전한 후보들에서 공통으로 보완 가능한 slot과 전체 inventory를 구성하는 것이다.

리팩터 목표:

1. backend/API/runtime/checkpoint의 role enum을 단일 상수 또는 동일 계약으로 통일한다.
2. profile incomplete 후보는 option source에서 제외하고 exclusion receipt를 남긴다. 모든 후보가 불완전할 때만 `METADATA_PROFILE_INCOMPLETE`로 종료한다.
3. 모든 남은 후보가 같은 필수 role에서 막힌 경우에만 option 질문을 만든다.
4. 사용자가 고른 label을 내부 selector로 직접 확정하지 않고, sealed applicability의 `table_key + profile SHA + axis/value`와 대조한 semantic constraint로 적용한 뒤 **전체 후보를 다시 Late Binding + Strict Validator**한다.
5. 유일한 `QUERY_READY`가 아니면 Cell API는 0회여야 한다.

### P0-4. `MULTIPLE_COMPATIBLE_SERIES`의 502 재현 경로를 명시적 Gate B 응답으로 봉합

현재 option inventory가 존재하는 경우에는 질문 ledger로 전환되지만, inventory가 없거나 일부 profile이 불완전한 `MULTIPLE_COMPATIBLE_SERIES`는 v2 plan을 만들지 못한다. 따라서 과거 502 사례 전체를 닫았다고 볼 수 없다.

리팩터 목표:

1. 실제 `MULTIPLE_COMPATIBLE_SERIES` shape을 fixture로 고정한다.
2. profile 기반으로 구분 가능한 경우 `needs_user_input + OPTIONS/SEARCHABLE_OPTIONS`를 반환한다.
3. 구분 가능한 안전한 선택지가 없으면 fabricated option 없이 명시적 `unverifiable`/bounded limitation으로 반환한다.
4. 두 경우 모두 HTTP generic 502, Cell API 호출, HCX answer 호출이 없어야 한다.

### P0-5. speculative deadline을 실제 wall-clock 제한으로 만든다

현재 코드는 각 retrieval/profile 호출 **전후**에 경과시간을 확인할 뿐, 한 번의 외부 호출이 2.5초를 넘는 것을 중단시키지 못한다. 따라서 `deadline_ms`는 audit 표기일 뿐 엄격한 상한이 아니다.

리팩터 목표:

1. 남은 budget을 각 channel/profile 호출 timeout에 전달한다.
2. 총 wall deadline 초과 시 추가 호출을 시작하지 않고 `DEADLINE_EXCEEDED` FREE_TEXT fallback을 반환한다.
3. detached/background job은 만들지 않는다. 요청당 target 1, query 2, Top-30, retry 0, Cell/answer 0을 유지한다.

## 4. P1 — P0 직후 보완할 항목

### P1-1. timing receipt를 설계한 관측 단위까지 연결

현재 service timing은 L1을 포함하지 않고, runtime recorder는 주로 `preflight/live` 합계만 제공한다. target ledger도 `wall_ms/retrieval_wall_ms` 중심이며 `metadata binding/cell/answer`의 개별 wall span과 logical/physical cache 수치가 public target receipt에 충분히 투영되지 않는다.

보완 항목:

- top-level: `l1/l2/layers/preflight/retrieval/binding/cell/answer/total`.
- target: retrieval parallel wall, channel sum, profile binding, cell API, deterministic answer, HCX shadow.
- 질문 응답: Cell/answer 0과 speculative span 분리.
- 음수·secret·원문 query·vector·raw profile/path 미노출 allowlist.

### P1-2. cache receipt와 identity 검증 강화

request-local cache 구현은 존재하지만 다음을 종단에서 확인해야 한다.

- profile cached value 반환 시 `release_id/table_key/profile_sha256` 재검증.
- query vector key에 model revision/vector size/normalization contract 포함.
- retrieval cache는 전체 channel 성공만 저장하고 partial failure는 저장하지 않음.
- speculative와 final retrieval identity가 충돌하지 않음.
- HTTP 요청 간 cache 공유 0.

### P1-3. frontend option 상태 일관성

options/search/page API와 `option_id + label` 전송은 연결됐다. 다만 검색 또는 페이지가 바뀔 때 이전 페이지의 선택 상태가 남을 수 있으므로, page/query 변경 시 selection을 초기화하고 현재 표시 bundle에 속한 option만 제출해야 한다. 다음 페이지는 교체/누적 정책을 명확히 하고 이전·다음 또는 검색으로 484개 전체 값에 도달 가능해야 한다.

### P1-4. checkpoint 소비·재시도 상태 전이

현재 유효 답변은 pipeline 재개 전에 checkpoint를 `CONSUMED`로 바꾼다. 재개 중 일시적 오류가 나면 같은 답변으로 재시도할 수 없는 one-shot 상태가 된다. `ACTIVE → RESUMING → CONSUMED` 또는 idempotency receipt를 도입해 동일 answer SHA의 안전한 재시도와 중복 Cell 호출 방지를 정의한다.

## 5. P2 — 정리 및 회귀 방지

1. backend에 중복된 legacy date clarification fallback을 Gate A/B coordinator로 통합한다.
2. backend/runtime role, reason code, input mode를 한 계약에서 생성해 drift를 막는다.
3. 새 helper 테스트를 실제 service integration 테스트로 보강한다. 현재 테스트는 Gate A helper, Gate B planner, speculative helper를 각각 직접 호출하므로 연결 누락을 잡지 못한다.
4. 기존 11개 실패를 환경 실패와 코드 계약 실패로 분리한다. `psycopg` 5건은 의존성 설치 환경에서 재실행하고, 나머지 6건은 현재 “날짜 무조건 선질문” 구형 기대와 새 Gate A 계약 중 어느 쪽이 정본인지 테스트를 갱신하기 전에 명시적으로 결정한다.
5. runtime manifest는 최종 byte 변경 후에만 다시 생성하고 closure SHA 검사를 수행한다.

## 6. 구현 순서

1. P0-1 Gate A coordinator와 planning-only speculative 진입점 연결.
2. P0-2 checkpoint/runtime continuation을 `retrieval/binding`까지 확장.
3. P0-3 role 통일, semantic constraint 적용, profile exclusion 규칙 구현.
4. P0-4 실제 MULTIPLE fixture로 502 제거.
5. P0-5 hard deadline 적용.
6. P1 timing/cache/public receipt 보완.
7. P1 frontend 상태 및 checkpoint idempotency 보완.
8. helper 단위 테스트 → API integration → 동일 기사 E2E 순으로 확인.

## 7. 완료 게이트

아래를 모두 만족해야 구현 완료로 본다.

1. 상대 시점+날짜 누락: layers 후 질문, retrieval/Cell/answer 0.
2. indicator/item 누락: bounded speculative retrieval `> 0`, options 또는 FREE_TEXT fallback, Cell/answer 0.
3. high-impact 답변 후 speculative 후보를 authority로 재사용하지 않고 full retrieval 수행.
4. Gate B selector 답변 후 L1/L2/retrieval physical call 0, binding부터 재개.
5. `MULTIPLE_COMPATIBLE_SERIES`에서 generic 502 0.
6. globally unique `QUERY_READY` 전 Cell API 0.
7. default answer mode에서 HCX answer call 0, 공식값 deterministic answer 유지.
8. 484개 option 전체 도달, 내부 ID 미노출, 자동 선택/prefill 0.
9. timing/cache receipt가 실제 호출 수와 일치하고 secret/query/vector/raw profile을 노출하지 않음.
10. 기존 baseline 대비 신규 실패 0, 기존 통과 감소 0. 환경 의존 실패는 동일 의존성 환경에서 별도 판정.

## 8. 최종 목표

최종 상태는 다음과 같다.

```text
기사 → L1/L2/L3~L5
  → Gate A
      ├─ 저영향 누락: 즉시 질문
      └─ indicator/item 누락: bounded synchronous speculative retrieval → 질문
  → retrieval → Late Binding
  → Gate B
      └─ profile 기반 안전한 선택지 → checkpoint
  → 사용자 응답
      └─ dependency-aware resume (layers/retrieval/binding)
  → 전체 후보 재검증 → 유일한 QUERY_READY
  → KOSIS Cell API → deterministic 공식값 답변
```

이를 통해 첫 요청의 불필요한 live/HCX 비용을 제거하고, 누락 필드가 있을 때도 후보 표의 정본 profile을 근거로 자연스러운 선택지를 제공하되, 사용자 선택이 검증 우회나 특정 셀의 직접 권한이 되지 않도록 한다.
