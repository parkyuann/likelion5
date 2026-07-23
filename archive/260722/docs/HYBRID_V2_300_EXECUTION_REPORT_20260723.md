# KOSIS Catalog v2 Claim 300건 하이브리드 검색 실행 결과 보고서

- 보고서 작성일: 2026-07-23
- 실제 실행일: 2026-07-23
- 계획 최초 작성일: 2026-07-22
- Claim 원본: `data/claims_v1.jsonl`
- Catalog: `data/kosis_catalog_v2.jsonl`
- 코드: `archive/260722/src/v2/run_hybrid_v2_300.py`
- 결과: `archive/260722/outputs/hybrid_v2_300_20260722`

## 1. 실행 목적

`claims_v1.jsonl`에서 300건을 고정 시드로 샘플링하고, KOSIS Catalog v2 515건을 대상으로 네 경로 하이브리드 검색을 실행했다. 최종 결과는 Claim당 한 행의 JSONL로 저장했다.

- B2 형태소 BM25 → `doc_meta_text`
- B4 핵심 성분 BM25 → `doc_meta_text`
- Claim Dense → `doc_meta_vector`
- HyDE Dense → HCX 예상 표명 생성 후 `tbl_name_vector`
- RRF `k=60` → 최종 Top-20

## 2. 기존 방식과 달라진 점

| 구분 | 기존 v1 100건 실행 | 이번 v2 300건 실행 |
|---|---:|---:|
| Claim 수 | 100 | 300 |
| Catalog | v1 | v2 |
| BM25 문서 | `doc_item_index` | `doc_meta_text` |
| BM25 검색 범위 | 265,094 | 515 |
| Dense 검색 범위 | 1,000 | 515 |
| 범위 일치 | 불일치 | 네 경로 모두 515건 |
| Claim Dense | `doc_meta_vector` | 기존 방식 유지 |
| HyDE Dense | `tbl_name_vector` | 기존 방식 유지 |
| 결과 형식 | JSONL | 최종·디버그 JSONL 분리 |

BM25는 이번 실험에서 표 내부 항목인 `doc_item_index`를 사용하지 않고, 표명·분류 경로·차원이 포함된 `doc_meta_text`만 사용했다.

## 3. 샘플링 결과

| 항목 | 결과 |
|---|---:|
| `claims_v1.jsonl` 전체 행 | 19,415 |
| 샘플 크기 | 300 |
| 샘플 시드 | 20260722 |
| 고유 `claim_id` | 300 |
| 기존 B1~B4 300건 샘플과 일치 | 예 |

Reservoir sampling을 사용했으며 동일한 원본과 시드를 사용하면 같은 300건이 선택된다.

## 4. 실행 명령

```powershell
.\.venv\Scripts\python.exe archive\260722\src\v2\run_hybrid_v2_300.py `
  --claims data\claims_v1.jsonl `
  --sample-size 300 `
  --seed 20260722 `
  --per-path-n 20 `
  --top-n 20
