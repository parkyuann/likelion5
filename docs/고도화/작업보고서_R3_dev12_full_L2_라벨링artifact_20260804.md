# R3 dev-12 전체 L2 사람 라벨링 artifact 작업보고서

- 작성일: 2026-08-04
- Task ID: `R3 / Task 1-1 Gate B`
- 상태 변경: `[ ] → [~]`
- 결론: 전체 문장 L2와 값별 3필드 gold 입력·검증·평가 차단 경로를 준비했으나 사람 판정이 문장 `0/256`, KOSIS 필드 `0/167`이므로 Gate B 및 Task 1-1은 완료가 아니다.

## 1. 범위와 고정 입력

현재 동결된 dev-12 기사와 현재 sentence offset map을 그대로 사용했다. 작업지침에는 255문장으로 적혀 있지만 실제 고정 입력은 12기사 256문장, value candidate 226건이다. 성능 결과에 맞추기 위해 한 문장을 제외하지 않았으며 이 차이를 contract의 `measurement_note`에 기록했다.

## 2. 구현 및 산출물

### 변경 파일

- `src/develop/build_full_l2_review_artifact.py`
  - dev-12의 모든 문장을 사람 검토 행으로 생성한다.
  - 결정론적 L1 value span을 문장별 경계 참조로 포함한다.
  - 모델 출력, 자동확정 행, 추천 gold를 포함하지 않는다.
  - 사람 입력 필드는 모두 빈 값으로 시작한다.
  - 기존 사람이 확정한 dev-12 routing 226건은 provenance와 함께 별도 보존한다.
  - KOSIS 후보 167건에 빈 `measurement_gold`·`period_gold` scaffold를 생성한다.
- `src/develop/l2_labeler.html`, `src/develop/l2_labeler_app.py`, `src/develop/l2_label_assembly.py`
  - 사람 표면에는 scope/region/span/sentence ID와 문자 offset을 표시하지 않는다.
  - 지표 상속은 사람이 읽는 지표 설명으로 선택하고 내부 ID는 코드가 연결한다.
  - 같은 문장의 공통 지표는 한 번 정의하고 여러 값 칩을 연결한다.
  - KOSIS 후보 값에만 측정유형과 기간 gold를 받는다.
  - KOSIS 값이 정확히 한 지표에 연결되지 않거나 필드가 비면 검토완료 저장을 거부한다.
- `src/develop/evaluate_r3_gate_b.py`
  - 256문장과 167개 KOSIS 필드가 모두 사람 확정된 뒤에만 value-level gold와 Gate B 지표를 생성한다.
  - 미완료 artifact는 `R3_NOT_READY`로 중단한다.
  - holdout routing F1이 없으면 dev-holdout gap을 통과로 간주하지 않는다.
- `src/develop/export_l2_review_workbook.py`
  - contract의 실제 문장 수에 맞춰 문맥 sheet 제목을 생성한다.
- `tests/test_build_full_l2_review_artifact.py`
  - 전 문장 보존, 사람 필드 미입력, 모델 출력 부재, ID 계약을 검증한다.

### 라벨링 산출물

- `data/develop/r3_full_l2_gold_20260804/dev12_l2_full_review.jsonl`
- `data/develop/r3_full_l2_gold_20260804/dev12_l2_full_context.jsonl`
- `data/develop/r3_full_l2_gold_20260804/dev12_l2_full_contract.json`
- `data/develop/r3_full_l2_gold_20260804/dev12_l2_full_review.xlsx`

계약은 다음을 고정한다.

- 기사: 12
- 검토 문장: 256
- 문맥 문장: 256
- value candidate: 226
- contract: `l2-full-dev12-v2`
- 사람 routing gold: KOSIS 167 / OUT_OF_SCOPE 37 / NOT_CLAIM 22
- 모델 출력 포함: false
- 자동확정 문장: 0
- 사람 검토 완료: 0/256
- KOSIS 3필드 gold 완료: 0/167
- `scope_id`와 `region_id`: 기사 단위로 기계가 유일성 검증
- span offset: 사람이 직접 입력하지 않고 `source_span_text`에서 resolver가 계산

## 3. 검증 결과

