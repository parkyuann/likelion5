# 뉴스 사실검증 파이프라인 배포 인계서 (20260824)

이 브랜치는 이미 구축·연결된 Qdrant/BM25/profile DB와 encoder/reranker 서비스를 호출해 기사 본문을 검증하는 파이프라인 코드만 전달합니다. 데이터, 모델, DB, 색인, 로그, 샘플 출력, 실험·라벨링 파일은 포함하지 않습니다.

## 실행 경계

- 배포 진입점: `src/develop/run_article_body_pipeline_trace_v1.py`
- 운영 오케스트레이터: `src/develop/run_pipeline_operational_v2.py`
- 설정: `configs/pipeline_operational_v2.json`
- 모델 서비스 HTTP client/contract: `src/develop/bge_m3_ko_query_encoder_service.py`, `src/develop/bge_reranker_v2_service.py`

프론트엔드는 이 코드를 직접 호출하지 않고 팀 FastAPI 서버만 호출합니다. FastAPI 서버는 요청마다 입력 JSONL과 고유 출력 디렉터리를 만든 뒤 `run_trace()`를 호출하고, 완료 후 결과 파일을 응답 DTO로 투영해야 합니다.

## 사전 조건

다음 외부 런타임이 연결된 상태를 가정합니다.

| 항목 | 기본 계약 |
|---|---|
| Qdrant | `http://127.0.0.1:6335`, collection `kosis_v6_bge_m3_ko_20260821`, vector `dense` |
| Query encoder | `http://127.0.0.1:8820`, BGE-m3-ko revision `7074d66aa46562342193ca4feb3d89bf9dad71b4` |
| Reranker | `http://127.0.0.1:8819`, bge-reranker-v2-m3-ko revision `2aca5884ecac490192af9ebd86836d9073d826cd` |
| BM25/profile DB | `configs/pipeline_operational_v2.json`의 `assets` 경로에 mount |
| 외부 API | `KOSIS_API_KEY`, `NCP_CLOVASTUDIO_API_KEY` 환경변수 |

서비스 URL은 `QDRANT_URL`, `BGE_QUERY_ENCODER_URL`, `BGE_RERANKER_URL`로 재지정할 수 있습니다. 실제 키나 연결 문자열은 Git에 커밋하지 않습니다.

## 설치

```powershell
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
# GPU 모델 서비스도 같은 환경에서 띄우는 경우 먼저 호스트 CUDA에 맞는 torch를 설치합니다.
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

## 입력 계약

UTF-8 JSONL이며 기사 1건당 한 줄입니다.

```json
{"article_idx":"request-uuid","title":"기사 제목","date":"2026-08-24","article_text":"기사 본문 전체"}
```

FastAPI에서 호출할 최소 형태:

```python
from pathlib import Path
from src.develop.run_article_body_pipeline_trace_v1 import run_trace

manifests = run_trace(
    articles_path=Path("runtime/requests/request-uuid/articles.jsonl"),
    output_root=Path("runtime/results/request-uuid"),
    stage="all",
    config_path=Path("configs/pipeline_operational_v2.json"),
)
```

CLI 재현:

```powershell
.\venv\Scripts\python.exe -m src.develop.run_article_body_pipeline_trace_v1 `
  --articles runtime\requests\request-uuid\articles.jsonl `
  --output runtime\results\request-uuid `
  --config configs\pipeline_operational_v2.json `
  --stage all
```

같은 출력 디렉터리를 여러 요청이 공유하면 안 됩니다. 요청 ID별 새 디렉터리를 사용하고, timeout과 동시 실행 제한은 FastAPI 작업 계층에서 관리하세요.

## 출력 계약

| 파일 | 용도 |
|---|---|
| `01_sentences.jsonl` | 문장 inventory |
| `01_value_candidates.jsonl` | 수치 후보 |
| `02_l2_results.jsonl` | HCX 구조화 상태 |
| `03_routed.jsonl` | 검증 target과 검색 질의 |
| `04_stage_ledger.jsonl` | 표 선택, 셀 조회, blocker, 비교 근거 |
| `04_answers.jsonl` | 최종 `verdict`, `headline`, `explanation`, `limitation` |
| `01_manifest.json`~`04_manifest.json` | 단계 완료, SHA, 호출량, secret scan 결과 |

일반 응답은 `04_answers.jsonl`을 사용하고 상세 근거 화면은 같은 target의 `04_stage_ledger.jsonl`을 연결합니다. 내부 API 키, 로컬 절대경로, 원본 예외 문자열은 클라이언트 응답에 노출하지 않습니다.

## 현재 동작 한계

- 이미지 분석은 포함하지 않습니다.
- 검색·binding·공식 셀 확인이 실패하면 억지 판정 대신 `UNVERIFIABLE`로 닫힙니다.
- `role_aware_dimension_shadow`, `failure_recovery_shadow`, `user_intent_shadow`는 opt-in 실험 경로이므로 기본 API에서 활성화하지 않습니다.
- 이 브랜치는 실행 코드 전달본이며 모델 성능 승인 또는 운영 승인 기록이 아닙니다.

## 최소 검증

```powershell
.\venv\Scripts\python.exe -m compileall -q src
.\venv\Scripts\python.exe -m src.develop.run_article_body_pipeline_trace_v1 --help
```

full live 재현은 위 DB·서비스·API 키가 연결된 배포 환경에서 확인해야 합니다.
