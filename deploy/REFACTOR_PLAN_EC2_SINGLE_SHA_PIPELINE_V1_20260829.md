# EC2 단일 SHA 파이프라인 구현 누락 점검 및 최소 리팩터링 계획 v1

- 기준일: 2026-08-29
- 역할: 구현 누락 점검 및 리팩터링 계획. 독립 승인 아님.
- 설계 정본 후보: `deploy/DESIGN_EC2_SINGLE_SHA_PIPELINE_INTEGRATION_V1_20260829.md`
- 설계 SHA-256: `596A5821A812F428C2F3A91FC72F10D0688DFBDB1055DE0D6D4D791E84516269`
- 점검 worktree: `E:\news_verification_1\worktrees\ec2-single-sha-20260829`
- 점검 HEAD: `39ab8372372c25f26b0d55d5ea8bb8c4807634e2`
- 기준 `origin/develop`: `5c33ac2d465efdb5917d317fbff5a212b1e68b46`
- 종합 판정: **FAIL — 구현 방향은 대부분 반영됐지만 promotion 차단 결함 5건이 남아 있다.**

## 1. 점검 범위와 변경 경계

현재 worktree에는 commit된 URL/image adapter 변경과 미커밋 Luna 변경이 함께 있다.

미커밋 production 변경:

- `backend/app.py`
- `backend/develop_verify_service.py`
- `deploy/api.Dockerfile`
- `deploy/compose.yaml`
- `deploy/nginx.Dockerfile`
- `deploy/pipeline_runtime/src/news_verification/runtime/release_bound_live_adapters_v1.py`
- `deploy/pipeline_runtime/src/news_verification/runtime/run_pipeline_operational_v2.py`
- `frontend/src/api.js`

미커밋 test/tool 변경:

- `tests_backend/test_release_bound_dominance_v1.py`
- `deploy/release_manifest.py`
- `tests_backend/test_article_multi_target_v1.py`
- `tests_backend/test_release_manifest_v1.py`
- `tests_backend/test_release_version_v1.py`

이번 점검에서는 production code, commit, push, EC2 및 데이터 계층을 변경하지 않았다. 이 문서만 추가한다.

## 2. 설계 요구사항별 판정

