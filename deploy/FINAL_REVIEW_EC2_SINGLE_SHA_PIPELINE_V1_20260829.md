# EC2 단일 SHA 파이프라인 통합 최종 리팩터링 검사 — 20260829

## 1. 검사 경계와 판정

- 검사 대상: `E:\news_verification_1\worktrees\ec2-single-sha-20260829` 현재 bytes
- 기준 설계: `deploy/DESIGN_EC2_SINGLE_SHA_PIPELINE_INTEGRATION_V1_20260829.md`
- 기준 리팩터링 계획: `deploy/REFACTOR_PLAN_EC2_SINGLE_SHA_PIPELINE_V1_20260829.md`
- 검사 HEAD: `39ab8372372c25f26b0d55d5ea8bb8c4807634e2` (변경은 아직 미커밋)
- 수행 범위: read-only code inspection, 최종 승인 후 최소 수정 2건, 전달된 focused/build/Compose/differential receipt 교차 확인, `git diff --check`
- 금지 범위 준수: production/test 수정, commit, push, EC2 변경 없음

**최종 판정: `APPROVE_IMPLEMENTATION_READY`**

핵심 파이프라인·응답·UI 계약과 release manifest의 clean-HEAD 경계가 설계대로 닫혔다. 최종 승인 후 추가된 API namespace/capability-order 수정과 static guard 계약 갱신도 기존 설계·승인 경계를 유지한다. 현재 bytes는 **코드·설계 구현 완료 및 단일 commit 후보**로 승인한다. 다만 아직 미커밋 상태이므로 실제 release manifest·image digest가 생성된 promotion artifact나 EC2 반영 완료를 승인하는 것은 아니다.

## 2. 요구사항별 최종 점검

