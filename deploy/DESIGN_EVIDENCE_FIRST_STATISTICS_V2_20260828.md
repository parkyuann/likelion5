# 설계도 — 근거 중심 통계 답변과 시계열 주장 v2 (20260828)

## 1. 목적

사용자 화면의 중심을 `일치/불일치` 분류에서 현재 KOSIS 공식값, 계산식, 기사 기재값,
자료 시점 한계를 설명하는 답변으로 바꾼다. 기존 deterministic verdict는 평가·감사·안전 게이트용
내부 필드로 유지하며 채점 산식과 동결 gold는 변경하지 않는다.

대상 예문은 2026년 4월 출생아 24,521명, 전년동월 대비 3,734명·18.0% 증가 주장과
22개월 연속 증가, 2019년 4월 이후 최대, 1982년 이후 증가율 최대, 1992년 4월 이후
증가폭 최대 주장이다.

## 2. 권위와 시점

- 현재 개발 답변은 `truth_mode=CURRENT_RELEASE`만 제공한다.
- 기사 발행 당시 snapshot이 없으면 `AS_PUBLISHED` 판정을 만들지 않는다.
- 답변에는 KOSIS 셀의 `LST_CHN_DE`, 조회 시각, table key, query plan, raw response SHA를 보존한다.
- 현재값과 기사값의 차이는 사실로 설명하되 그 원인을 통계 개정으로 단정하지 않는다.
- 과거 snapshot이 없으면 `historical_vintage_status=UNAVAILABLE`과 한계를 출력한다.

## 3. L3/L4 기간·지표 보완

1. `지난 4월`은 하나의 기간 표현으로 제거해 indicator가 `출생아 수`로 남아야 한다.
2. `1년 전보다`는 YOY와 generic period-to-period가 동시에 잡히더라도 더 구체적인 YOY 하나로
   결정한다. 서로 다른 실제 비교 marker가 공존할 때만 기존처럼 `NONE`으로 닫는다.
3. 결과 period pair는 measurement `2026-04`, baseline `2025-04`, basis `YOY`여야 한다.
4. 특정 기사·통계표 이름·숫자를 규칙에 하드코딩하지 않는다.

## 4. 같은 계열 두 시점 비교 계약

`LEVEL` target이 전역 유일성 검사를 통과해 `QUERY_READY`이고 현재 셀이 정확히 한 건
`CELL_RESOLVED`일 때만 sibling `CHANGE_POINT`·`CHANGE_RATE`를 결합한다.

기사 전체 실행의 최초 target은 값이나 검색 순위로 고르지 않는다. L2/L5가 보존한
`value_span_id` 중 동일 source region에 `LEVEL`과 change sibling이 함께 있고 measurement period와
indicator가 완전한 묶음을 만든다. 그 묶음이 전역 유일할 때만 `LEVEL value_span_id`를 primary로
고정한다. 0개 또는 2개 이상이면 명시적 UI target ID 없이는 `PRIMARY_TARGET_AMBIGUOUS`로 닫는다.
숫자값은 table binding이나 primary 선택 tie-break에 사용하지 않는다.

- sibling 조건: 같은 article, 같은 sentence/source region, 같은 정규화 indicator, 같은 measurement
  period, 명시된 baseline period.
- baseline/range plan은 선택된 current plan에서 period 필드(`start_prd_de`, `end_prd_de`)만
  바꾼다. period 필드를 제거한 canonical JSON이 완전히 같아야 하며 profile SHA와 release_id도
  같아야 한다.
- table/item/dimension/frequency/release는 불변이며 관측값으로 검색·결박을 다시 하지 않는다.
- current와 baseline은 각각 exact one-cell cardinality와 요청 identity를 검증한다.
- period-only requery 동안 검색·reranker·projection·selector 호출은 각각 0회다. `release_id`,
  `org_id`, `tbl_id`, `itm_id`, `prd_se`, `obj_levels`, unit identity, selected assignment provenance 중
  하나라도 바뀌면 `SERIES_IDENTITY_CHANGED`로 닫는다.
- 계산은 Decimal 단일 경로로 수행한다.

```text
difference = current - baseline
percent_change = (current - baseline) / abs(baseline) * 100
```

한 문장에 두 change target이 있으면 같은 두 셀로 모두 계산한다. 내부 component verdict는
보존하지만 사용자 문구는 다음 순서로 구성한다.

