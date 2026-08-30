# 기사 발행일 확인 및 상대기간 재실행 설계 v1

- 날짜: 2026-08-27
- 상태: `PENDING_INDEPENDENT_APPROVAL`
- 기준 브랜치: `feat/ec2-readonly-adapters-auth-20260827`
- 기준 HEAD / `origin/develop`: `598b8125f79224dd90e10d7d32b7b8733f555397`
- 대상: application overlay의 기사 본문 입력과 승인된 pipeline runtime closure
- 비대상: EC2 기동, 데이터 계층 변경, migration, 재색인·재임베딩, 검색 ranking 변경

## 1. 확인된 문제

사용자가 붙여넣은 기사 본문에는 발행일이 없었지만 수동 테스트 fixture에
`date="2026-08-24"`가 주입됐다. 현재 terminal helper도 날짜가 없으면
`date.today()`를 기본값으로 사용한다. 이 값은 기사에서 확인된 사실이 아니므로
`지난 4월`, `올해 4월`, `작년` 같은 상대기간을 절대기간으로 바꾸는 근거가 될 수 없다.

실제 실행에서는 L2 6건과 routed 6건이 생성되고 후보 50건까지 확보됐지만,
Late Binding이 `지난 4월 -> 2026-04`의 손실 없는 근거를 승인하지 않아
`PERIOD_INVALID`, `cell_api=0`, `UNVERIFIABLE`로 종료됐다.

사용자에게 발행일을 다시 묻는 것만으로도 충분하지 않다. 현재
`_anchored_period_normalization()`은 `4월`, `올해 4월` 일부가 아니라
`작년`, `지난해`, `올해`, `올해 N분기`, `N월`만 제한적으로 허용하며
`지난 N월`을 승인하지 않는다. 따라서 입력 확인과 relative-period grammar 보완을
같은 변경에서 닫는다.

## 2. 결정

### 2.1 날짜가 없으면 pipeline을 호출하지 않는다

`POST /api/v1/verify/develop`과 `POST /api/v1/analyze`의 명시적 article 경로에서
`date`가 비어 있으면 L1 이전에 다음 응답을 반환한다.

```json
{
  "type": "needs_user_input",
  "status": "awaiting_article_date",
  "reason": "ARTICLE_DATE_REQUIRED",
  "question": {
    "id": "article_published_date",
    "prompt": "기사 발행일을 YYYY-MM-DD 형식으로 알려주세요.",
    "input_mode": "DATE",
    "required": true
  }
}
```

- HTTP 200을 사용한다. 실패가 아니라 대화형 입력 대기 상태다.
- 응답에는 기사 원문, 내부 경로, target ID, 임시 session ID를 넣지 않는다.
- L1, HCX L2, 검색, metadata, cell, answer 호출은 모두 0회다.
- 이 0-call 응답은 `PIPELINE_RUNTIME_ENABLED` 확인보다 먼저 만들어야 한다.
  FastAPI route-level `Depends(require_pipeline_runtime)`는 handler보다 먼저 실행되므로
  `/api/v1/verify/develop`에서 제거하고, 유효한 날짜가 확인된 분기에서만
  `require_pipeline_runtime()`과 `_load_trace_runner()`를 순서대로 호출한다.
- `/api/v1/analyze`도 explicit article 입력에 동일한 날짜 확인 함수를 먼저 적용한다.
- 서버 DB나 Redis에 미완료 본문을 저장하지 않는다. product-state 002가 아직 없기 때문이다.

### 2.2 사용자의 날짜 응답은 브라우저 메모리에만 보관한 원 요청과 결합한다

frontend는 `needs_user_input`을 받으면 원래 본문·제목·conversation ID를 React 메모리에만
보관하고 질문을 채팅에 표시한다. 다음 입력은 발행일 응답으로 처리한다.

- 허용 형식: 실제 달력에 존재하는 `YYYY-MM-DD`
- 유효한 날짜: 원 본문을 같은 endpoint로 재요청하며 `date_source="user_feedback"`
- 잘못된 날짜: pipeline 호출 없이 형식을 다시 안내한다.
- `취소`: 보관 중인 요청을 폐기하고 일반 입력 상태로 돌아간다.
- 새로고침: 보관 상태가 사라진다. localStorage·URL query·서버 session에는 저장하지 않는다.

URL 추출기가 발행일을 제공한 경우에는 `date_source="url_metadata"`로 바로 실행한다.
그 값이 없으면 본문 붙여넣기와 동일하게 질문한다.

### 2.3 backend 입력과 provenance

`DevelopVerifyRequest`와 `AnalyzeRequest`에 다음 필드를 추가한다.

```text
date: YYYY-MM-DD 또는 빈 문자열
date_source: user_feedback | url_metadata | api_request | null
```