| 요구사항 | 판정 | 현재 코드 근거 | 결론 |
|---|---|---|---|
| article multi-target actual runtime | **PASS** | `run_pipeline_operational_v2.py:2393-2401`은 routed evidence snapshot을 보존하고, `2412-2429`는 explicit query일 때만 target을 좁힌다. article mode인 `claim_query is None`은 `2444-2454`에서 snapshot 전체를 복원해 `FAMILY_TARGETS_PRESERVED`로 기록한다. `develop_verify_service.py:838-843`도 live 호출에 `claim_query=None`을 전달한다. actual operational focused test는 LEVEL/CHANGE_RATE/CHANGE_POINT 3개 ledger를 확인한다(`tests_backend/test_article_multi_target_v1.py:211-291`). | 대표 target 축소 결함은 제거됐다. `2491`의 primary 축소는 explicit-query 분기 안에만 남아 article mode를 침범하지 않는다. |
| official cell proof 기반 status | **PASS** | `develop_verify_service.py:571-615`가 QUERY_READY, selected table, expected release ID, profile SHA, strict send_de, full selector, selector SHA, target ID, 실제 cell call ledger, CELL_RESOLVED, response SHA, query identity, DT, unit을 모두 요구한다. `618-689`는 이 predicate로 공개 receipt를 만들고, `692-700`은 official evidence 수로만 `completed`/`completed_with_limits`를 계산한다. | cell object 존재만으로 완료되던 결함과 `cell.calls` 추정 결함이 제거됐다. |
| selected `send_de` receipt | **PASS** | operational ledger는 `run_pipeline_operational_v2.py:3082-3088`의 `selected_table`에 table key, strict `send_de`, release ID, profile SHA, query-plan SHA를 함께 기록한다. public receipt는 `develop_verify_service.py:641-667`에서 이 구조를 읽는다. 내부 dominance receipt와 strict parsing은 `release_bound_live_adapters_v1.py:73-87,152-157,312`, `run_pipeline_operational_v2.py:1492-1681`에 유지된다. | 선택값과 선택 근거가 stage/public receipt에 연결됐다. |
| 모든 frontend 검증 진입 경로 release gate | **PASS** | `frontend/src/api.js:44-59`는 frontend/server SHA가 valid 40-hex가 아니거나 불일치하면 fail-closed한다. text/URL의 공통 검증 진입점 `verifyArticleDevelop()`은 `93`, image 검증 진입점 `analyzeImage()`는 `110`에서 먼저 gate를 호출한다. URL 본문 획득 후에도 ChatApp은 공통 verify로 진입한다(`ChatApp.jsx:774-790`). | URL 획득 자체는 검증이 아니며, 실제 검증 요청은 모두 gate 뒤에 있다. |
| status UI | **PASS** | `ChatApp.jsx:348-357`이 `completed`, `completed_with_limits`, `unverifiable`, `structured_only`를 구별하고, `713-733`이 backend top-level status를 progress bubble에 전달한다. | 일부 성공·검증 불가·구조화 전용을 모두 “검증 완료”로 표시하던 결함이 제거됐다. |
| runtime closure | **PASS** | 현재 `deploy/pipeline_runtime/manifest.json` SHA-256은 `94A14D5B03F84021CF25211AC9434FCA90162053574ED95CE9FF5D1E72EEF99C`다. 전달 receipt는 74/74 valid이며 verifier도 fixed 74 allowlist, tracked membership, size/SHA를 검사한다(`deploy/release_manifest.py:90-123`). | runtime source closure 자체는 닫혔다. |
| APP_RELEASE_SHA required / same build SHA | **PASS** | Compose는 `${APP_RELEASE_SHA:?set the exact 40-character Git SHA}`를 API·Nginx build/runtime/label에 강제한다(`deploy/compose.yaml:7,18,20,101,103`). 두 Dockerfile도 40 lowercase hex가 아니면 build를 중단한다(`deploy/api.Dockerfile:2-6`, `deploy/nginx.Dockerfile:2-4,12-14`). | `unknown` 기본값으로 배포되는 경로는 제거됐다. |
| Compose application overlay | **PASS** | 전달된 resolved service 목록은 `bge-query-encoder`, `api`, `nginx`이며 PostgreSQL/OpenSearch/Qdrant/Redis 서비스 선언이 없다. `kosis_shadow_internal`은 external network다. | 기존 EC2 데이터 계층을 생성·변경하지 않는다. |
| release manifest clean HEAD + image digests | **PASS** | `deploy/release_manifest.py:62-71`은 HEAD SHA 일치 후 `git status --porcelain --untracked-files=all`을 실행한다. untracked가 하나라도 있으면 `UNTRACKED_WORKTREE_CONTENT`, tracked/staged 변경이면 `TRACKED_WORKTREE_DIRTY`로 거부한다. `72-79`는 tracked bytes와 HEAD blob 일치를 재확인하고, `152-177`은 api/nginx digest와 Compose path 및 74/74 runtime closure를 요구한다. `tests_backend/test_release_manifest_v1.py:64-81`은 non-HEAD, tracked dirty, untracked build input, digest 누락을 모두 fail-closed로 확인한다. | 저장소 전체 Docker build context에 unsealed 파일이 섞인 상태에서는 manifest를 생성할 수 없다. 유일 blocker가 해소됐다. |

## 3. 이전 promotion blocker 해소 확인

### `RELEASE_MANIFEST_UNTRACKED_CONTEXT_NOT_CLOSED`

**상태: RESOLVED**

1. `_assert_clean_head()`가 `--untracked-files=all`로 모든 untracked 경로를 검사한다.
2. untracked가 있으면 `UNTRACKED_WORKTREE_CONTENT`로 즉시 중단한다.
3. 회귀 test가 untracked Python build input을 만든 뒤 manifest 생성 거부를 확인한다.
4. tracked dirty와 non-HEAD SHA 및 필수 image digest 누락 거부도 유지된다.

추가 production 리팩터링은 필요하지 않다.

## 4. 검증 receipt 해석

- focused tests: **27 passed** — Terra 전달 receipt이며 이번 bounded review에서 재실행하지 않았다.
- blocker focused tests: **3 passed** — 최종 수정 후 전달 receipt이며 이번 재검사에서 코드를 교차 확인했다.
- tests_backend differential: origin/develop **10 failed / 104 passed**, 통합 bytes **10 failed / 118 passed** — 전달 receipt 기준 실패 집합 동일, 신규 실패 **0**, 기존 통과 감소 **0**, 통과 **14 증가**. 이번 bounded review에서 전체 differential을 재실행하지 않았다.
- frontend production build: **PASS** — 전달 receipt.
- Compose services: **PASS**, `bge-query-encoder / api / nginx` — 전달 receipt 및 현재 Compose와 일치.
- runtime closure: **74/74 valid**, manifest SHA-256 `94A14D5B03F84021CF25211AC9434FCA90162053574ED95CE9FF5D1E72EEF99C` — 전달 receipt와 현재 파일 SHA 일치.
- `git diff --check`: **PASS** — 이번 검사에서 재확인. 출력된 LF/CRLF 메시지는 whitespace error가 아니라 Git 변환 경고다.
- full suite: 실행하지 않음(이번 bounded review 범위 밖).