```text
현재 KOSIS 통계표에서는 2026년 4월 출생아 수가 24,521명이고,
2025년 4월은 20,764명입니다. 따라서 전년동월 대비 증가는 3,757명,
증가율은 약 18.1%입니다. 기사에는 3,734명·18.0%로 적혀 있습니다.
현재 KOSIS 조회값과 기사 기재값이 다른 원인은 기사 작성 당시의 공식 통계 snapshot이 없어
확인할 수 없습니다.
```

## 5. 범위 주장 계약

범위 주장은 선택된 동일 월별 series에서만 계산하며 별도 검색으로 표를 바꾸지 않는다.
KOSIS range 응답은 요청 기간의 모든 월이 정확히 한 번씩 존재하고 정렬·중복·계열 identity가
검증될 때만 사용한다.

동일 달 비교는 원문에 `4월 기준`, `YYYY년 4월`처럼 월 span이 명시되거나 같은 source region의
`지난 4월`을 모호함 없이 상속할 수 있을 때만 허용한다. 이 근거가 없으면 모든 월을 훑은 뒤
임의로 동일 달 비교로 축소하지 않고 `RANGE_CLAIM_MONTH_SCOPE_AMBIGUOUS`로 닫는다.

문장 간 담화 상속은 다음 조건을 모두 만족할 때만 직전 문장의 명시 월 span을 한 문장 앞으로
상속한다: 동일 article, 동일 paragraph, 바로 인접한 문장, 아래 `indicator_family_key` 동일, 직전 문장에
`M월 기준` 또는 명시적 `YYYY년 M월` provenance 존재, 현재 문장에 경쟁 month/quarter/YTD 표현
없음, 중간 indicator 전환 없음. 두 개 이상의 후보, 문단 경계, 한 문장 초과 거리, 현재 문장의
다른 기간 표현이 있으면 `RANGE_CLAIM_DISCOURSE_SCOPE_AMBIGUOUS`로 거부한다. receipt에는
donor/receiver sentence ID와 원문 month span을 저장한다.

`indicator_family_key`는 L2가 보존한 indicator 원문 span에서 Unicode·공백을 정규화한 뒤 문장
끝의 measurement-role phrase만 닫힌 목록으로 반복 제거해 만든 base token sequence다. 닫힌 목록은
`수`, `건수`, `규모`, `수준`, `증가율`, `감소율`, `증감률`, `증가폭`, `감소폭`, `증감폭`이며,
예를 들어 `출생아 규모`와 `출생아 수 증가율`은 모두 base `출생아`가 된다. 제거 후 빈 값이면
상속하지 않는다. 숫자값, 검색 순위, 선택된 표·cell은 family 생성·동일성 근거로 사용하지 않는다.
인접 문장이 다른 base token sequence이면 `RANGE_CLAIM_INDICATOR_FAMILY_MISMATCH`, 둘 이상의 family
후보가 있으면 `RANGE_CLAIM_INDICATOR_FAMILY_AMBIGUOUS`로 거부한다. 실제 donor `출생아 규모`와
receiver `출생아 수 증가율` 허용 fixture 및 `사망자 수 증가율` 거부 fixture를 사전등록한다.

anchor 포함 규칙은 원문 표현별로 고정한다.

- `YYYY년 M월 이후 N년 만`: `AFTER_ANCHOR_EXCLUSIVE`. anchor 값은 근거로 보존하되 최대값
  비교 집합에서는 제외하고, anchor 다음 해부터 현재 직전 해까지의 같은 달과 현재를 비교한다.
- `YYYY년 ... 이래`: `SINCE_INCLUSIVE`. anchor와 현재를 포함한 같은 달 전체를 비교한다.
- 문구가 위 두 의미 중 하나로 확정되지 않으면 연산하지 않는다.

지원 연산자는 다음 네 가지다.

1. `YOY_STREAK`: 각 월을 정확히 12개월 전과 비교해 모두 `current > baseline`인지 확인한다.
   22개월 연속 주장에는 비교 대상 22개월과 그 전년동월을 합친 34개의 연속 raw month가
   정확히 필요하다. raw 34개와 비교 22개가 정확한 분모다.
2. `MONTH_OF_YEAR_MAX_SINCE`: 2019.04~2026.04 같은 달 raw cell 8개를 요구한다. anchor는
   evidence에 남기고 exclusive 비교 집합은 2020~2026의 7개다.