| 요구사항 | 판정 | 근거 | 결론 |
|---|---|---|---|
| URL/text/image input adapter와 resume 공존 | **PASS(배선) / PENDING(실행 검증)** | URL은 `frontend/src/ChatApp.jsx:772-790`에서 canonical document를 공통 `verifyArticleDevelop()`로 넘기고 token을 보존한다. image는 `backend/app.py:343`에서 OCR 후 `verify_article_develop()`을 호출하고, `frontend/src/ChatApp.jsx:874-889`가 OCR text와 token을 다음 공통 verify 요청에 보존한다. | 구조는 공존한다. 다만 enabled URL/image → clarification → resume focused test가 없다. acquisition receipt가 checkpoint/live receipt까지 결박되는지도 증명되지 않았다. |
| article_date/period는 layers부터, 기타 필드는 live부터 재개 | **PASS(정적)** | `backend/develop_verify_service.py:744-756`이 checkpoint의 `resume_from_stage`만 실행한다. `backend/develop_verify_service.py:788-818`이 role에 따라 `layers|live`를 기록한다. `backend/verification_checkpoint_store.py:276-326`은 L1/L2/L3 artifact를 복사·재봉인한다. | L1/L2를 다시 호출하지 않는 구조다. 기존 resume focused test가 있으나 이번 환경에서는 실행하지 못했다. |
| resume checkpoint fingerprint/TTL/fail-closed | **PASS(정적)** | `backend/verification_checkpoint_store.py:332-357`은 body/runtime/config/title/history를 검사하고 consume 처리한다. TTL은 같은 파일 `15-16`, 만료 처리는 `268-272`다. | 설계에 부합한다. API container restart 시 checkpoint 소실은 파일 주석에 명시된 개발환경 한계다. |
| article `claim_query=None`으로 전체 target 실행 | **FAIL** | service는 `backend/develop_verify_service.py:765`에서 `claim_query=None`을 넘긴다. 그러나 actual runtime은 `run_pipeline_operational_v2.py:2422-2508`에서 evidence family가 하나이고 primary가 있으면 `routed = [dict(primary)]`로 다시 1건 축소한다. | 같은 family의 LEVEL/CHANGE sibling은 여전히 전체 실행되지 않는다. 현재 신규 test는 fake `run_trace`만 사용해 actual runtime 축소를 검출하지 못한다(`tests_backend/test_article_multi_target_v1.py:16-116`). |
| target별 release-bound retrieval → metadata → QUERY_READY → cell | **PASS(선택된 target) / FAIL(기사 전체)** | actual per-target loop는 `run_pipeline_operational_v2.py:2551` 이후 routed row별로 retrieval/binding/cell을 수행한다. `QUERY_READY` 및 `CELL_RESOLVED` 분기는 `2893-3073`에 있다. | 개별 target spine은 존재한다. 하지만 앞선 primary 축소로 기사 전체 target 계약은 충족하지 못한다. 실제 release-bound E2E receipt도 아직 없다. |
| 완료 status는 official cell evidence 기준 | **FAIL** | `backend/develop_verify_service.py:617-624`는 `cell.status == CELL_RESOLVED` 문자열만 센다. `545-612`의 receipt는 `response_sha256`, official value, release/profile/query-ready 결박을 필수 검증하지 않으며 `cell.calls`도 non-empty cell dict이면 1로 계산한다. 신규 test fixture도 response SHA/profile/release proof 없이 완료로 인정한다(`tests_backend/test_article_multi_target_v1.py:69-112`). | status가 공식 KOSIS cell receipt가 아니라 상태 문자열에 의존한다. 위조·불완전 ledger도 `completed`가 될 수 있다. |
| `completed`, `completed_with_limits`, `unverifiable` UI 구분 | **FAIL** | backend는 세 상태를 반환하지만 `frontend/src/ChatApp.jsx:705-724`가 `verified.status`를 사용하지 않는다. progress UI는 `frontend/src/ChatApp.jsx:361`에서 항상 `검증 완료`를 표시한다. | backend status 개선이 사용자 화면에 반영되지 않는다. |
| send_de precedence와 fail-closed | **FAIL(공개 receipt) / PASS(내부 로직) / PENDING(경계 테스트)** | metadata adapter는 `release_bound_live_adapters_v1.py:152-157,312`에서 release-pinned `send_de`를 strict ISO로 읽는다. dominance는 `run_pipeline_operational_v2.py:1567-1654`에서 item-specific 비교군 → 동일 semantic/geo selector → 최신 send_de → 최신일 동률 subset의 coarser-geo 순서로 처리하고, 결정 receipt를 `resolution.audit.release_bound_evidence_specificity_dominance`에 넣는다(`1664-1681`). 그러나 stage ledger 생성부 `3047-3085`는 이를 top-level `send_de`로 투영하지 않고, public target receipt는 `ledger.get("send_de")`만 읽는다(`backend/develop_verify_service.py:591`). | 내부 precedence는 존재하지만 설계가 요구한 target receipt의 selected `send_de`는 현재 `null`이 된다. semantic/geo selector mixed, 동일 최신일+coarser geo, non-nationwide 경계 test도 없다. |
| `/version`과 frontend 동일 SHA 실제 비교 | **FAIL** | API는 `backend/app.py:186-205`에서 SHA/manifest/release ID를 반환한다. frontend는 `frontend/src/api.js:43-56`에서 비교하지만 양쪽 중 하나가 `unknown`이면 통과한다. 검사는 `verifyArticleDevelop()`에만 있고 `analyzeImage()`에는 없다(`frontend/src/api.js:90,106-112`). | fail-closed가 아니며 image route는 backend 검증을 직접 실행하면서 SHA 확인을 우회한다. 현재 test는 API payload만 검사하고 browser/build SHA 비교를 검증하지 않는다. |
| API/Nginx/frontend 같은 build SHA | **PASS(배선) / PENDING(artifact 증명)** | `deploy/compose.yaml:3-18,95-101`, `deploy/api.Dockerfile:2-5`, `deploy/nginx.Dockerfile:2-13`이 같은 `APP_RELEASE_SHA`를 build arg/env/label로 전달한다. | 배선은 있다. 기본값 `unknown`을 허용하고 실제 image label/API/frontend bundle 삼자 비교 receipt가 없다. |
| release manifest closure | **FAIL** | `deploy/pipeline_runtime/manifest.json`은 변경되지 않았으나 runtime 실측에서 74개 중 9개 path의 size/SHA가 불일치했다. `deploy/release_manifest.py:24-72`는 `git ls-files`와 현재 working bytes를 기록할 뿐 전달된 SHA가 HEAD인지, worktree가 clean인지, runtime manifest가 닫혔는지 검증하지 않는다. | stale runtime closure와 임의 `app_release_sha`를 정상 manifest로 만들 수 있다. promotion 차단 결함이다. |
| Compose 데이터서비스 선언 금지 | **PASS** | `docker compose -f deploy/compose.yaml config --services` 실측 결과는 `bge-query-encoder`, `api`, `nginx`뿐이다. `deploy/compose.yaml:122-126`은 기존 external network와 internal encoder network만 선언한다. | PostgreSQL/OpenSearch/Qdrant/Redis 서비스 선언이 없다. BGE encoder는 설계상 허용된 내부 GPU 서비스다. |
| 단일 추적 가능 commit/release SHA | **PENDING** | 현재 HEAD는 `39ab837...`이고 production/test/tool 변경과 설계 문서가 미커밋 상태다. `APP_RELEASE_SHA`는 compose에서 기본 `unknown`이다. | 구현 완료·검증·단일 commit·image digest attestation 전에는 성립하지 않는다. |
| focused gate | **PENDING(환경 차단)** | 저장소 venv는 존재하지 않는 `C:\Users\user\...\Python313\python.exe`를 가리켜 pytest 시작 전 실패했다. bundled Python은 `pytest`가 없어 시작하지 못했다. `git diff --check`는 오류 없이 끝났다. | pytest 실패가 코드 실패라는 뜻은 아니다. 재현 가능한 Python 환경에서 다시 실행해야 한다. |

