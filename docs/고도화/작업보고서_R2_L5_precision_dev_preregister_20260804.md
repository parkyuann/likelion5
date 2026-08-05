# 작업보고서 — R2 L5 precision dev 검증과 holdout-2 사전등록

작성일: 2026-08-04
Task ID: `R2`
상태: `[~]`

## 1. dev 가설 감사

작업지침은 “L4가 계산한 `measurement_type`을 L5가 읽지 않아 NOT_CLAIM 과라우팅이
생겼다”는 가설에서 시작했다. 같은 dev‑12 입력으로 현재 KOSIS 라우팅 행의 사람 gold를
교차 집계한 결과는 다음과 같다.

| measurement_type | KOSIS | OUT_OF_SCOPE | NOT_CLAIM | type 전체 차단 시 신규 차단 정밀도 |
|---|---:|---:|---:|---:|
| LEVEL | 100 | 34 | 17 | 51/151 = 0.338 |
| CHANGE_RATE | 42 | 1 | 0 | 1/43 = 0.023 |
| CHANGE_POINT | 6 | 1 | 0 | 1/7 = 0.143 |

어느 type도 lossless하지 않다. LEVEL 차단은 precision을 0.960으로 보이게 하지만 KOSIS
100건을 버려 recall이 0.886→0.287로 붕괴한다. 따라서 `measurement_type` 단독 규칙은
반증됐고 구현하지 않았다.

재현 산출물:
`data/develop/r2_l5_precision_20260804/dev12_measurement_signal.json`.

## 2. 채택한 L5 단일 변경

현재 라우팅 행 중 compact `value_text`가 compact indicator의 exact substring인 경우를
`VALUE_REPEATED_INSIDE_INDICATOR`로 차단한다. 이는 `소득 하위 20%`의 20%처럼 관측값이
아니라 분류 기준인 값을 가려내는 구조 신호다. 도메인 리터럴이나 `lexical_rules.py`
항목은 추가하지 않았다.

dev 결과:

| 지표 | 전 | 후 | 변화 |
|---|---:|---:|---:|
| TP | 148 | 148 | 0 |
| FP | 53 | 49 | -4 |
| FN | 19 | 19 | 0 |
| TN | 6 | 10 | +4 |
| Precision | 0.7363 | 0.7513 | +0.0150 |
| Recall | 0.8862 | 0.8862 | 0 |
| F1 | 0.8043 | 0.8132 | +0.0088 |
| 차단 정밀도 | 0.2400 | 0.3448 | +0.1048 |

신규 차단은 4/4 정당하고 KOSIS 손실은 0/4이지만 모수가 4다. 개선 크기는 독립
표본에서 측정되지 않았으므로 채택 성능으로 선언하지 않는다.

## 3. 후속 독립 stratum 계획

현재 clean reserve는 2기사뿐이다. holdout-2 소진 후 재개발이 필요할 때를 위해
다음을 사전 고정했다.

- 신규 사람 판정 60기사(LOW/MID/HIGH 각 20)
- accepted KOSIS source-shaped 최소 36기사
- 후속 dev 18 / sealed holdout-3 18
- 결과를 보고 strata 비율·라벨·표본을 바꾸지 않음

상세: `docs/고도화/실행계획_R2_후속stratum_20260804.md`.

## 4. holdout-2 preregistration과 현재 상태

전체 회귀 552개 통과 후 다음을
`data/develop/r2_l5_precision_20260804/holdout2_preregistration.json`에 고정했다.

- holdout input SHA-256:
  `73e7cbbd8cd569281af1f803bd537781ae5302e8c7ba343ef99889f1a3489cb7`
- L5 code SHA-256:
  `c2a8f2a2f00945bea064e132636a7a4b1758c6cf117e53372611488e2e9d6e36`
- threshold 변경 없음
- 단일 신규 reason `VALUE_REPEATED_INSIDE_INDICATOR`
- 보고 지표: P/R/F1, 차단 정밀도, 신규 차단 분자/분모, KOSIS 손실 수

Claude 코드 검토에서 이 규칙이 `confidence=1.0`인 신규 hard block이고, 기존의
“복구 불가능한 소수만 hard block·과차단을 더 엄격히 회피” 원칙과 긴장된다는 점을
확인했다. 이미 고정한 L5 코드를 지금 바꾸면 one-shot 계약이 무효가 되므로 코드는
변경하지 않고, prediction·사람 gold 완료 전에 채택 규칙을 preregistration에 추가했다.

- holdout에서 `kosis_loss_n > 0`: hard block을 채택하지 않는다. holdout에서 감점값을
  튜닝하지 않고, 새 개발 주기에서 routing threshold 아래의 soft penalty로 평가한다.
