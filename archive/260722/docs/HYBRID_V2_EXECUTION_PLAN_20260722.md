# KOSIS Catalog v2 하이브리드 검색 실행 계획서

- 작성일: 2026-07-22
- 대상 데이터: `data/kosis_catalog_v2.jsonl`
- 구현 위치: `archive/260722/src/v2`
- 실험 범위: Catalog v2 전체 515개 표

## 1. 목적

KOSIS Catalog v2를 대상으로 다음 네 검색 경로를 독립 실행한 뒤 RRF로 최종 Top-20 표를 선정한다.

1. B2 형태소 BM25 → `doc_meta_text`
2. B4 핵심 성분 BM25 → `doc_meta_text`
3. Claim Dense → `doc_meta_text` 임베딩
4. HyDE Dense → HCX 예상 표명 생성 후 `tbl_name` 임베딩

## 2. 기존 v1 실험과 달라진 점

| 구분 | 기존 v1 | 이번 v2 |
|---|---|---|
| Catalog | `kosis_catalog_v1.jsonl` | `kosis_catalog_v2.jsonl` |
| 실험 표 수 | BM25 265,094건, Dense 1,000건 | 모든 경로 515건으로 통일 |
| BM25 문서 | `doc_item_index` 우선 | `doc_meta_text`만 사용 |
| Claim Dense 문서 | `doc_meta_text`/`document_text` | `doc_meta_text` 유지 |
| HyDE Dense 문서 | `tbl_name` | `tbl_name` 유지 |
| Qdrant | `kosis_tables`, v1 로컬 DB | `kosis_tables_v2`, v2 전용 로컬 DB |
| 최종 산출물 | RRF Top-20 | RRF Top-20, v2 식별자 포함 |

핵심 변경은 BM25 문서를 항목 나열인 `doc_item_index`에서 표명·분류 경로·차원을 담은 `doc_meta_text`로 교체하는 것이다. Claim Dense와 HyDE Dense의 검색 대상 필드는 기존 방식을 유지한다.

## 3. 데이터 사전 점검

- JSONL 515행이 모두 유효한지 확인한다.
- `table_key`가 515개 모두 고유한지 확인한다.
- `doc_meta_text`와 `tbl_name` 누락이 없는지 확인한다.
- 확인 결과: 유효 515행, 고유 키 515개, 두 필드 누락 0건, `meta_status=enriched` 515건.

## 4. 구현 파일

- `archive/260722/src/v2/kosis_v2_indexer.py`
  - v2 전용 Qdrant 두 named-vector 색인
  - `doc_meta_vector`: `doc_meta_text`
  - `tbl_name_vector`: `tbl_name`
  - 임베딩 캐시와 색인 요약 제공
- `archive/260722/src/v2/search_hybrid_v2.py`
  - `doc_meta_text` 형태소 BM25
  - Claim Dense, HCX HyDE Dense
  - 네 경로 RRF와 최종/디버그 JSON 분리

## 5. 실행 순서

### 5.1 v2 Qdrant 색인

```powershell
.\.venv\Scripts\python.exe archive\260722\src\v2\kosis_v2_indexer.py `
  --input data\kosis_catalog_v2.jsonl `
  --local `
  --batch-size 32
```

### 5.2 단일 Claim 하이브리드 검색

```powershell
.\.venv\Scripts\python.exe archive\260722\src\v2\search_hybrid_v2.py `
  --claim-text '17일 통계청은 지난달 보건·사회복지 서비스업 종사자가 281만2000명으로 전체 취업자(2787만8000명)의 10.1%를 차지했다고 밝혔다.' `
  --per-path-n 20 `
  --top-n 20
```

## 6. 산출물

- `archive/260722/outputs/hybrid_v2_20260722/index_summary.json`
- `archive/260722/outputs/hybrid_v2_20260722/result.json`
- `archive/260722/outputs/hybrid_v2_20260722/result_debug.json`
- `archive/260722/docs/HYBRID_V2_EXECUTION_REPORT_20260722.md`

`result.json`에는 최종 선정 표만 기록하고, 경로별 후보·순위·원점수는 `result_debug.json`에만 기록한다.

## 7. 검증 기준

- Qdrant 포인트 수 515개
- 각 포인트에 두 named vector 존재
- B2/B4/Claim Dense/HyDE Dense 각각 최대 20개 후보
- 최종 Top-20의 `table_key` 중복 없음
- 최종 순위 1~20 연속
- UTF-8 JSON 파싱 성공 및 유니코드 대체문자 없음
- 실제 골드 `table_key`가 없으므로 Recall@N은 이번 단일 Claim 실행에서 계산하지 않음

## 8. 외부 API 전송 범위

- 색인: v2의 `doc_meta_text`와 `tbl_name`을 CLOVA Embedding API에 전송
- 검색: Claim 원문과 HCX 예상 표명을 CLOVA Embedding API에 전송
- HyDE: Claim 원문만 HCX Chat API에 전송
- BM25 후보나 Qdrant 후보를 HCX 예상 표명 생성에 제공하지 않음