## 5. 구현 승인과 실제 promotion의 경계

### 승인한 것

- 현재 코드와 설계의 완성
- 현재 변경 전체를 단일 추적 가능한 commit 후보로 봉인하는 것
- commit 후 immutable checkout에서 promotion artifact 생성 단계로 진행하는 것

### 아직 승인하지 않은 것

- 아직 존재하지 않는 최종 commit SHA를 release SHA로 주장하는 것
- commit 전 dirty worktree에서 release manifest나 image를 생성하는 것
- api/nginx 실제 image digest 및 manifest receipt 확인 전 develop/EC2 promotion 완료를 선언하는 것
- commit, push, EC2 변경 자체(이번 재검사 범위 밖)

따라서 현재 상태는 **promotion candidate 준비 완료**이며 **promotion 완료 상태는 아니다**. 동일 bytes를 commit한 뒤 clean immutable checkout에서 74/74 closure, 필수 image digest, Compose path를 포함한 release manifest를 생성·확인해야 실제 promotion gate가 닫힌다.

## 6. 최종 승인 후 최소 수정 2건 재검사

### 6.1 API version namespace 및 image capability gate 순서

**판정: PASS**

- `backend/app.py:203`에는 `@app.get("/api/version")`만 존재하고 비-namespaced `@app.get("/version")`은 없다.
- frontend의 `apiFetch("/version")`는 `VITE_API_BASE_URL` 기본 `/api`와 결합되어 `/api/version`을 호출한다(`frontend/src/api.js:1,44-45`). Nginx도 `/api/` prefix를 제거하지 않고 API로 전달한다(`deploy/nginx.conf:17-18`).
- image 경로는 `backend/app.py:344-345`에서 `require_pipeline_image()`를 먼저 평가하고, capability가 허용된 경우에만 공통 `require_pipeline_runtime()`으로 진행한다. 따라서 image 기능 비활성은 generic runtime 오류에 가려지지 않으면서 두 gate 모두 fail-closed한다.
- article verify와 일반 analyze 경로의 runtime guard는 유지된다(`backend/app.py:296,322,332`). URL adapter도 runtime guard 뒤에 URL capability를 검사한다(`322-323`).

이 변경은 새 외부 경로를 추가하거나 인증·검색·데이터 계층 계약을 넓히지 않는다. 오히려 application overlay의 `/api` namespace와 capability-specific 오류 경계를 명확히 한다.

### 6.2 Overlay static runtime guard 계약 갱신

**판정: PASS**

- 현재 `backend/app.py`의 직접 `require_pipeline_runtime()` 호출은 정확히 4개다: article verify 1개, URL analyze branch 1개, 일반 article analyze 1개, image 1개.
- `tests_backend/test_overlay_contract_static.py:57-60`은 input adapter 추가에 따른 계약 설명과 기대 개수 4개를 동일하게 반영한다.
- 같은 test는 모든 route가 `/health` 또는 `/api/` namespace인지 계속 AST로 검사하고, runtime dependency가 FastAPI dependency injection으로 우회되지 않는지도 유지한다(`54-64`).

이는 production 동작을 느슨하게 만드는 테스트 변경이 아니라, 이미 승인된 URL/image input adapter로 늘어난 fail-closed guard 호출 지점을 현재 구조에 맞춰 추적하는 정적 계약 갱신이다.

### 6.3 재검사 결론

두 수정에서 신규 promotion blocker, 범위 확장, 기존 데이터 계층 변경, fake fallback 또는 release gate 약화는 발견되지 않았다. 전달된 differential도 기존 실패 집합을 그대로 유지하면서 신규 실패 0건을 보인다. 따라서 최종 판정은 **`APPROVE_IMPLEMENTATION_READY` 유지**다.