3. `YOY_RATE_MAX_SINCE`: 1982~2026 증가율 45개를 위해 1981.04~2026.04 같은 달 raw cell
   46개를 요구한다. inclusive 비교 분모는 45개이며 baseline 0이면 거부한다.
4. `YOY_DIFFERENCE_MAX_SINCE`: 1993~2026 차이 34개와 1992 anchor 증가폭 표시를 위해
   1991.04~2026.04 같은 달 raw cell 36개를 요구한다. 1992 차이는 anchor evidence로만 보존하고
   exclusive 비교 분모 34개에는 포함하지 않는다.

각 연산의 expected-period set과 raw cardinality가 정확히 일치해야 한다. 누락은
`RANGE_CELLS_MISSING`, 중복은 `RANGE_CELLS_DUPLICATED`, 예상 밖 period는
`RANGE_CELLS_UNEXPECTED`, 0 baseline은 `RANGE_ZERO_BASELINE`, identity 변화는
`SERIES_IDENTITY_CHANGED`로 닫는다.

문장 패턴은 일반적인 기간·순위 표현과 원문 span을 요구하며, duration·anchor·현재 period가
서로 산술적으로 일치하지 않으면 `RANGE_CLAIM_PERIOD_INCONSISTENT`로 닫는다. 누락 월, 중복 월,
0 baseline, table/release 불일치가 있으면 해당 범위 답변을 생성하지 않는다.

아래 값은 구현 전 read-only feasibility probe로 얻은 사전등록 기대값이며, 성공 결과가 아니다.
성공은 구현 후 새 L1→cell receipt가 같은 결과와 provenance를 재현할 때만 선언한다.

- 2024.07~2026.04 전년동월 증가: 22/22개월.
- 2026.04 24,521명: 2019.04 이후 비교 구간에서 최대.
- 2026.04 증가율 약 18.1%: 1982년 이후 4월 증가율 최대. 현재 release의 직전 최대는
  2025.04 약 9.1%다.
- 2026.04 증가폭 3,757명: 1992.04 anchor(4,043명)는 `이후 34년 만`의 비교 집합에서
  제외하되 근거로 표시하고, 1993.04~2026.04 비교 집합에서는 최대다.

## 6. 사용자 응답 계약

새 top-level `evidence_answer`는 다음을 포함한다.

- `text`: 공식값 중심 자연어 답변.
- `truth_mode=CURRENT_RELEASE`.
- `table_key`, `periods`, `observed_values`, `calculations`.
- `range_findings`와 각 연산의 입력 범위·분모.
- `historical_vintage_status`와 limitation.
- 내부 `component_verdicts`는 디버그/감사용이며 기본 UI에서 분류 제목으로 노출하지 않는다.

기존 문장별 `verdict`는 호환성과 내부 집계를 위해 유지한다. 프론트는 기사 결과 카드 상단에
`evidence_answer.text`를 먼저 표시하고 일치/불일치 요약 숫자를 주된 결론으로 사용하지 않는다.

`CURRENT_RELEASE` 사용자 문구는 반드시 `현재 KOSIS 통계표에서는`으로 시작한다. 기사 당시
snapshot이 없을 때 `기사가 참/거짓`, `기사 당시 공식값`, `공식 통계와 일치/불일치`를 headline,
badge, fallback 어디에도 표시하지 않는다. 대신 `기사에는 ...로 적혀 있습니다`와
`현재 KOSIS 조회값과 기사 기재값이 다른 원인은 기사 작성 당시의 공식 통계 snapshot이 없어
확인할 수 없습니다`를 사용한다.

## 7. 실패 폐쇄

- `QUERY_READY` 전 cell API 0회.
- 현재 셀 실패 시 baseline/range 호출 0회.
- sibling이 없거나 모호하면 단일 공식값 설명만 출력한다.
- range extraction이 없거나 불완전하면 paired answer 성공을 막지 않고 해당 finding만
  `not_evaluated`로 남긴다.
- 답변 모델은 sealed evidence를 문장화할 수만 있고 계산·표 선택·판정은 바꾸지 못한다.
- legacy SQLite/Bearer/local search fallback, 데이터 write, index/collection 변경은 금지한다.

## 8. 구현 범위

