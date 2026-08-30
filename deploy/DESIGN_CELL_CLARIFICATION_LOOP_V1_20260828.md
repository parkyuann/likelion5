# 셀 식별 누락 필드 재질의 루프 v1 설계

## 1. 목표

기사 날짜를 파이프라인 실행 전에 무조건 요구하지 않는다. 기사 본문을 먼저
L1→L2→L3~L5→retrieval→Late Binding/Strict Validator에 통과시킨 뒤, 실제 셀을
유일하게 특정하지 못한 원인이 사용자 입력으로 보완 가능한 필드일 때만 자연어
질문을 반환한다. 사용자의 답변은 원문을 고치는 문자열이 아니라 별도
`USER_CLARIFICATION` provenance로 해당 필드에 결합하고 같은 기사를 한 번 다시
실행한다.

## 2. 외부 계약

요청은 기존 본문·제목·날짜 필드를 보존하고 다음 선택 필드를 추가한다.

```json
{
  "clarification_answers": [
    {
      "question_id": "clarify-article_date",
      "role": "article_date",
      "value": "2026-06-24"
    }
  ]
}
```

서버가 추가 정보가 필요하다고 판단하면 HTTP 200으로 다음을 반환한다.

```json
{
  "type": "needs_user_input",
  "status": "awaiting_clarification",
  "reason": "PERIOD_INVALID",
  "question": {
    "id": "clarify-article_date",
    "role": "article_date",
    "prompt": "기사에서 말한 ‘지난 4월’의 연도를 정하려면 기사 발행일을 알려주세요.",
    "input_mode": "DATE",
    "options": []
  }
}
```

질문 role은 `article_date`, `period`, `region`, `population`, `indicator`, `unit`으로
제한한다. 내부 table/item/dimension ID는 노출하지 않는다. 질문은 실제 resolver의
HOLD/누락 근거에서만 만들며, 날짜가 없다는 사실만으로 선행 질문하지 않는다.

## 3. 실행 순서

1. 날짜가 없어도 기사 JSONL에 빈 날짜로 기록하고 L1부터 live까지 실행한다.
2. `failure_recovery_shadow`를 켜서 각 target의 strict resolution과 ASK_USER 근거를
   ledger에 남긴다.
3. QUERY_READY target이 있으면 정상 답변을 반환한다. 미해결 target 중 사용자 입력으로
   해소 가능한 첫 필드만 deterministic priority
   `article_date→period→region→population→indicator→unit`으로 질문한다.
4. `PERIOD_INVALID/PERIOD_UNKNOWN`이고 상대시점 표현과 article-date 부재가 함께
   확인될 때만 `article_date`를 질문한다. 절대시점이면 `period`를 질문한다.
5. 재입력은 기존 pending 기사 본문·제목·대화 context와 함께 전송한다. 서버는 answer
   role/value를 검증하고 article-date는 기존 date provenance에, 나머지는
   `user_clarifications`에 넣는다.
6. runtime은 routed row를 immutable copy한 뒤 해당 role만 보완한다. Claim Core에는
   `source=USER_CLARIFICATION`, question id, role, answer SHA-256을 남기고 article span으로
   가장하지 않는다.
7. 동일 파이프라인을 처음부터 한 번 재실행한다. 셀이 유일해지면 공식 통계 답변을,
   여전히 특정되지 않으면 다음 실제 누락 필드 질문 또는 명시적 검증 불가 답변을 낸다.

## 4. 필드 병합 규칙

- `article_date`: `YYYY-MM-DD` 실제 날짜만 허용하고 date_source=`user_feedback`.
- `period`: `YYYY`, `YYYY-MM`, `YYYY년 N월`, `YYYY-QN`의 bounded 문법만 정규화.
- `region/population/indicator/unit`: 공백 정규화 후 1~120자 자연어 값. 기존 explicit
  기사 span 값과 충돌하면 덮어쓰지 않고 `CLARIFICATION_CONFLICT`로 fail-closed.
- 사용자 답변은 검색 후보나 공식값에서 추정하지 않는다. 한 요청당 질문 1개,
  재질의 최대 3회는 frontend pending context의 `clarification_history` 길이로 제한한다.
- 답변은 주장 수치, 공식 셀 값, table ID를 채우는 데 사용할 수 없다.

## 5. 구현 범위

- `backend/app.py`: request schema와 날짜 선행 gate 제거.
- `backend/develop_verify_service.py`: pipeline-first 실행, ledger 기반 질문 투영,
  clarification 검증/기사 입력 결합.
- `frontend/src/api.js`, `frontend/src/ChatApp.jsx`: 범용 pending clarification 상태,
  FREE_TEXT/DATE/OPTIONS 입력과 원 기사 context 재전송.
- `src/news_verification/runtime/run_pipeline_operational_v2.py` 및 deploy mirror:
  user clarification을 routed row에 결합.
- `src/news_verification/runtime/r4c1_claim_core_v2.py` 및 deploy mirror:
  USER_CLARIFICATION provenance를 별도 evidence로 수용.
- `src/develop/failure_recovery_shadow_v1.py` 및 deploy mirror: bounded role/question 계약.
- `deploy/pipeline_runtime/manifest.json`: 변경 closure size/SHA 갱신.

## 6. 완료 기준

별도 전체 suite 대신 다음 종단 시나리오만 확인한다.

1. 동일 기사를 날짜 없이 보내면 L1/L2가 실제 실행된 뒤 article-date 질문이 반환된다.
2. `2026-06-24`를 같은 pending context의 `article_date` 답변으로 보내면 같은 기사가
   재실행된다.
3. L2=`L2_READY`, routed target>0, QUERY_READY, CELL_RESOLVED 및 최종 공식값 답변이
   생성된다.
4. 응답/로그에 raw secret·내부 예외·원본 세션 ID가 노출되지 않는다.

## 7. 비범위

동결 gold·채점 산식·001/002 migration·EC2 데이터 계층·색인/collection·alias·receipt를
변경하지 않는다. 사용자의 기사나 pending context를 DB/Redis에 저장하지 않는다.
이번 변경은 application request 동안 client가 보존하는 bounded clarification loop이며
공식값을 추정하는 fallback을 추가하지 않는다.