- `kosis_loss_n == 0`이어도 `new_block_total_n`이 안정적인 독립 추정에 너무 작으면
  잠정 결과로만 보고하며, 관측 손실 0만으로 `[x]` 처리하지 않는다.
- holdout threshold tuning은 허용하지 않는다.

그 뒤 모델 출력이 없는 사람 routing-gold scaffold를 생성했다.

```text
articles: 12
value candidates: 225
contains_model_output: false
human reviewed: 0/225
```

이 시점부터 holdout-2는 “미개봉”이 아니라 **사람 gold 준비를 시작한 측정 중 표본**이다.
본문과 판정행은 이 보고에서 열거하지 않았고 모델 prediction도 실행하지 않았지만,
다른 규칙 개발에는 더 이상 사용할 수 없다.

현재 환경에는 `NCP_CLOVASTUDIO_API_KEY`가 없어 L2 prediction을 생성할 수 없다.

## 5. 사람 gold 실행

```powershell
venv\Scripts\python.exe -m src.develop.routing_gold_app `
  --source data\develop\r2_l5_precision_20260804\holdout2_routing_scaffold.jsonl `
  --working data\develop\r2_l5_precision_20260804\holdout2_routing_working.jsonl `
  --port 8782
```

사람 판정 225/225 완료 후에만, API key가 있는 환경에서 고정 HCX 계약으로 L2를 한 번
실행하고 preregistered L5 코드로 채점한다. prediction을 사람에게 보여준 뒤 gold를
수정하지 않는다.

### 5.1 라벨링 부담 축소 (2026-08-05)

holdout-2 225값은 115문장으로 묶이며 group size는
`1값 54 / 2값 32 / 3값 14 / 4값 12 / 5값 1 / 6값 2`다. UI 진행률을
값 행만이 아니라 `문장 x/115 · 값 y/225`로 표시하고, 문장 일괄 판정은 값마다
파일을 다시 쓰지 않고 한 번만 저장하도록 바꿨다.

혼합 class 문장은 예외값을 먼저 개별 판정한 뒤 나머지에 문장 class를 적용한다.
`preserve_reviewed=true`가 이미 판정한 예외를 보존하므로 뒤의 문장 판정이 예외를
덮지 않는다. 비동기 저장 중 다른 문장으로 이동해도 응답이 현재 문장에서 추가
이동을 일으키지 않는다.

```text
집중 검증: 38 passed in 0.90s
전체 회귀: 619 passed in 3.33s
```

## 6. 변경 파일과 검증

| 파일 | 변경 |
|---|---|
| `src/develop/l5_routing.py` | exact value-in-indicator 구조 차단 |
| `src/develop/evaluate_l5_measurement_signal.py` | measurement type별 정답 손실·차단 정밀도 probe |
| `tests/test_l5_routing.py` | 분류값과 관측값 구분 계약 |
| `tests/test_evaluate_l5_measurement_signal.py` | 분자/분모와 loss 동시 보고 계약 |
| `docs/고도화/실행계획_R2_후속stratum_20260804.md` | holdout 소진 후 표본 계획 |

```text
검증 명령: venv\Scripts\python.exe -m pytest tests -q --basetemp outputs\pytest_tmp\r2_l5_full_20260804
검증 결과: 552 passed in 4.08s
```

## 7. 남은 이슈

- holdout-2 사람 routing gold 0/225
- HCX API key 부재로 L2 prediction 미생성
- holdout-2 one-shot P/R/F1·차단 정밀도 미측정
- dev 신규 차단 모수 4로 효과 크기 불확실
- 후속 stratum 60기사 사람 판정·36기사 동결 미실행

Claude 코드 검토 후 hard-block 채택 규칙을 사전등록에 추가한 상태의 전체 회귀는
`599 passed in 4.36s`다. L5 코드와 그 SHA-256은 변경하지 않았다.

```text
Task ID: R2
상태 변경: [ ] → [~]
변경 파일: 위 6절
검증 명령: 위 6절
검증 결과: dev 신규 정당차단 4/4, KOSIS 손실 0/4, 552 passed
산출물/보고서: data/develop/r2_l5_precision_20260804/*, 본 문서
남은 이슈: 사람 gold 225건, HCX key, one-shot holdout 측정, 신규 stratum 미확보
```

## 8. holdout-2 사람 gold 완료와 실행 전 감사 (2026-08-05)

사람 판정은 값 `225/225`, 문장 `115/115` 완료됐다. 작업 파일의 ID·순서는 동결
scaffold와 일치했고 알 수 없는 class·중복 ID·부분 판정은 0건이었다.

```text
KOSIS_CANDIDATE   73
OUT_OF_SCOPE     144
NOT_CLAIM          8
gold SHA-256       3d99c6f49654441232e4673683e463c5af953ce61d800fb55d2dc03212c54327
```

판정 메모는 최종 파일에서 영어 근거 225건이 모두 비어 있지 않다. 메모는 선택 audit
필드일 뿐 metric 입력에는 쓰지 않았다. prediction 전에 preregistration의 input·L4·L5
SHA-256 세 개가 모두 일치함을 다시 확인했다.

## 9. HCX structured-output 운영 호환성 실패와 수정

첫 실행은 예측 0건으로 끝났다. 12기사 모두 `CALL_FAILED`였고 11건은 HTTP 400, 1건은
180초 read timeout이었다. runner는 전체 완료 뒤에만 출력하므로 부분 prediction과
분류 지표는 생성되지 않았다.

holdout을 다시 보지 않고 dev split의 동일 L2 prompt·schema로 transport를 probe했다.

```text
maxCompletionTokens=8000 → HTTP 400 (0.8초)
maxCompletionTokens=4000 → HTTP 200 (44.3초)
```

따라서 `src/develop/hcx_client.py`의 transport 상한만 4000으로 낮췄다. L2 의미 prompt,
response schema, model, L4, L5, threshold, gold는 바꾸지 않았다. 실패 시도와 운영 재시도
허용 근거는 `holdout2_execution_addendum_20260805.json`에 prediction 전에 기록했다.

```text
집중 검증: 31 passed
전체 회귀: 620 passed in 3.46s
```

## 10. holdout-2 one-shot 결과

운영 재시도는 호출 실패 없이 끝났다.

```text
articles                  12
sentences_predicted       265
total_tokens            76526
model latency        424844 ms
CALL_FAILED                 0
MISSING_SENTENCES           5  (기사 1342: 1, 기사 2037: 4)
UNRESOLVED_SPANS            25
L3→L5 value assignments    225/225
```

누락 문장이나 span을 보고 재호출·수정하지 않았으며 그대로 하류 입력에 포함했다. L1의
동결 value candidate에서 L3가 225값을 모두 생성했으므로 routing evaluator의
`values_missing_from_prediction`은 0이다.

| 지표 | baseline | 신규 hard block | 변화 |
|---|---:|---:|---:|
| TP | 70 | 70 | 0 |
| FP | 144 | 142 | -2 |
| FN | 3 | 3 | 0 |
| TN | 8 | 10 | +2 |
| Precision | 0.3271 | 0.3302 | +0.0031 |
| Recall | 0.9589 | 0.9589 | 0 |
| F1 | 0.4878 | 0.4912 | +0.0034 |
| 차단 정밀도 | 0.7273 | 0.7692 | +0.0420 |

사전등록 필수 분자·분모:

```text
new_block_correct_n / new_block_total_n = 2 / 2
kosis_loss_n                             = 0 / 2
new block의 사람 class                   = OUT_OF_SCOPE 2
```

KOSIS 손실은 관측되지 않았지만 독립 신규 차단 모수가 2뿐이다. 더구나 가설이 겨냥한
`NOT_CLAIM`이 아니라 두 건 모두 `OUT_OF_SCOPE`였다. 따라서 사전등록 규칙대로 결과는
`PROVISIONAL_SMALL_N`이며 hard block을 안정적인 개선으로 채택하거나 R2를 `[x]`로
종료하지 않는다.

전체 precision 0.3302는 신규 규칙의 효과와 별개다. 남은 FP 142건은
`OUT_OF_SCOPE→KOSIS 134`, `NOT_CLAIM→KOSIS 8`로, 사전 분리했던 L2 region 문제와
NOT_CLAIM 미회수가 그대로 남았다. holdout-2 결과를 보고 이 표본에서 새 규칙이나
threshold를 조정하지 않는다.

재현 산출물:

- `holdout2_l2_predictions.jsonl`, `holdout2_l2_manifest.json`
- `holdout2_routed_value_repeat.jsonl`, `holdout2_summary.json`
- `holdout2_evaluation.json`, `holdout2_comparison.json`
- `holdout2_execution_addendum_20260805.json`

## 11. 상태 기록

```text
Task ID: R2
상태 변경: [~] 유지 — holdout-2 one-shot 측정 완료, hard block 효과는 n=2라 잠정
변경 파일: src/develop/hcx_client.py, src/develop/evaluate_r2_holdout.py,
           tests/test_hcx_client.py, tests/test_evaluate_r2_holdout.py, 본 보고서
검증 명령: venv\Scripts\python.exe -m pytest tests -q
검증 결과: 620 passed; holdout P/R/F1 0.3302/0.9589/0.4912,
           신규 정당차단 2/2, KOSIS 손실 0/2
산출물/보고서: data/develop/r2_l5_precision_20260804/holdout2_*, 본 보고서
남은 이슈: holdout-2 소진, 신규 차단 모수 2, 후속 60기사 판정·holdout-3 동결 미실행,
           L2 OUT_OF_SCOPE 과라우팅 134건은 이 표본에서 교정 금지
```