모든 새 cell/range 실행은 기본값 false인 `EVIDENCE_FIRST_STATISTICS_SHADOW_ENABLED` opt-in
shadow gate 뒤에 둔다. 로컬 실험에서만 승인된 환경으로 true를 사용하며, 이번 commit으로 EC2
기본값을 true로 바꾸거나 mainline/production 승격하지 않는다. 실제 실험 성공과 EC2 활성화는
별도 결과 승인으로 분리한다.

- `indicator_text.py`, `l4_field_normalization.py`: 두 일반화 결함 보완.
- 신규 runtime module: same-series pair/range 계산과 evidence-first text 생성.
- `run_pipeline_operational_v2.py`: 성공한 current level 뒤 opt-in evidence synthesis 연결.
- `operational_answer_v2.py`: 내부 verdict 불변, 사용자 headline을 공식값 설명형으로 변경.
- backend/frontend: `evidence_answer` 투영 및 표시.
- runtime closure manifest와 관련 테스트·배포 inventory 갱신.

## 9. 검증 게이트

1. focused unit tests: 기간 충돌, indicator 정규화, 두 시점 plan 불변, exact cardinality,
   Decimal 계산, 범위 4연산, vintage limitation, 답변 숫자 sealing.
   - `지난 4월` 원자 제거와 `지난 4개월`, `지난 4월간`, `4월물`, 월 범위 1~12 반례를 검사한다.
   - 동일 span YOY/generic 중복은 YOY 우선, 서로 다른 YOY/MOM/P2P marker는 `NONE`인지 검사한다.
   - 특정 기사 숫자·표면형 literal이 runtime에 추가되지 않았음을 검사한다.
   - CURRENT_RELEASE 금지 문구가 headline·badge·fallback에 없는지 검사한다.
   - 22개월 연속은 정확히 34 raw month를 요구하는 회귀 테스트를 둔다.
   - anchor exclusive/inclusive와 같은 달 범위 provenance를 각각 검사한다.
2. 변경 전 exact differential baseline은 **1916 passed / 11 failed / 1 skipped**다. full JUnit은
   1915/12/1을 기록했지만 추가 1건은 1초 timing test였고 즉시 독립 3회 재실행에서 3/3 통과했다.
   이 node는 통과 집합에 봉인하며 변경 후 full에서도 반드시 통과해야 한다. 기존 11개
   failure ID는 `baseline_outcomes_v7r5.json`의 exact set(SHA-256
   `a6340ebb0a81c7bd498e649cc23774f37d955ad68d0b4f0d738533be06bc3a4b`)과 동일함을 새
   preimplementation JUnit에서 재확인해 봉인한다. 변경 후 신규 실패 0, 기존 통과 감소 0이며
   실패 대체를 허용하지 않는다.
3. frontend production build, backend tests, secret scan, `git diff --check`.
4. 동일 예문 실제 L1~L5→검색→Late Binding→KOSIS cell→최종 답변 실행.
5. 실제 실행에서 current/baseline 24,521/20,764, difference 3,757, rate 약 18.1,
   range finding 4종이 근거와 함께 출력되어야 성공이다.
6. 새 독립 Sol이 코드·실행 receipt·변경 범위를 검토해 승인한 뒤에만 commit/push한다.

## 10. Git·배포 경계

- 정본 프로젝트 `E:\news_verification_1`, Git checkout
  `E:\news_verification_1\github_handoff_likelion5`, EC2 실행 상태를 별도로 취급한다.
- 현재 checkout의 기존 날짜 보완 변경을 덮어쓰지 않는다.
- 구현 시작 전 보호 파일 SHA-256과 dirty patch blob을 기록하고, 구현 중 예상하지 않은 변화나
  충돌이 발견되면 즉시 중단한다. 기존 untracked architecture 이미지/초안은 commit 범위에서
  제외한다.
- 보호 receipt는 기존 tracked 수정 12개 전체의 경로·SHA-256, `git diff` blob SHA, untracked
  목록, 날짜 보완 focused test 결과를 포함한다. 날짜 관련 기존 테스트는 변경 전후 동일하게
  통과해야 한다.
- force push, EC2 데이터 계층 변경, 재색인·재임베딩·alias 변경은 하지 않는다.
- 성공 시 최신 `origin/develop`과 fast-forward 가능 여부를 재확인하고 검증 커밋만 develop에 push한다.
