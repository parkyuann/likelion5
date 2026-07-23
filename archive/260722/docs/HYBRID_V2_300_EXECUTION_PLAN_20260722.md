# KOSIS Catalog v2 Claim 300건 하이브리드 검색 실행 계획서

- 최초 작성일: 2026-07-22
- 실제 실행일: 2026-07-23
- Catalog: `data/kosis_catalog_v2.jsonl` 515건
- Claim 원본: `data/claims_v1.jsonl` 19,415건
- 샘플 수: 300건
- 샘플 시드: `20260722`
- 코드 위치: `archive/260722/src/v2`

## 목적

`claims_v1.jsonl`에서 300건을 재현 가능한 방식으로 샘플링하고, Catalog v2 515건을 대상으로 다음 네 경로를 실행해 Claim별 최종 Top-20을 JSONL로 저장한다.

- B2 형태소 BM25 → `doc_meta_text`
- B4 핵심 성분 BM25 → `doc_meta_text`
- Claim Dense → `doc_meta_vector`
- HyDE Dense → HCX 예상 표명 생성 후 `tbl_name_vector`
- RRF `k=60` 결합

## 기존 단일 v2 실행과 달라진 점

| 구분 | 단일 v2 실행 | 이번 실행 |
|---|---:|---:|
| Claim | 1건 | 300건 |
| BM25 인덱스 빌드 | 1회 | 전체 배치에서 1회만 빌드 후 재사용 |
| Qdrant 연결 | Claim 1건 | 300건 처리 동안 단일 연결 재사용 |
| HCX/임베딩 캐시 | 단일 호출 | JSONL 캐시로 중단 후 재실행 지원 |
| 최종 산출물 | JSON | Claim당 1행 JSONL |

## 샘플링

전체 파일을 메모리에 적재하지 않고 reservoir sampling을 사용한다. 동일한 원본 파일과 시드 `20260722`를 사용하면 같은 300건이 선택된다.

## 출력

경로: `archive/260722/outputs/hybrid_v2_300_20260722`

- `sample_claims_300.jsonl`: 실제 샘플 Claim
- `hybrid_top20_300.jsonl`: 최종 선정 표만 포함한 Claim별 결과
- `path_debug_300.jsonl`: 경로별 후보·순위·원점수
- `hyde_predictions_300.jsonl`: HCX 예상 표명 캐시
- `query_embedding_cache.jsonl`: Claim/HyDE 질의 임베딩 캐시
- `errors.jsonl`: 실패 경로
- `summary.json`: 처리량·중복률·속도 요약

## 실행 명령

```powershell
.\.venv\Scripts\python.exe archive\260722\src\v2\run_hybrid_v2_300.py `
  --claims data\claims_v1.jsonl `
  --sample-size 300 `
  --seed 20260722 `
  --per-path-n 20 `
  --top-n 20
```

## 검증 기준

- 샘플 300행 및 고유 `claim_id` 300개
- 최종 JSONL 300행
- 각 Claim의 최종 후보 최대 20개, 중복 `table_key` 없음
- 모든 최종 순위가 1부터 연속
- Qdrant 검색 범위 515건
- JSONL 전 행 파싱 성공 및 UTF-8 유니코드 대체문자 없음
- 경로별 후보 수, 경로 간 평균 Jaccard, 오류 수 집계

## 외부 API 전송

- 샘플 Claim 300건의 Claim 기반 질의를 CLOVA Embedding API에 전송
- 샘플 Claim 300건을 HCX Chat API에 전송해 예상 표명 생성
- 예상 표명 최대 300건을 CLOVA Embedding API에 전송
- 기존 성공 캐시와 일치하는 Claim/예상 표명은 재전송하지 않음
- BM25 후보 및 Qdrant 후보는 HCX에 전송하지 않음

## 평가 한계

골드 `table_key`가 제공되지 않았으므로 Recall@N은 계산하지 않는다. 이번 실행은 처리 안정성, 경로별 후보 수, 후보 중복률 및 결과 형식 검증을 중심으로 한다.
