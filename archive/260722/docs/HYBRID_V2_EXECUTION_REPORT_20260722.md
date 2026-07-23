# KOSIS Catalog v2 하이브리드 검색 실행 결과 보고서

- 작성일: 2026-07-22
- 실행일: 2026-07-22
- 대상 데이터: `data/kosis_catalog_v2.jsonl`
- 구현 코드: `archive/260722/src/v2`
- 결과 경로: `archive/260722/outputs/hybrid_v2_20260722`

## 1. 실행 목적

KOSIS Catalog v2를 사용하면서 BM25 문서를 기존 `doc_item_index`에서 `doc_meta_text`로 변경하고, Claim Dense와 HyDE Dense는 기존 검색 대상을 유지한 하이브리드 검색을 구현·실행했다.

검색 경로는 다음과 같다.

1. B2 형태소 BM25 → `doc_meta_text`
2. B4 핵심 성분 BM25 → `doc_meta_text`
3. Claim Dense → `doc_meta_vector` (`doc_meta_text` 임베딩)
4. HyDE Dense → `tbl_name_vector` (`tbl_name` 임베딩)
5. 네 경로를 RRF(`k=60`)로 결합하여 최종 Top-20 선정

## 2. 기존 v1과 달라진 점

| 구분 | 기존 v1 실행 | 이번 v2 실행 |
|---|---|---|
| Catalog | `kosis_catalog_v1.jsonl` | `kosis_catalog_v2.jsonl` |
| BM25 문서 | `doc_item_index` | `doc_meta_text` |
| Claim Dense 문서 | `doc_meta_text`/`document_text` | `doc_meta_text` 유지 |
| HyDE Dense 문서 | `tbl_name` | `tbl_name` 유지 |
| BM25 범위 | 265,094건 | 515건 |
| Dense 범위 | v1 Qdrant 1,000건 | v2 Qdrant 515건 |
| 검색 범위 일치 | 불일치 | 모든 경로 515건으로 일치 |
| Qdrant | `kosis_tables` | 별도 `kosis_tables_v2` |

이번 변경의 핵심은 BM25가 표 내부 항목 목록이 아니라 표명·분류 경로·차원을 포함한 `doc_meta_text`의 형태소를 검색하도록 한 것이다. Dense 두 경로의 역할은 변경하지 않았다.

## 3. v2 데이터 검증

| 항목 | 결과 |
|---|---:|
| 전체 JSONL 행 | 515 |
| 유효 JSON 행 | 515 |
| 고유 `table_key` | 515 |
| `doc_meta_text` 누락 | 0 |
| `tbl_name` 누락 | 0 |
| `meta_status=enriched` | 515 |

## 4. Qdrant 색인 결과

실행 명령:

```powershell
.\.venv\Scripts\python.exe archive\260722\src\v2\kosis_v2_indexer.py `
  --input data\kosis_catalog_v2.jsonl `
  --local `
  --batch-size 32
```

| 항목 | 결과 |
|---|---:|
| 입력 레코드 | 515 |
| Qdrant 포인트 | 515 |
| 컬렉션 상태 | green |
| `doc_meta_vector` | 1024차원, Cosine |
| `tbl_name_vector` | 1024차원, Cosine |
| 임베딩 캐시 적중 | 234 |
| 임베딩 API 호출 | 798 |
| 색인 소요 시간 | 547.871초(약 9분 8초) |

표 하나당 두 텍스트를 처리하므로 총 조회 대상은 1,030개이며, 최초 컬렉션 설정용 중복 조회를 포함한 캐시 통계는 적중 234건, 신규 API 호출 798건이다. 기존 v1 Qdrant는 수정하지 않았다.

## 5. 단일 Claim 검색 결과

검색 Claim:

```text
17일 통계청은 지난달 보건·사회복지 서비스업 종사자가 281만2000명으로 전체 취업자(2787만8000명)의 10.1%를 차지했다고 밝혔다.
```

실행 명령:

```powershell
.\.venv\Scripts\python.exe archive\260722\src\v2\search_hybrid_v2.py `
  --claim-text '17일 통계청은 지난달 보건·사회복지 서비스업 종사자가 281만2000명으로 전체 취업자(2787만8000명)의 10.1%를 차지했다고 밝혔다.' `
  --per-path-n 20 `
  --top-n 20
```

HCX 예상 표명:

```text
보건 및 사회복지서비스업 취업자 현황
```

| 항목 | 결과 |
|---|---:|
| BM25 검색 범위 | 515 |
| Dense 검색 범위 | 515 |
| B2 BM25 후보 | 20 |
| B4 BM25 후보 | 20 |
| Claim Dense 후보 | 20 |
| HyDE Dense 후보 | 20 |
| 최종 후보 | 20 |
| 경로 오류 | 0 |
| 검색 소요 시간 | 4.934초 |

## 6. 경로 간 후보 중복

| 경로 쌍 | 교집합 | Jaccard |
|---|---:|---:|
| B2 BM25 / B4 BM25 | 14 | 0.538462 |
| B2 BM25 / Claim Dense | 7 | 0.212121 |
| B2 BM25 / HyDE Dense | 10 | 0.333333 |
| B4 BM25 / Claim Dense | 7 | 0.212121 |
| B4 BM25 / HyDE Dense | 10 | 0.333333 |
| Claim Dense / HyDE Dense | 10 | 0.333333 |

