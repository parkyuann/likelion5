# EC2 단일 SHA 파이프라인 통합 설계 v1

- 기준일: 2026-08-29
- 설계 역할: gpt-5.6-sol / high
- 기준 commit: `origin/develop@5c33ac2d465efdb5917d317fbff5a212b1e68b46`
- 입력 adapter 원본: `refs/remotes/ec2/input-adapters@502d18695e35eae5499d70e371207d6046e7fa2c`
- 운영 데이터: 기존 PostgreSQL, OpenSearch, Qdrant, redis-session 및 EBS를 읽기 전용으로 유지한다.

## 1. 목표와 완료 정의

Frontend, API, URL/image input adapter, resumable clarification, release-bound runtime을 하나의 Git SHA로 묶는다. 기사 안의 모든 검증 가능한 수치 target은 기존 operational runtime의 per-target loop를 통해 `retrieval -> metadata binding -> QUERY_READY -> KOSIS cell -> deterministic comparison`으로 진행한다.

`completed`는 최소 한 target에서 공식 KOSIS cell을 확보한 경우에만 허용한다. 필드 보완이 가능하면 `needs_user_input`, 공식 cell이 하나도 없고 보완으로 해결할 수 없으면 `unverifiable`, 일부 target만 공식 cell을 확보하면 `completed_with_limits`를 반환한다.

## 2. Git 통합 경계

1. 최신 `origin/develop`에서 전용 통합 branch/worktree를 만든다.
2. EC2의 dirty worktree 파일은 복사하지 않는다.
3. 아래 commit의 기능 delta만 순서대로 이식한다.
   - `bb44560` URL/image adapter 연결
   - `d77ced2` input adapter receipt
   - `f4daf56` public URL handoff 문서
   - `502d186` HCX-005 OCR 전환
4. 충돌 시 `5c33ac2`의 resume/checkpoint/runtime 파일을 정본으로 유지하고 input adapter endpoint와 OCR 기능만 병합한다. `verification_checkpoint_store.py`, resume tests, annual/runtime 보완을 삭제하는 충돌 해결은 금지한다.
5. 현재 로컬의 `send_de` 변경 3개 파일은 동일 bytes를 통합 worktree에 적용하되 unrelated untracked 파일은 포함하지 않는다.

## 3. 허용 변경 파일

입력 adapter 통합:

- `.env.example`, `requirements.txt`
- `backend/app.py`, `backend/runtime_gate.py`
- `backend/url_article_service.py`, `backend/image_ocr_service.py`, `backend/hcx_ocr_client.py`
- `frontend/src/ChatApp.jsx`, `frontend/src/api.js`
- input adapter receipt/document와 해당 focused tests

파이프라인 계약:

- `backend/develop_verify_service.py`, `backend/verification_checkpoint_store.py`
- `deploy/pipeline_runtime/src/develop/run_article_body_pipeline_trace_v1.py`
- `deploy/pipeline_runtime/src/news_verification/runtime/release_bound_live_adapters_v1.py`
- `deploy/pipeline_runtime/src/news_verification/runtime/run_pipeline_operational_v2.py`
- 관련 `tests_backend/test_*` focused tests

배포 provenance:

- `deploy/compose.yaml`, `deploy/api.Dockerfile`, `deploy/nginx.Dockerfile`
- `backend/app.py`의 read-only version endpoint
- `deploy/release_manifest.py` 또는 동등한 deterministic manifest 도구
- `deploy/README.md` 또는 `DEPLOYMENT_INVENTORY.md`

동결 gold, 채점 산식, migration 001/002, 데이터 적재·색인·collection write, alias/current pointer는 수정하지 않는다.

## 4. Resume 계약

- request: optional opaque `resume_token`, `clarification_answers`, `date`, `date_source`.
- response `needs_user_input`: `resume_token`, `resume_from_stage`, bounded natural-language question.
- checkpoint: immutable article/L1/L2 bytes, runtime fingerprint, article hash, title, clarification history, expiry를 결박한다.
- article_date/period 보완은 `layers`, 그 밖의 cell selector 보완은 `live`부터 재개한다.
- resume 시 L1/HCX-L2 외부 호출은 0회여야 한다.
- token 누락·만료·fingerprint mismatch는 409 bounded code로 fail-closed 한다.
- frontend는 URL/text/image 경로 모두 token을 보존하고 다음 `verifyArticleDevelop()` body에 전송한다.

## 5. 다중 주장 실행 계약

기사 검증 경로에서는 `_deterministic_claim_query_from_routed()`로 대표 target을 고르지 않는다. `claim_query=None`을 전달하여 precomputed routed target 전체를 operational runtime에 보낸다. 기존 runtime의 stable routed order와 target ID를 보존하며 target마다 독립 retrieval, binding, cell receipt를 만든다.

명시적 사용자의 단일 통계 질문처럼 target 축소가 필요한 별도 경로에서만 `claim_query`를 허용한다. 기사 본문의 첫 LEVEL, 가장 큰 수치, 검색 점수로 target을 고르는 것은 금지한다.

