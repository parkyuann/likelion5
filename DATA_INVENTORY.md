# 잔존 데이터 및 디렉토리 안내

작성일: 2026-07-15

현재 루트에는 실제 파이프라인에 필요한 코드·설정·원천/중간 데이터만 남기고, 사용 빈도가 낮은 실험 파일은 `archive/2026-07-15/`로 이동했다. `archive/` 전체는 `.gitignore`에 등록되어 Git에 포함되지 않는다.

## 현재 유지하는 항목

### 실행 코드: `src/`

| 파일 | 용도 |
|---|---|
| `claim_extractor.py` | 기사에서 수치 claim 후보 및 구조 필드 추출 |
| `claim_normalizer.py` | 수치·단위·상대 기간 정규화 |
| `convert_claim_listform_to_schema.py` | `claim_listform.csv`를 schema v1 JSONL로 변환 |
| `create_retrieval_eval_set.py` | retrieval 평가/silver labeling 표본 생성 |
| `label_retrieval_eval_codex.py` | Codex silver pre-label 생성 |
| `retrieval_schema.py` | Claim·KOSIS table·mapping canonical schema 및 검증 |
| `kosis_tree_crawler.py` | KOSIS 통계표 트리 수집 |
| `kosis_client.py` / `kosis_schema.py` | KOSIS API 호출 및 SQLite schema |
| `kosis_claim_matcher.py` | claim과 KOSIS 후보 매칭 로직 |
| `llm_claim_extractor.py` / `fewshot_claim_extractor.py` | LLM 기반 claim 추출 실험·실행 코드 |
| `hcx_embedding_client.py` | HCX embedding 호출 래퍼 |
| `news_preprocessor.py` | 원천 기사 전처리 |
| `source_scope_analysis.py` | 기관·출처 범위 분석 |

### 주요 설정·문서

- `readme.md`: 프로젝트 개요와 실행 안내
- `AGENTS.md`: 작업 규칙
- `configs/fewshot_config.json`: few-shot 설정
- `requirements.txt`: Python 의존성
- `AI_기반_뉴스_사실검증_시스템_프로젝트.pdf`, `openApi_manual_v1.0.pdf`: 프로젝트/API 참고 자료
- `docs/`: 단계별 계획·schema·작업 보고서

### 원천 및 현재 중간 산출물

| 경로 | 설명 |
|---|---|
| `data/raw/` | 원본 프로젝트 데이터 |
| `data/news_preprocessed.csv` | 전처리된 기사 데이터 |
| `data/claim_listform.csv` | 현재 retrieval 전 단계의 claim 목록 |
| `data/claims_v1.jsonl` | schema v1 변환 결과 |
| `data/kosis_table_tree.json` | KOSIS 통계표 catalog |
| `data/kosis_org_names.json` | KOSIS 기관 ID·기관명 catalog |
| `data/retrieval_eval_claims_v0.csv` | 80% retrieval silver labeling 입력셋 |
| `data/retrieval_eval_claims_v0_codex.csv` | Codex silver pre-label 결과 |
| `data/retrieval_eval_claims_v0_manifest.json` | 평가셋 샘플링·split manifest |
| `data/labeling/` | Claude/Codex/team2 라벨링 및 통합 검토 파일 |

## 보관한 항목

`archive/2026-07-15/` 아래로 이동한 파일은 삭제하지 않은 이전 실험·임시 산출물이다.

- `root/`: 임시 텍스트, exploratory CSV, notebook
- `data_legacy/`: 이전 claim 후보/파일럿 라벨 및 빈 mapping JSONL
- `src_experiments/`: table index 실험 스크립트
- `output_experiments/`: few-shot cache 및 table index 실험 결과

필요해질 경우 보관 경로에서 복사해 복원하고, 복원한 파일의 목적과 현재 schema 호환 여부를 확인한 뒤 사용한다.

## 재생성 가능한 파일

- `claims_v1.jsonl`: `src/convert_claim_listform_to_schema.py`로 재생성
- `retrieval_eval_claims_v0.csv`: `src/create_retrieval_eval_set.py`로 재생성
- `retrieval_eval_claims_v0_codex.csv`: `src/label_retrieval_eval_codex.py`로 재생성

CSV·JSONL·대용량 원천 데이터는 `.gitignore` 정책상 Git에 포함하지 않고 로컬 또는 공유 저장소에서 관리한다.
