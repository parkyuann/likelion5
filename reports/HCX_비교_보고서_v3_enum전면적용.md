# HCX 모델 비교 실험 보고서 (v3 — enum 전면 적용 + 개선 지표)

> **안내**: v2가 지적한 두 축(Schema Valid Rate, claim_class Macro-F1)을 **전 모델에 enum을 적용**해
> 다시 측정한 버전이다. enum 적용 방식은 모델별로 다르다:
> - **프롬프트-only 모델(DASH-001/002, HCX-003, HCX-005)·HCX-007 SO off**: 허용 라벨을 **프롬프트에 명시**(soft 제약).
> - **HCX-007 SO on**: responseFormat 스키마에 **enum 강제**(hard 제약).
>
> 조건: pilot120(120건), temperature 0.0, top_p 0.8, HCX-007 thinking OFF 통일. **라벨은 silver**(사람 검수 gold 아님).
> 네트워크 오류로 케이스별 성공 건수(N)는 109~119로 약간 다르다.

---

## 1. 종합 결과표 (enum 적용)

| 케이스 | 성공/실패 | JSON 파싱% | **Schema Valid%** | **cls Macro-F1** | cls Weighted-F1 | **집계통계 Recall** | 주장탐지 F1 | 지연(초) | 토큰 | 건당(원) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1. DASH-001 (SOoff+enum) | 109/11 | 90.8 | 0.8 | **0.377** | 0.627 | 0.620 | 0.995 | 5.2 | 618 | 0.561 |
| 2. DASH-002 (SOoff+enum) | 119/1 | 99.2 | 0.0 | 0.243 | **0.648** | **0.798** | 0.982 | 2.9 | 489 | **0.171** |
| 3. HCX-003 (SOoff+enum) | 115/5 | 95.8 | 0.0 | 0.197 | 0.464 | 0.388 | 0.881 | 5.7 | 573 | 2.748 |
| 4. HCX-005 (SOoff+enum) | 117/3 | 97.5 | 0.8 | 0.000 | 0.000 | 0.000 | 0.182 | 3.1 | 529 | 1.036 |
| 5. HCX-007 (SO **on**+enum) | 117/3 | 97.5 | **97.5** | 0.295 | 0.331 | 0.179 | 0.962 | 3.6 | 951 | 1.844 |
| 6. HCX-007 (SO **off**+enum) | 119/1 | 99.2 | 0.8 | 0.286 | 0.583 | 0.583 | 0.933 | 4.0 | 649 | 1.650 |

---

## 2. 핵심 발견

### 2-1. Schema Valid Rate는 enum과 무관 — 구조는 오직 structured output이 보장

enum을 붙여도 **Schema Valid Rate는 SO on(97.5%)만 높고 프롬프트-only는 여전히 0~1%**다. enum은 **라벨(내용)** 을 유도할 뿐 **필수필드·구조(형식)** 를 강제하지 못하기 때문이다. 즉:
- **형식(스키마 준수)** → structured output이 유일한 해결책 (v2 결론 유지·강화)
- **라벨 정확도** → enum이 필요 (아래 2-2)

두 축은 **독립적인 레버**다.

### 2-2. enum으로 claim_class가 비로소 측정 가능해짐

enum 없이는 전부 Macro-F1 0.0이던 것이, enum 적용 후 **Macro-F1 0.20~0.38 / Weighted-F1 0.46~0.65**로 실측된다. 팀 피드백 #2(허용 클래스 명시 후 재실행)가 실제로 지표를 살렸다.

### 2-3. 🔴 반전 — 다수 클래스(집계통계)는 프롬프트-only가 오히려 우세, structured output이 분류를 악화

불균형 데이터(집계통계 다수)에서 팀이 중시한 **집계통계 Recall**·**Weighted-F1**을 보면:

| | 집계통계 Recall | Weighted-F1 |
|---|---:|---:|
| DASH-002 (SOoff+enum) | **0.798** | **0.648** |
| DASH-001 (SOoff+enum) | 0.620 | 0.627 |
| HCX-007 (SO **off**+enum) | 0.583 | 0.583 |
| HCX-007 (SO **on**+enum) | **0.179** | 0.331 |

**HCX-007은 structured output을 켜면 분류가 오히려 나빠진다.** 같은 모델·같은 enum·thinking OFF에서 **SO off는 집계통계 Recall 0.583인데 SO on은 0.179**로 급락한다. Confusion을 보면 원인이 명확하다:

- **HCX-007 SO on + enum** (정답 33/117): `집계통계 → 개별사례` **49건** 오분류, 집계통계 정답은 15건뿐.
- **HCX-007 SO off + enum** (정답 60/117): `집계통계 → 집계통계` **49건 정답**, 개별사례 오분류는 9건.