각 target receipt 최소 필드:

- `article_idx`, `target_id`, `sentence_id`, `value_span_id`
- `measurement_type`, normalized indicator/period/unit/region
- retrieval channel별 호출수와 ordered candidate table keys
- metadata binding 호출수와 compatible table keys
- selected table key, `send_de`, release ID, profile SHA
- `QUERY_READY` 여부와 cell selector
- cell API 호출수, response SHA, official value/unit/period
- terminal status와 bounded limitation code

## 6. 최신 호환 표 선택

`statistics_table.send_de`를 canonical profile에 strict `YYYY-MM-DD`로 보존한다. 최신일은 indicator, item, period coverage, unit, region 및 cell selector가 의미상 호환된 후보 집합 안에서만 적용한다.

- unique latest `send_de`: 해당 표 선택
- latest date tie: 동률 subset 안에서 기존 deterministic specificity rule 적용
- missing/invalid date 또는 semantic selector 혼합: 선택하지 않고 fail-closed
- retrieval score가 높다는 이유로 최신성 또는 cell identity를 대체하지 않는다.

선택 receipt에는 모든 considered candidate의 `table_key`, `send_de`, semantic signature와 최종 결정 이유를 기록한다.

## 7. 응답 상태 계약

- `needs_user_input`: resolver가 채울 수 있는 필수 필드가 누락됨. token과 질문 필수.
- `unverifiable`: official cell 0건이며 추가 사용자 입력으로 해결할 수 없음.
- `completed_with_limits`: official cell 1건 이상이나 일부 target이 unsupported/hold.
- `completed`: 검증 가능 target 전체가 official cell evidence를 가짐.
- `structured_only`: live feature gate가 꺼진 명시적 개발 모드에만 허용.

HTTP 200이라는 이유만으로 `completed`를 쓰지 않는다. 각 결과 답변은 공식값, 단위, 기간, 전국/지역 기준, table key, `send_de`를 포함한다. 지원하지 않는 range/rank claim은 공식값이 있는 sibling claim과 분리해 limitation으로 표시한다.

## 8. 단일 SHA 배포 계약

- build context는 dirty worktree가 아니라 `/srv/news_verification/application-overlay/releases/<APP_RELEASE_SHA>` 같은 immutable checkout이다.
- API와 Nginx/frontend는 같은 `APP_RELEASE_SHA` build arg/OCI label을 가진다.
- API `/version`은 `release_sha`, runtime manifest SHA, release ID만 반환하고 secret/DSN은 반환하지 않는다.
- frontend build에는 같은 release SHA를 주입하며 `/version`과 불일치하면 UI가 검증 요청을 시작하지 않고 명시적 deployment mismatch를 표시한다.
- release manifest는 Git tracked files의 path, size, SHA-256과 image digest, Compose config paths를 기록한다.
- Compose에는 PostgreSQL, OpenSearch, Qdrant, redis-session을 새 서비스로 선언하지 않는다. BGE encoder는 기존 pinned internal-only 계약을 유지한다.
- 배포는 API와 Nginx만 recreate한다. 데이터 서비스 재시작, migration, write, reindex, re-embedding은 금지한다.

## 9. 집중 검증 게이트

구현 중 최소 검증:

1. input adapter URL/text/image route와 resume field 공존
2. resume 후 L1/L2 call count 0
3. 두 개 이상의 routed numeric target이 각각 live ledger row를 생성
4. cell call 0일 때 `completed`가 나오지 않음
5. 일부 성공 시 `completed_with_limits`
6. `send_de`가 coarser-but-older 후보보다 newer compatible 후보를 선택
7. missing/invalid `send_de` fail-closed
8. `/version` API/frontend SHA 계약
9. Compose에 데이터 계층 서비스가 없음
10. `git diff --check`, secret scan, manifest closure 일치

전체 suite 결과는 기존 baseline과 분리해 보고하며 기존 11개 sealed failure를 새 실패로 재분류하지 않는다.

## 10. EC2 E2E 완료 게이트

동일 release SHA의 API와 frontend를 배포한 뒤 대표 복합 출생 기사와 날짜 재질의 사례를 실행한다.

- 최초 응답에 `resume_token`과 정확한 `resume_from_stage`
- 재질의 요청에 같은 token 왕복
- 재개 receipt에서 L1/HCX-L2 추가 호출 0
- target별 retrieval 호출 `> 0`
- metadata binding `> 0`
- `QUERY_READY` 전 cell API 0, 이후 cell API `> 0`
- 공식값·기간·단위·table key·`send_de`가 포함된 answer 1건 이상
- unsupported range/rank는 별도 limitation
- API, frontend, container labels, release manifest SHA 일치
- PostgreSQL/OpenSearch/Qdrant/Redis/BGE의 container ID와 data count가 배포 전후 동일

하나라도 충족하지 못하면 develop promotion과 완료 선언을 하지 않는다. application image만 이전 digest로 rollback하며 데이터 계층은 rollback 대상으로 다루지 않는다.