```

## 5. 실행 결과 요약

| 항목 | 결과 |
|---|---:|
| 처리 Claim | 300 |
| Catalog BM25 문서 | 515 |
| Qdrant 포인트 | 515 |
| 경로별 최대 후보 | 20 |
| 최종 후보 | Claim당 20 |
| 전체 최종 후보 행 | 6,000 |
| 오류 Claim | 0 |
| HCX 성공 | 300/300 |
| 고유 HCX 예상 표명 | 289 |
| 실행 시간 | 646.685초(약 10분 47초) |
| Claim당 평균 | 2,155.617ms |

## 6. 외부 API와 캐시

| 항목 | 결과 |
|---|---:|
| HCX 캐시 적중 | 100 |
| HCX 신규 호출 | 200 |
| 임베딩 캐시 적중 | 221 |
| 임베딩 신규 호출 | 379 |

기존 100건 HCX 결과와 기존 질의 임베딩 캐시를 재사용했다. BM25 후보 및 Qdrant 후보는 HCX에 전송하지 않았다.

## 7. 경로별 후보 수

| 경로 | 평균 | 중앙값 | 최소 | 최대 | 빈 결과 Claim |
|---|---:|---:|---:|---:|---:|
| B2 doc_meta BM25 | 17.14 | 20 | 0 | 20 | 26 |
| B4 doc_meta BM25 | 12.97 | 20 | 0 | 20 | 65 |
| Claim Dense | 20.00 | 20 | 20 | 20 | 0 |
| HyDE Dense | 20.00 | 20 | 20 | 20 | 0 |

`doc_meta_text` BM25는 Claim의 형태소가 표명·분류·차원에 정확히 존재할 때만 점수를 얻는다. B2에서 26건, 더 선택적인 B4에서 65건이 빈 결과였으며, 이는 Dense 경로가 후보 회수 안전망으로 필요하다는 근거다.

## 8. 경로 간 평균 후보 중복률

| 경로 쌍 | 평균 Jaccard |
|---|---:|
| B2 / B4 | 0.603283 |
| B2 / Claim Dense | 0.080183 |
| B2 / HyDE Dense | 0.040749 |
| B4 / Claim Dense | 0.062531 |
| B4 / HyDE Dense | 0.036268 |
| Claim Dense / HyDE Dense | 0.135781 |

B2와 B4는 동일한 `doc_meta_text` 인덱스를 검색하므로 중복이 높다. 반면 BM25와 Dense 경로의 중복은 낮아 서로 다른 후보를 공급한다. Claim Dense와 HyDE Dense도 동일한 임베딩 모델을 사용하지만 검색 질의와 대상 벡터가 달라 평균 Jaccard가 0.135781에 그쳤다.

## 9. 최종 후보의 지원 경로 수

| 지원 경로 수 | 후보 수 | 6,000개 중 비율 |
|---:|---:|---:|
| 1개 | 1,764 | 29.40% |
| 2개 | 3,520 | 58.67% |
| 3개 | 540 | 9.00% |
| 4개 | 176 | 2.93% |

최종 후보의 70.60%는 두 개 이상의 경로에서 검색됐다. 네 경로 모두가 지지한 후보는 176개였다.

## 10. 후보 다양성

| 항목 | 결과 |
|---|---:|
| Claim당 네 경로 합집합 평균 | 51.81 |
| 합집합 중앙값 | 54 |
| 합집합 최소/최대 | 26 / 78 |
| 최종 결과에 한 번 이상 등장한 표 | 475/515 |
| v2 Catalog 최종 후보 커버리지 | 92.233% |

경로별로 한 번 이상 검색된 고유 표 수는 B2 424개, B4 435개, Claim Dense 479개, HyDE Dense 420개였다.

## 11. 반복적으로 1위가 된 표

가장 자주 최종 1위가 된 표는 다음과 같다.

| table_key | 표명 | 1위 Claim 수 |
|---|---|---:|
| `101:DT_101094B005` | 성별, 기업규모별 혼인·출산 변화 비율(1년 단위) | 14 |
| `101:DT_101094B004` | 성별, 소득수준별 혼인·출산 변화 비율(1년 단위) | 11 |
| `110:TX_11025_A000_A` | 시도별 외국인주민 현황 | 11 |
| `101:DT_1BPA002` | 주요 인구지표 / 전국 | 10 |
| `358:DT_358005_008` | 고령친화 용품 수출액 | 9 |

반복 1위는 일부 v2 표가 다양한 Claim에 일반적으로 높은 유사도를 얻는 허브 후보일 가능성을 보여준다. 골드가 없으므로 이 현상이 정답 적중인지 과다 노출인지는 판단할 수 없다.

## 12. JSONL 검증

| 파일 | 행 수 | 결과 |
|---|---:|---|
| `sample_claims_300.jsonl` | 300 | 통과 |
| `hybrid_top20_300.jsonl` | 300 | 통과 |
| `path_debug_300.jsonl` | 300 | 통과 |
| `hyde_predictions_300.jsonl` | 300 | 통과 |
| `query_embedding_cache.jsonl` | 575 | 통과 |
| `errors.jsonl` | 0 | 오류 없음 |

추가 검증 결과:

- 최종 결과 고유 Claim 300개
- 모든 Claim의 최종 후보 20개
- 모든 최종 순위 1~20 연속
- Claim 내부 중복 `table_key` 0건
- 모든 결과의 `catalog_version=kosis-catalog-v2`
- 모든 결과의 `run_date=2026-07-23`
- 전 파일 JSONL 파싱 성공
- 유니코드 대체문자 0개

## 13. Recall 미계산 사유

샘플 Claim에 대응하는 골드 `table_key`가 제공되지 않았으므로 Recall@5·10·20은 계산하지 않았다. 이번 결과는 실행 안정성, 후보 다양성, 경로 중복률을 보여주지만 검색 정확도를 의미하지 않는다.

또한 v2 Catalog가 515건에 불과하므로 상당수 Claim의 실제 정답 표가 후보군에 존재하지 않을 수 있다. 정답 표가 Catalog에 없으면 검색기나 RRF가 해당 표를 선택할 수 없다.

## 14. 산출물

### 코드 및 문서

- `archive/260722/src/v2/run_hybrid_v2_300.py`
- `archive/260722/docs/HYBRID_V2_300_EXECUTION_PLAN_20260722.md`
- `archive/260722/docs/HYBRID_V2_300_EXECUTION_REPORT_20260723.md`

### JSONL 및 요약

- `archive/260722/outputs/hybrid_v2_300_20260722/sample_claims_300.jsonl`
- `archive/260722/outputs/hybrid_v2_300_20260722/hybrid_top20_300.jsonl`
- `archive/260722/outputs/hybrid_v2_300_20260722/path_debug_300.jsonl`
- `archive/260722/outputs/hybrid_v2_300_20260722/hyde_predictions_300.jsonl`
- `archive/260722/outputs/hybrid_v2_300_20260722/query_embedding_cache.jsonl`
- `archive/260722/outputs/hybrid_v2_300_20260722/errors.jsonl`
- `archive/260722/outputs/hybrid_v2_300_20260722/summary.json`

## 15. 결론 및 권장 작업

Catalog v2 515건과 Claim 300건으로 네 경로 하이브리드 검색이 오류 없이 완료됐다. BM25를 `doc_meta_text`로 변경하자 B2/B4와 Dense 경로의 후보 중복이 낮아져 경로 간 보완성이 확인됐지만, B4 빈 결과가 65건 발생했다.

다음 단계에서는 골드 `table_key`를 마련하고 아래 실험을 수행해야 한다.

1. `doc_item_index BM25`와 `doc_meta_text BM25`의 Recall@N 비교
2. B2만, B4만, B2+B4 RRF의 ablation 비교
3. Claim Dense와 HyDE Dense의 독점 골드 기여도 측정
4. 반복적으로 상위에 노출되는 허브 표의 오탐 여부 분석
5. v2 Catalog를 실제 정답 표가 포함되도록 확장
