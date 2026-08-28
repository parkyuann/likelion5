# 재질의 체크포인트 재개와 연간 통계 검증 설계 v1

## 1. 목표와 관측 원인

- 재질의 응답이 들어오면 L1부터 다시 호출하지 않고, 서버가 봉인한 중간 산출물에서 필요한 최소 단계만 재개한다.
- `ARTICLE_DATE_PROVENANCE_INVALID`는 내부 제한 코드로 완료하지 않고 `article_date` 질문으로 투영한다.
- 날짜가 보완된 연간 표현(`지난해`, `작년`, `올해`, `YYYY년`)을 월간 전용 `build_claim_core_monthly_v2h`에 넣지 않는다.
- 관측된 `PERIOD_INVALID`는 날짜 형식 오류가 아니라 monthly-v2h가 월 토큰만 허용한 데서 발생했다.
- 연간 단일값과 전년 비교는 기존 generic ClaimCore, release-bound 검색/Resolver, period-only cell requery를 재사용한다.
- 역사적 범위 주장은 지원되는 연산자만 결정론적으로 계산하고, 의미가 불명확하거나 필요한 series가 유일하지 않으면 자연어 재질의 또는 명시적 `not_evaluated`로 종료한다.

## 2. 체크포인트 계약

서버는 `needs_user_input` 직전에 무작위 256-bit opaque `resume_token`을 발급한다. 토큰으로 찾는 서버 레코드는 다음을 결박한다.

- canonical article text SHA-256, title, article id
- clarification history SHA-256
- runtime source fingerprint와 pipeline config SHA-256
- 재사용 가능한 마지막 단계와 생성 시각/만료 시각
- immutable 입력·단계 산출물과 mutable clarification context가 있는 서버 내부 경로

보호 조건:

- TTL 15분, 프로세스당 최대 32건, 만료·초과·완료 시 디렉터리를 삭제한다.
- 토큰과 기사 SHA가 다르거나 runtime/config가 바뀌면 fail-closed `RESUME_CHECKPOINT_*` 오류를 반환한다.
- 경로, secret, raw adapter 응답은 클라이언트에 반환하지 않는다.
- 토큰은 한 실행 흐름에서만 쓰며 최종 결과 또는 취소·만료 후 재사용하지 못한다.
- API 컨테이너 재시작 뒤 체크포인트가 사라지는 것은 현재 통합 개발 환경의 명시적 한계다. 외부 Redis session은 인증 전용이므로 pipeline checkpoint 저장에 섞지 않는다.

재개 위치:

| 보완 role | 재사용 | 재실행 |
|---|---|---|
| `article_date`, `period` | L1, L2 | layers, live |
| `region`, `population`, `indicator`, `unit` | L1, L2, layers | live |

`articles.jsonl`은 최초 본문·article id를 담은 immutable 입력으로 유지한다. 재질의 답변으로 이 파일을
교체하면 기존 L1/L2 manifest의 input SHA가 달라지므로 허용하지 않는다. 대신 별도
`clarification_context.json`을 원자적으로 작성하고, layers/live runner가 이 context를 읽어
article date와 사용자 보완 필드만 merge한다. 기존 단계 파일은 immutable input SHA, body SHA,
manifest SHA와 행 cardinality를 확인한 뒤에만 재사용하며, 재개 단계 receipt에는 context SHA를
추가한다.

## 3. API와 frontend

- `/api/v1/verify/develop` 요청에 optional `resume_token`을 추가한다.
- `needs_user_input` 응답에는 opaque `resume_token`과 `resume_from_stage`를 포함한다.
- frontend는 질문 메시지 상태에 토큰을 보관하고 답변 요청에 그대로 돌려준다.
- 재개 실패를 새 전체 실행으로 조용히 대체하지 않는다. 사용자가 원문을 다시 제출할 수 있는 안전 오류를 표시한다.

## 4. 기간 계약 선택