- 날짜가 있으면 strict calendar date로 검증한다.
- 날짜가 있는데 source가 없으면 하위 호환을 위해 `api_request`로 기록한다.
- `/api/v1/analyze`의 explicit article 분기는 유효한 `date`와 `date_source`를
  verifier에 그대로 전달한다. 자연어·URL·image PENDING 경계는 변경하지 않는다.
- pipeline 입력 JSONL에는 `date`와 함께 bounded provenance를 기록한다.
- live runtime이 routed row에 넣는 `article_date_provenance`는 입력 provenance와
  article body SHA를 포함한다.
- 현재 시각이나 파일 생성시각으로 발행일을 추정하지 않는다.

### 2.4 `지난 N월`의 손실 없는 anchor 검증

`_anchored_period_normalization()`에 `지난 N월`을 추가한다.

```text
anchor = 기사 발행일 YYYY-MM-DD
N < anchor.month  -> anchor.year의 N월
N > anchor.month  -> anchor.year - 1의 N월
N == anchor.month -> 모호하므로 승인하지 않음
```

예:

- 발행일 `2026-08-24`, `지난 4월` -> `2026.04`
- 발행일 `2026-02-10`, `지난 4월` -> `2025.04`
- 발행일 `2026-08-24`, `지난 8월` -> 자동 확정하지 않고 `PERIOD_INVALID`

계산 결과와 L3 structured period가 동일하고 N이 발행월과 다를 때만
`rule_id="anchor-most-recent-named-month"`, `lossless=true`를 기록한다.
날짜가 없거나 structured period가 다르면 기존처럼 `PERIOD_INVALID`로 fail-closed한다.

## 3. 구현 범위

### canonical source

- `src/news_verification/runtime/r4c1_claim_core_v2.py`
- `src/news_verification/runtime/run_pipeline_operational_v2.py`
- `src/news_verification/runtime/run_pipeline_terminal_v1.py`
- 관련 canonical tests

terminal 경로는 `date.today()` 기본값을 제거하고 날짜가 없으면
`ARTICLE_DATE_REQUIRED`로 중단한다. 대화형 모드에서는 빈 기본값 없이 발행일을 묻는다.

### develop application overlay

- `backend/app.py`
- `backend/develop_verify_service.py`
- `frontend/src/api.js`
- `frontend/src/ChatApp.jsx`
- 관련 backend tests

### packaged runtime closure

- 변경된 canonical runtime 파일을 `deploy/pipeline_runtime/src/**`에 동일 바이트로 반영
- `deploy/pipeline_runtime/manifest.json`의 해당 size/SHA만 재산출
- 동결 gold, historical receipt, model/image receipt는 수정하지 않는다.

## 4. 검증 기준

1. 날짜 없는 요청은 `needs_user_input`을 반환하고 `_load_trace_runner` 호출 0회다.
   `PIPELINE_RUNTIME_ENABLED=false`에서도 HTTP 200과 호출 0회를 유지한다.
   `/api/v1/verify/develop`과 `/api/v1/analyze` article 경로 모두에 적용한다.
2. 잘못된 날짜는 422 또는 frontend 재질문이며 pipeline 호출 0회다.
3. 유효한 사용자 날짜는 원 본문·제목을 유지해 한 번만 재실행한다.
   `/api/v1/analyze`는 verifier를 정확히 1회 호출하며 날짜와 provenance를 전달한다.
4. URL metadata 날짜는 추가 질문 없이 실행한다.
5. `취소` 후 pending 본문은 재사용되지 않는다.
6. `2026-08-24 + 지난 4월 + 2026.04`는 anchored normalization을 만든다.
7. `2026-02-10 + 지난 4월 + 2025.04`는 anchored normalization을 만든다.
8. `2026-08-24 + 지난 8월`은 structured 후보가 현재 연도 또는 전년도여도
   anchored normalization을 만들지 않는다.
9. anchor와 맞지 않는 structured period, 날짜 없음, 잘못된 날짜는 fail-closed한다.
10. 기존 absolute period 및 기존 relative-period 테스트 통과 감소가 없다.
11. canonical 변경 파일과 packaged closure 파일의 raw SHA가 일치한다.
12. closure manifest는 전체 파일 size/SHA가 모두 일치한다.
13. backend tests, frontend lint/build, `git diff --check`, secret scan을 통과한다.

## 5. 비범위와 배포 경계

- EC2 `docker compose up`, migration, 데이터 적재, index/collection 변경을 하지 않는다.
- 사용자의 기사 원문을 Redis session 또는 application DB에 저장하지 않는다.
- 발행일을 모델로 추정하거나 검색 결과에서 역추론하지 않는다.
- `PERIOD_INVALID` 이외의 retrieval/binding 문제를 이 변경에서 우회하지 않는다.
- 현재 handoff 작업트리의 기존 미추적 파일
  `ARCHITECTURE_DRAFT.md`, `architecture_overview.png`, `architecture_overview.svg`는
  읽거나 수정하거나 stage하지 않는다.
- 구현 후 독립 Sol 결과 승인 전에는 commit/push하지 않는다.