즉 structured output의 **형식 강제가 모델의 분류 판단을 경직**시켜 다수 클래스를 개별사례로 몰아버린다. 반면 프롬프트-only(특히 DASH 계열)는 라벨 가이드만 받고 자유롭게 판단해 **집계통계를 훨씬 잘 맞춘다.**

### 2-4. Macro-F1 vs Weighted-F1의 해석 차이

- **DASH-001**은 Macro-F1 최고(0.377) — 희소 클래스까지 고르게 잡음.
- **DASH-002**는 Macro-F1은 중간(0.243)이나 Weighted-F1(0.648)·집계Recall(0.798) 최고 — **운영 성능(다수 클래스)에 강함.**
- **HCX-007 SO on**은 Macro(0.295)는 중간이나 Weighted(0.331)가 낮음 — 다수 클래스에 약함.

운영 목적(집계통계 위주 검증)이면 **Weighted-F1·집계Recall 기준으로 DASH-002가 최적**이다.

### 2-5. HCX-005는 enum으로도 회복 불가

프롬프트에 라벨을 명시해도 HCX-005는 여전히 `claims` 배열로 감싸 스키마를 어겨, `claim_class`가 최상위에 없다 → Macro-F1·집계Recall·주장탐지 전부 0에 수렴. **enum은 라벨 문제만 다루지 구조 미준수는 못 고친다.**

---

## 3. 종합 판단 (어느 것도 전부를 이기지 못함)

| 목적 | 최적 케이스 | 근거 |
|---|---|---|
| **형식(스키마) 100% 보장** | HCX-007 SO **on** + enum | Schema Valid 97.5% (유일) |
| **claim_class 다수 클래스 분류** | **DASH-002** (SOoff+enum) | 집계Recall 0.80·Weighted-F1 0.65, 최저가(0.17원) |
| **희소 클래스까지 균형** | DASH-001 (SOoff+enum) | Macro-F1 0.38 |
| **주장탐지(is_claim)만** | DASH-001/DASH-002 | F1 0.98~0.99 |
| **사용 부적합** | HCX-005 | 스키마 붕괴로 전 지표 0 |

**핵심 시사점**: "구조화(형식)"와 "분류(라벨)"는 서로 다른 축이고, **한 모델·한 설정으로 둘 다 최적화되지 않는다.** HCX-007 SO on은 형식을 보장하지만 분류(특히 다수 클래스)를 희생하고, DASH-002 프롬프트-only는 분류·비용은 최고지만 형식을 못 지킨다. → 실전 파이프라인은 **형식 보장(structured output)과 분류 정확도(적합 모델/enum)를 분리 설계**하거나, HCX-007 SO on의 분류 열세를 프롬프트 개선으로 보완해야 한다.

---

## 4. 남은 한계 (팀 재설계안 대비, v2와 동일)

1. **라벨이 silver**(사람 검수 gold 아님) → 위 수치는 "silver 대비". 층화표본 gold로 재측정 필요.
2. **필드 추출**(indicator/population/단위 정규화)·**다중 수치(observations 튜플 F1·relation_type Macro-F1)** 미평가.
3. **KOSIS 매핑** 미포함.
4. 네트워크 오류로 N이 109~119로 불균일(재시도로 완화했으나 일부 잔여).

---

## 5. 결론

1. **형식 보장 = structured output 전용**: enum을 붙여도 프롬프트-only의 Schema Valid는 ~0%, SO on만 97.5%.
2. **분류 정확도 = enum 필수 + 모델 선택이 중요**: enum으로 Macro-F1을 살렸고, **다수 클래스(집계통계)는 DASH-002/DASH-001 프롬프트-only가 HCX-007 SO on보다 크게 우세**(집계Recall 0.80/0.62 vs 0.18).
3. **structured output이 분류를 악화**시키는 역설(HCX-007): SO on이 집계통계를 개별사례로 대량 오분류.
4. **비용까지 고려하면 DASH-002가 분류·효율 종합 우위**(건당 0.17원). 단 형식 보장이 필요하면 별도로 HCX-007 SO on을 조합해야 함.
5. 모든 수치는 **silver 기준 예비 결과** — gold·필드별·다중수치 평가로 재설계 시 확정.

---

*재계산 근거: 각 `output/hcx_experiments/<experiment_id>/predictions.csv`. Schema 검증은 CLAIM_SCHEMA(필수 9필드 + observations 6필드) 존재·타입·구조. claim_class는 silver gold 대비 Macro/Weighted-F1·집계통계 Recall·Confusion. enum 실행 파일: `hcx_claim_experiment_enum.py`(라벨 프롬프트, 007은 스키마 enum), `hcx_claim_experiment_enum_noso.py`(007 SO off+enum). v1/v2 보고서와 병존.*