release-bound 실행은 원문 period evidence와 L4 structured period를 함께 보고 다음처럼 고른다.

- 월 토큰: 기존 `monthly-v2h` ClaimCore + monthly resolver
- 연 토큰: 기존 generic `build_claim_core_v2`의 article-date anchored normalization + generic resolver
- 분기/기타: 기존 지원 계약이 없으면 질문 또는 `PERIOD_UNSUPPORTED`; 월로 임의 변환하지 않는다.

`ARTICLE_DATE_PROVENANCE_INVALID`는 article date가 비어 있을 때만 날짜 질문이다. 날짜가 있는데 provenance/hash가 맞지 않으면 질문으로 숨기지 않고 fail-closed 한다.

## 5. 연간 값·전년 비교·범위 연산

1. 연간 LEVEL은 release-pinned query plan의 단일 연도 셀을 조회한다.
2. 연간 CHANGE_RATE/DIFFERENCE는 같은 table/item/dimension plan에서 현재 연도와 전년 셀만 바꿔 조회하고 Decimal로 계산한다.
3. `YYYY년 이후 ... 가장 높다`는 같은 series의 연도 범위를 조회해 연도별 전년 대비 변화율을 계산한다.
4. `N년 연속 반등`은 현재 연도를 포함한 N+1개 연간 셀의 엄격한 단조 증가를 확인한다.
5. `N년 만에 가장 큰 폭`은 `폭`이 rate인지 absolute difference인지 문장에서 유일하게 결정되지 않으면 값을 추정하지 않고 `measurement_basis`를 재질의한다.
6. 순위 표현(`역대 네 번째`)은 동률 순위 규칙이 명시되지 않으면 이번 v1에서 `not_evaluated:RANK_TIE_POLICY_PENDING`으로 둔다.

모든 range 조회는 검색·rerank·projection을 다시 하지 않는 period-only requery이며 release id, profile SHA, table/item/dimensions가 최초 plan과 같아야 한다. 기존 PostgreSQL/OpenSearch/Qdrant 데이터와 index/collection에는 쓰지 않는다.

## 6. 문장 분리와 사용자 답변

- 하나의 문장에 출생아 증가율과 합계출산율이 함께 있어도 indicator family가 다른 target은 별도 결과로 유지한다.
- 최종 표현은 단순 일치/불일치보다 `공식 통계는 ...`를 우선한다.
- 지원되지 않은 역사적 주장은 내부 코드만 노출하지 않고, 확인된 현재값·전년값과 확인하지 못한 범위를 구분한다.

## 7. 허용 변경 범위

- `backend/develop_verify_service.py`
- `backend/app.py`
- 신규 backend checkpoint helper 1개
- `frontend/src/api.js`, `frontend/src/ChatApp.jsx`
- release-bound runtime의 ClaimCore 선택 및 연간 period-only evidence helper
- `run_article_body_pipeline_trace_v1.py`의 immutable input / mutable clarification context 분리
- 관련 `tests_backend/**`

동결 gold, historical SHA receipt, migration, EC2 데이터 계층, index/collection, alias/current pointer는 수정하지 않는다.

## 8. 완료 조건

- 최초 요청이 article date 질문과 resume token을 반환한다.
- 답변 요청에서 L1/HCX-L2 호출 수가 0이고 필요한 단계부터 재개된다.
- 날짜 `2026-08-26` 보완 뒤 `PERIOD_INVALID`가 발생하지 않는다.
- 제공 문장에서 출생아 증가율과 합계출산율이 서로 다른 indicator 결과로 남는다.
- 가능한 연간 값/전년 비교는 실제 KOSIS 셀로 설명하고, 불명확한 `큰 폭`은 재질의 또는 명시적 제한으로 남긴다.
- 최종 결과에서 `ARTICLE_DATE_PROVENANCE_INVALID`, `PERIOD_INVALID` 같은 내부 코드를 사용자 문장으로 직접 노출하지 않는다.