### 저장소 계약 검증

```text
progress = {total: 256, done: 0, remaining: 256}
validate.status = VALID
human_rows = 256
context_rows = 256
label_provenance_counts = {UNREVIEWED: 256}
kosis_field_gold_total = 167
kosis_field_gold_confirmed = 0
```

`VALID`는 파일·ID·참조 계약이 유효하다는 뜻이며 라벨링 완료를 뜻하지 않는다.

### 워크북 시각·구조 검증

artifact-tool로 xlsx를 import해 세 sheet를 inspect/render했다. workbook에는 내부 ID·JSON을 넣지 않고, 원문·값 후보·진행상태만 표시한다. 실제 판정 입력은 클릭 UI에서 수행한다.

- `검토입력`: `A1:H257`
- `기사별 문맥 256문장`: `A1:F257`
- `계약·가이드`: `A1:B17`
- formula error 검색: 0건
- 세 sheet PNG 육안 검토: 헤더, 줄바꿈, 입력 영역, 문맥 영역이 표시되며 치명적 잘림 없음
- artifact-tool render 파일 생성 후 프로세스가 메시지 없이 exit 1을 반환하는 런타임 현상이 있어, 최종 xlsx의 sheet명·한국어 cell 값·행/열 구조는 openpyxl read-only로 추가 확인했다. 전달 xlsx는 artifact-tool로 재저장하지 않았다.

### 로컬 UI 실화면 검증

- 측정유형 select와 기간 입력 동작 확인
- 미완성 KOSIS 값의 검토완료 저장 차단 확인
- 오류문은 span ID 대신 사람이 읽는 값(`2주`)을 표시
- 화면 전체에서 scope/region/span ID와 문자 offset 패턴 0건
- header는 기사 제목·사람 기준 문장 번호만 표시

### 회귀 테스트

```powershell
.\venv\Scripts\python.exe -m pytest tests -q --basetemp outputs\pytest_tmp\r3_full_l2_full_20260804
```

결과: `585 passed in 4.55s`

## 4. 사람 검토 실행

```powershell
.\venv\Scripts\python.exe -m src.develop.l2_labeler_app `
  --source data\develop\r3_full_l2_gold_20260804\dev12_l2_full_review.jsonl `
  --working data\develop\r3_full_l2_gold_20260804\dev12_l2_full_working.jsonl `
  --context data\develop\r3_full_l2_gold_20260804\dev12_l2_full_context.jsonl `
  --contract data\develop\r3_full_l2_gold_20260804\dev12_l2_full_contract.json `
  --port 8783
```

검토자는 source span 문자열과 분류값만 입력한다. offset과 기사 내 ID 참조 무결성은 코드가 검사한다.

## 5. 완료 게이트와 남은 의존성

현재는 artifact 생성 단계만 완료했다. 다음 조건 전에는 `[x]`로 승격하지 않는다.

1. 256문장 전부 사람 판정
2. 기사 내 `scope_id`·`region_id` 정의/참조 무결성 통과
3. 동일 질문 반복을 제거한 고유 평가축 확정
4. routing·field·측정치 세 Gate B 수치를 동일한 사람 gold에서 재측정
5. Precision/Recall/F1, 결측률, 오탐 유형 및 분자/분모 보고

따라서 현재 상태는 `[~]`이며, 이 artifact 자체를 성능 근거로 사용하지 않는다.

사람 판정 완료 뒤의 단일 평가 명령은 다음과 같다. 미완료 상태에서는 의도적으로 실패한다.

```powershell
.\venv\Scripts\python.exe -m src.develop.evaluate_r3_gate_b `
  --human-jsonl data\develop\r3_full_l2_gold_20260804\dev12_l2_full_working.jsonl `
  --context-jsonl data\develop\r3_full_l2_gold_20260804\dev12_l2_full_context.jsonl `
  --predictions data\develop\r2_l5_precision_20260804\dev12_routed_value_repeat.jsonl `
  --field-gold-output data\develop\r3_full_l2_gold_20260804\dev12_value_field_gold.jsonl `
  --output data\develop\r3_full_l2_gold_20260804\gate_b_metrics.json
```