## 3. Promotion 차단 결함

### P0-1. 기사 전체 target이 actual runtime에서 보존되지 않는다

service의 `claim_query=None`만으로 충분하지 않다. actual operational runtime의 evidence-first primary selection이 같은 family를 1건으로 축소한다.

최소 수정:

1. `run_pipeline_operational_v2.py`에 기사 다중 target 모드를 명시적으로 전달한다. 새 boolean을 넓게 추가하기보다 기존 `claim_query is None`을 기사 전체 실행의 명확한 신호로 사용할 수 있다.
2. `claim_query is None`이면 primary selection은 evidence synthesis용 audit에만 남기고 `routed` membership을 변경하지 않는다.
3. 명시적 단일 통계 질문처럼 `claim_query`가 제공된 경로에서만 `select_query_target()`과 primary 축소를 허용한다.
4. actual `run_new_articles_v2()` 또는 가장 좁은 operational function을 사용한 focused test를 추가한다. fake `run_trace`만 검증하는 현재 test는 보조 test로만 남긴다.
5. 같은 evidence family 안의 LEVEL + CHANGE_RATE + CHANGE_POINT 3개가 모두 distinct ledger/receipt를 생성하는지 검증한다.

허용 파일:

- `deploy/pipeline_runtime/src/news_verification/runtime/run_pipeline_operational_v2.py`
- 대응 정본 source가 별도라면 그 source와 materialized closure
- `tests_backend/test_article_multi_target_v1.py`

### P0-2. 완료 status가 공식 cell receipt를 충분히 검증하지 않는다

`CELL_RESOLVED` 문자열 하나를 official evidence로 취급하면 안 된다.

최소 수정:

1. `_target_receipts()`에서 official-cell predicate를 단일 함수로 만든다.
2. 최소 필수 조건을 모두 요구한다.
   - resolution outcome `QUERY_READY`
   - non-empty selected `table_key`
   - expected canonical `release_id`
   - valid `profile_sha256`
   - cell status `CELL_RESOLVED`
   - non-empty official `DT`, unit, period
   - cell response SHA 또는 immutable cell receipt SHA
   - target과 cell selector의 identity 결박
3. `cell.calls`는 dict 존재 여부가 아니라 target call receipt/ledger로 계산한다.
4. 하나라도 빠지면 official count에 포함하지 않고 bounded limitation을 반환한다.
5. incomplete `CELL_RESOLVED` fixture가 `unverifiable`인 회귀 test와 1/2 evidence가 정확히 `completed_with_limits`가 되는 test를 추가한다.
6. dominance audit의 선택된 `send_de`를 stage ledger의 명시적 selected-table receipt로 투영하고, public target receipt가 이를 검증하여 노출하게 한다.

허용 파일:

- `backend/develop_verify_service.py`
- `tests_backend/test_article_multi_target_v1.py`

### P0-3. Runtime manifest closure가 이미 깨져 있다

현재 `deploy/pipeline_runtime/manifest.json` 실측은 74개 중 다음 9개가 불일치한다.