B2와 B4는 동일한 `doc_meta_text` 인덱스를 서로 다른 질의 표현으로 검색하므로 중복이 가장 높았다. Claim Dense와 HyDE Dense는 검색 문서가 다름에도 Top-20 중 10개가 겹쳤다.

## 7. 최종 Top-5

| 최종 순위 | table_key | 표명 | RRF 점수 |
|---:|---|---|---:|
| 1 | `101:DT_1IN3017` | 행정구역/직업(대분류)/성별 취업자 | 0.0619979727 |
| 2 | `101:DT_1IN3018` | 행정구역/직업(소분류)/성별 취업자 | 0.0605444217 |
| 3 | `358:DT_358005_012` | 고령친화 용품 제품 관련 종사자 고용형태별 종사자 비율 | 0.0597347665 |
| 4 | `101:DT_1IN5508` | 행정구역/성/직업(대분류)별 취업자(14세이상 인구) | 0.0596392738 |
| 5 | `358:DT_358005_010` | 고령친화 용품 제조업 전체 종사자 수 분포 | 0.0595296201 |

최종 1~7위는 네 경로 모두에서 검색된 후보였다. 따라서 RRF 관점에서는 강한 합의 후보지만, Claim이 요구하는 산업별 보건·사회복지서비스업 취업자 표와 직접 일치하지는 않는다.

## 8. 결과 해석

### 8.1 확인된 개선점

- v1 실행의 가장 큰 문제였던 BM25와 Dense 검색 범위 불일치를 제거했다.
- v2 전용 컬렉션을 사용해 v1과 v2 포인트가 혼합되지 않는다.
- BM25가 `doc_meta_text`를 검색하므로 표명·분류 경로·차원 정보를 직접 이용한다.
- 네 경로가 모두 정상 실행됐고 결과 형식과 UTF-8 무결성이 검증됐다.

### 8.2 현재 결과의 한계

v2 515건에서 `보건`, `사회복지`, `서비스업`을 표명 또는 `doc_meta_text`에 직접 포함한 적합 산업별 취업자 표는 확인되지 않았다. `취업자`가 포함된 관련 레코드는 세 건뿐이며 모두 1930년 또는 1955년 인구총조사 직업별 표였다.

따라서 이번 Top-1이 기대 표와 다른 가장 큰 이유는 검색 알고리즘 오류라기보다 현재 v2 515건 후보군에 목표 표가 없기 때문이다. 후보군에 정답 표가 없다면 어떤 reranking 방식도 정답을 최종 선택할 수 없다.

또한 B2와 B4가 동일 문서를 검색하고 Top-20 중 14개가 겹치므로, RRF에서 BM25 계열 후보가 중복 투표 효과를 받을 수 있다. 추후에는 B2/B4 경로 가중치 조정이나 한 경로만 사용하는 ablation 비교가 필요하다.

### 8.3 Recall 미계산 사유

이 Claim에 대응하는 골드 `table_key`가 제공되지 않았고, v2 후보군에 목표 표가 존재하는지도 확인되지 않았다. 따라서 Recall@N은 계산하지 않았다. 현재 수치는 검색 실행과 후보 중복 진단 결과이며 정확도 지표가 아니다.

## 9. 검증 결과

- Qdrant 상태 `green`: 통과
- Qdrant 포인트 515개: 통과
- 두 named vector 1024차원 Cosine: 통과
- 네 경로 각각 Top-20: 통과
- 최종 후보 20개 및 고유 `table_key` 20개: 통과
- 최종 순위 1~20 연속: 통과
- UTF-8 JSON 파싱 및 유니코드 대체문자 0개: 통과
- 경로 오류 0건: 통과

## 10. 산출물

### 코드

- `archive/260722/src/v2/kosis_v2_indexer.py`
- `archive/260722/src/v2/search_hybrid_v2.py`

### 실행 결과

- `archive/260722/outputs/hybrid_v2_20260722/index_summary.json`
- `archive/260722/outputs/hybrid_v2_20260722/result.json`
- `archive/260722/outputs/hybrid_v2_20260722/result_debug.json`
- `archive/260722/outputs/hybrid_v2_20260722/embed_cache.jsonl`

### 문서

- `archive/260722/docs/HYBRID_V2_EXECUTION_PLAN_20260722.md`
- `archive/260722/docs/HYBRID_V2_EXECUTION_REPORT_20260722.md`

## 11. 다음 권장 작업

1. v2 Catalog를 목표 산업별 취업자 표까지 포함하도록 확장한다.
2. 골드 `table_key`가 있는 Claim 집합을 만든다.
3. 동일 후보군에서 `doc_item_index BM25`와 `doc_meta_text BM25`의 Recall@N을 비교한다.
4. B2만, B4만, B2+B4의 ablation 실험으로 중복 투표 효과를 측정한다.
5. 정답 표가 후보군에 존재하는 조건에서 Claim Dense와 HyDE Dense의 독점 기여도를 평가한다.
