# 파이프라인 지연·재질의 최종 바이트 재승인 (20260829)

## 1. 결론

**APPROVED_WITH_LIMITS**

이전 최종 리뷰에서 남긴 유일 P0는 현재 바이트에서 닫혔다. 코드 수준 구현·회귀·closure 기준으로 EC2 E2E 진행을 승인한다. EC2 E2E를 시작하기 전 추가 blocker는 없다.

설계 문서의 승인 후 byte drift를 다시 확인했다. 변경은 EOF의 불필요한 빈 줄 1개 삭제뿐이며 `git diff --numstat`은 `0 insertions / 1 deletion`이다. 실행 계약·코드·runtime·테스트 byte에는 영향이 없으므로 기존 기술 판정은 유지한다. 현재 설계 SHA-256은 `2FCA272F05F95CC5EDF477BCB9644FAF488E0F8E4D005F938ADE36F4D35AE1D3`이다.

`WITH_LIMITS`는 실제 EC2 release-bound 서비스에서의 E2E 및 20회 성능 측정이 아직 이 리뷰 범위에서 수행되지 않았다는 뜻이다. 데이터 계층 변경, 재색인, 재임베딩, alias/current pointer 변경을 승인한다는 뜻은 아니다.

## 2. 검토 증거

- 설계: `deploy/DESIGN_PIPELINE_LATENCY_CLARIFICATION_20260829.md`, SHA-256 `2FCA272F05F95CC5EDF477BCB9644FAF488E0F8E4D005F938ADE36F4D35AE1D3`
- 설계 byte drift 범위: EOF 빈 줄 1개 삭제만 확인, `0 insertions / 1 deletion`; 의미·실행 byte 변경 없음
- 기준선: `origin/develop` 3a3d262, 136 tests 중 126 passed / 10 failed
- 현재 전체 결과: 158 tests 중 152 passed / 6 failed
- differential gate: 신규 실패 0, 기존 통과 감소 0
- focused: 23 passed
- runtime closure: 74/74
- runtime manifest SHA-256: `028706a2936ab0170159f8b14d55ed4b190525da33df8fcd5950a61a251df9df`
- frontend production build: 통과
- Compose config: 통과
- `git diff --check`: 통과
- Python compile: 통과

## 3. 이전 네 P0 최종 상태

| P0 | 최종 판정 | 근거 |
|---|---|---|
| target-scoped binding continuation | CLOSED | target scope, release, candidate membership, raw/profile/projection SHA를 checkpoint와 runtime에서 검증한다. 대상 target은 retrieval physical call 0, reranker transport 0, metadata profile transport 0으로 봉인 profile을 사용한다. scope 밖 target은 정상 retrieval을 수행한다. |
| speculative native timeout | CLOSED | capability 판정이 transparent wrapper의 `inner` chain을 leaf transport까지 재귀적으로 추적한다. unsupported leaf는 executor submit 전에 제외한다. 지원 채널 0 또는 profile 미지원이면 `DEADLINE_EXCEEDED`, executor task 0, inner call 0, Cell API 0, HCX answer 0으로 종료한다. native 지원 leaf에는 남은 `timeout_seconds`가 wrapper를 통과해 전달된다. production-like nested wrapper 회귀 테스트가 두 경로를 고정한다. |
| checkpoint answer binding | CLOSED | resume generation마다 신규 답변을 정확히 1개로 제한하며 pending question ID·role 불일치를 fail-closed 처리한다. 검증되지 않은 답변은 semantic constraint에 들어가지 않는다. |
| `allow_direct_input` contract | CLOSED | 직접입력 허용 시 option ID 없는 semantic answer를 받고, options-only이면 option ID를 강제한다. frontend와 backend 조건이 일치하며 조합별 테스트가 있다. |

## 4. speculative timeout 최종 재검토

`_native_speculative_timeout_supported()`는 wrapper 자신에게 메서드가 있다는 이유만으로 지원을 선언하지 않고 `inner` chain의 leaf가 실제 `speculative()`를 제공하는지 확인한다.

`_speculative_clarification_plan()`은 다음을 보장한다.

1. native timeout 지원 채널만 allowlist에 포함한다.
2. 제외 채널과 profile 지원 여부를 receipt에 기록한다.
3. 지원 채널이 없거나 profile이 미지원이면 executor 생성·submit 전에 `DEADLINE_EXCEEDED`로 반환한다.
4. 지원 경로에서는 매 호출 직전 남은 deadline을 계산해 `timeout_seconds`로 전달한다.
5. speculative 경로는 Cell API, comparator, HCX answer 권한을 갖지 않는다.

추가된 production-like 테스트는 다음 두 경계를 직접 고정한다.

- `CountingAdapter(UnsupportedChannel)` 및 `RequestScopedProfileProvider(UnsupportedProfile)`: executor task 0, inner call 0, `DEADLINE_EXCEEDED`, Cell/HCX 0.
- native 지원 inner를 같은 wrapper로 감싼 경우: 정상 option 생성 및 `0 < timeout_seconds <= remaining deadline` 전달.

따라서 이전 리뷰의 wrapper 표면 오판 가능성은 해소됐다.

## 5. EC2 E2E 승인 범위와 운영 게이트

EC2 E2E 전 blocker는 **없다**. 다음 단계는 application overlay의 API/Nginx/frontend/BGE query encoder만 동일 commit으로 반영해 읽기 전용 E2E를 수행하는 것이다.

E2E에서 확인할 운영 게이트는 다음과 같다.

1. 날짜 누락 Gate A: retrieval·Cell·HCX answer 호출 0.
2. indicator/item Gate A: native 지원 시 제한 시간 내 option 생성, 미지원 시 명시적 `DEADLINE_EXCEEDED`와 inner call 0.
3. Gate B option 응답 후 해당 target retrieval physical call 0.
4. `MULTIPLE_COMPATIBLE_SERIES` generic 502 0.
5. 기본 deterministic answer에서 HCX answer call 0.
6. checkpoint token/question/role/option 왕복과 재시도 안전성.
7. 20회 성능 측정에서 첫 질문·resume·날짜 포함 요청의 p50/p95 및 성공 분자/분모 기록.
8. PostgreSQL·OpenSearch·Qdrant·redis-session·EBS·index·collection의 ID와 데이터가 변경되지 않았는지 확인.

이 E2E가 실패하면 본 승인은 운영 배포 완료 승인으로 확대되지 않는다. 실패 원인을 application overlay에서 수정한 새 바이트는 새 SHA로 다시 검토해야 한다.