- `develop/annual_requery_shadow_v1.py`
- `develop/role_aware_dimension_shadow_v1.py`
- `develop/run_article_body_pipeline_trace_v1.py`
- `news_verification/runtime/l4_field_normalization.py`
- `news_verification/runtime/r4c1_binding_proposer_v1.py`
- `news_verification/runtime/r4c1_claim_core_v2.py`
- `news_verification/runtime/r4c1_projection_v2.py`
- `news_verification/runtime/release_bound_live_adapters_v1.py`
- `news_verification/runtime/run_pipeline_operational_v2.py`

최소 수정:

1. 정본 materialization source를 확인한다. deployment closure를 직접 손으로 고친 상태라면 정본 source로 먼저 반영한다.
2. 기존 materialization 도구로 runtime bundle과 manifest를 한 번만 재생성한다.
3. manifest의 모든 path/size/SHA를 실제 bytes와 전수 비교한다.
4. API image build 후 image 내부 `/app/pipeline_runtime/manifest.json`과 runtime bytes를 다시 비교한다.
5. closure 74/74가 일치하지 않으면 commit 및 image promotion을 금지한다.

허용 파일:

- 정본 source 및 materialization script가 생성하는 allowlist 파일
- `deploy/pipeline_runtime/manifest.json`
- closure focused test

### P0-4. 동일 SHA 비교가 fail-open이며 image 검증이 우회한다

최소 수정:

1. production build에서 frontend SHA 또는 server SHA가 `unknown`, 빈 값, non-40-hex이면 `DEPLOYMENT_VERSION_UNAVAILABLE`로 차단한다.
2. `checkReleaseVersion()`을 `verifyArticleDevelop()` 내부에만 두지 말고 실제 검증을 시작하는 공통 entry 또는 `analyzeImage()`에도 적용한다.
3. URL acquisition 자체는 read-only 준비 단계이므로 허용할 수 있지만, URL document의 verify 진입 전에는 반드시 검사를 통과해야 한다.
4. frontend SHA와 `/api/version.release_sha`, API image label, Nginx image label을 동일 값으로 검증하는 build/compose focused test를 추가한다.
5. `/version.runtime_manifest_sha256`가 actual runtime manifest bytes와 같은지도 검사한다.

허용 파일:

- `frontend/src/api.js`
- 필요한 경우 `frontend/src/ChatApp.jsx`
- `backend/app.py`
- `tests_backend/test_release_version_v1.py`
- frontend focused test 또는 deterministic build inspection test

### P0-5. Source release manifest가 commit을 증명하지 못한다

현재 도구는 호출자가 임의로 넣은 `app_release_sha`와 dirty tracked working bytes를 한 manifest에 넣을 수 있다.

최소 수정:

1. `app_release_sha`는 `git rev-parse HEAD`와 exact match인 40-hex만 허용한다.
2. tracked dirty와 staged diff가 있으면 생성 거부한다. untracked inclusion 정책은 명시하되 release artifact는 commit에 포함된 파일만 사용한다.
3. `git ls-files`의 현재 bytes가 HEAD blob bytes와 같은지 확인한다.
4. runtime closure verifier를 호출해 100% 일치한 경우만 release manifest를 만든다.
5. `api`, `nginx/frontend` 필수 image digest와 compose path가 없으면 생성 거부한다.
6. manifest 생성 후 self hash를 독립 재계산하는 test를 유지한다.

허용 파일:

- `deploy/release_manifest.py`
- `tests_backend/test_release_manifest_v1.py`
- 최종 external promotion receipt 경로

## 4. P1 보완

### P1-1. 사용자 화면의 상태 계약 반영

`showArticleResult()`가 backend top-level status를 읽어 다음처럼 표시해야 한다.

- `completed`: 검증 완료
- `completed_with_limits`: 일부 근거 확인 · 추가 확인 필요
- `unverifiable`: 공식 통계 근거를 확인하지 못함
- `structured_only`: 구조화 완료 · 공식 대조 미실행

`unverifiable`과 `structured_only`에 100% check 아이콘과 “검증 완료”를 표시하지 않는다.

허용 파일:

- `frontend/src/ChatApp.jsx`
- 직접 관련 frontend test

### P1-2. Input adapter + resume focused test 추가

현재 enabled URL/image 경로 test가 없다. 기존 테스트는 feature gate가 false일 때 `PIPELINE_*_PENDING`만 검사한다.

최소 사례:

1. URL metadata date가 있는 기사 → common verify 호출 필드 보존
2. URL metadata date가 없는 기사 → token 발급 → date 답변 → 같은 token으로 layers/live 재개
3. image OCR text → token 발급 → 공통 verify resume
4. image path에서도 release SHA mismatch/unknown 차단
5. raw URL/image/OCR secret이 checkpoint·응답 receipt에 없는지 검사

허용 파일:

- 직접 관련 `tests_backend/test_*`
- 필요한 최소 frontend test

### P1-3. send_de 경계 test 닫기

현재 unique latest와 missing/invalid만 검증한다. 다음을 추가한다.

- latest date tie → tied subset 안에서만 coarser geo
- semantic selector mixed → send_de `NOT_APPLIED`, HOLD 유지
- non-nationwide/geo disclosure 불충족 → fail-closed
- older coarser 후보가 newer compatible 후보를 이기지 못함
- receipt candidate 순서와 reason 결정성

production 로직 변경은 이 테스트가 실제 결함을 재현할 때만 허용한다.

## 5. 최소 리팩터링 순서

범위를 늘리지 않도록 아래 순서를 고정한다.

1. **실제 multi-target membership 수정 및 actual runtime test**
2. **official-cell predicate 강화 및 status test**
3. **frontend status 표시와 SHA fail-closed/image entry 적용**
4. **release manifest를 HEAD/clean/closure/digest에 결박**
5. **runtime closure 정본 재materialize 및 74/74 검증**
6. **URL/image + resume focused test 추가**
7. **send_de 경계 test 추가; 실패 재현 시에만 최소 로직 수정**
8. **focused suite 재실행 → frontend build → compose config → secret scan → diff check**
9. 모든 gate 통과 후에만 단일 commit 후보를 만들고, commit byte 기준 manifest/image를 다시 검증

이 순서에서 retrieval 알고리즘, encoder/reranker, DB schema, index/collection, 평가 산식은 변경하지 않는다.

## 6. 재실행할 focused gate

프로젝트의 정상 Python runtime을 복구하거나 검증된 interpreter를 제공한 뒤 다음만 실행한다. 전체 suite는 이 점검 범위에서 실행하지 않는다.

```powershell
E:\news_verification_1\venv\Scripts\python.exe -m pytest `
  tests_backend/test_resumable_clarification_annual_v1.py `
  tests_backend/test_operational_clarification_context_v1.py `
  tests_backend/test_article_multi_target_v1.py `
  tests_backend/test_release_bound_dominance_v1.py `
  tests_backend/test_release_version_v1.py `
  tests_backend/test_release_manifest_v1.py `
  tests_backend/test_ec2_integration_compose_v1.py `
  -q --basetemp=.pytest_tmp_ec2_single_sha_refactor
```

추가 gate:

- actual operational multi-target test에서 routed target 수 = ledger target 수
- resumed date 요청의 L1/HCX-L2 call delta = 0
- official cell proof 누락 fixture의 `completed` = 0건
- runtime manifest path/size/SHA = 74/74
- `docker compose config --services` = `api`, `nginx`, `bge-query-encoder`만
- frontend/API/image entry의 release SHA mismatch 및 unknown 모두 차단
- `git diff --check` PASS
- secret scan 0건

## 7. 이번 점검의 실행 증거

- `git diff --check`: PASS
- Compose config services: PASS — `bge-query-encoder`, `api`, `nginx`
- runtime manifest closure: FAIL — 74개 중 9개 mismatch
- focused pytest 1차: NOT RUN — repository venv launcher가 존재하지 않는 Python 경로를 참조
- focused pytest 2차: NOT RUN — bundled Python에 `pytest` 미설치
- 전체 suite: 요청에 따라 실행하지 않음

## 8. 완료 게이트

다음이 모두 충족될 때만 구현을 `READY_FOR_INDEPENDENT_APPROVAL`로 올릴 수 있다.

- actual runtime에서 기사 routed target 전부가 독립 ledger/receipt를 가짐
- official cell proof가 완전한 target만 완료 count에 포함됨
- frontend가 backend status를 그대로 구분해 표시함
- frontend/API/Nginx label SHA가 valid 40-hex로 exact match하며 unknown을 허용하지 않음
- runtime closure 74/74 일치
- release manifest가 HEAD, clean tracked bytes, runtime closure, 필수 image digest에 결박됨
- URL/image/text 모두 clarification token을 보존하고 resume call delta를 증명함
- send_de 경계 test 통과
- focused gate 전부 PASS
- 데이터서비스 변경 0건

현재 판정은 `NOT_READY_FOR_COMMIT_OR_PROMOTION`이다.
