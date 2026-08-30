# AWS 배포 담당자용 인계 문서 — 프론트/백엔드 구동 파일 정리

> 대상: DB·서버·AWS 인프라 담당 팀원
> 작성 목적: `develop` 브랜치에 올릴 **구동 필수 파일**과, git에는 못 올리고 **별도로 전달해야 하는 것**(시크릿·대용량 데이터·인프라)을 분리해 전달.
> 검증 기준: 저장소 루트의 `./.venv` 파이썬으로 실제 import 테스트 완료(2026-08-26).

---

## 0. 연결 상태 확인 결과 (요약)

백엔드는 **develop 파이프라인과 전체(KOSIS) 파이프라인에 정상 연결**되어 있습니다. 실제 import 검증 결과:

| 경로 | 진입점 | 상태 |
|---|---|---|
| 기사 본문 검증 | `POST /v1/verify/develop` → `src.develop.run_article_body_pipeline_trace_v1.run_trace` | ✅ 연결 OK |
| 통계 질의(KOSIS) | `POST /v1/analyze` → `backend.team_mcp_service` → `src.run_pipeline.answer_natural_query` | ✅ 연결 OK |
| 이미지 OCR | `POST /v1/analyze/image` → `backend.image_ocr_service` → `src/hcx_ocr.py` | ✅ 연결 OK |
| 백엔드 앱 전체 | `backend.app` (+ 5개 서비스 모듈) | ✅ import OK |

**주의:** 위 경로가 의존하는 파일 중 **21개가 아직 git에 커밋되지 않은(untracked) 상태**입니다. 이걸 커밋하지 않으면 팀원이 clone해도 서버가 import 에러로 뜨지 않습니다. 아래 2번 목록이 반드시 함께 올라가야 합니다.

---

## 1. Git으로 올릴 것 — 폴더/파일 구성

### 1-A. 프론트엔드 (`frontend/`) — 전부 tracked, 그대로 push
```
frontend/
├─ src/            # React 소스 (ChatApp, Home, Login, api.js, auth.jsx 등)
├─ public/
├─ index.html
├─ package.json / package-lock.json
├─ vite.config.js
├─ .env.example    # 프론트 환경변수 예시
├─ .nvmrc / .oxlintrc.json
└─ README.md / DEVELOPMENT.md / HANDOFF.md
```
- **제외:** `frontend/node_modules/` (커밋 금지, `npm install`로 복원)

### 1-B. 백엔드 (`backend/`) — FastAPI 서버
이미 tracked 된 파일(app.py, 각종 `*_service.py`, `*_oauth.py`, `input_router.py`, `llm_router.py`, `errors.py`, `database.py`, `README.md`, `ERD.md` 등)에 더해 **아래 미트래킹 파일을 반드시 추가**:
```
backend/team_mcp_service.py      ← ⚠️ untracked. /v1/analyze(KOSIS 질의) 경로 필수
```

### 1-C. 파이프라인 소스 (`src/`) — ⚠️ 미트래킹 필수 파일
`src/develop/`(48개)와 `src/hcx_ocr.py` 등은 이미 tracked. **아래 파일들이 미트래킹인데 런타임 필수**이므로 함께 커밋:
```
src/config.py
src/run_pipeline.py                 # KOSIS 질의 파이프라인 진입점
src/kosis_statistical_grade.py
src/tolerance_judge.py

src/kosis_agent/
├─ axis_binding.py
├─ axis_contract.py
├─ kosis_call_tool.py
├─ period_lock.py
├─ period_resolver.py
├─ table_select.py
└─ verdict_output.py

src/kosis_retriever/               # 패키지 전체 (현재 통째로 미트래킹)
├─ __init__.py
├─ bm25_backend.py
├─ config.py
├─ embed.py
├─ fusion.py
├─ hyde.py
├─ org_infer.py
├─ pool.py
└─ retriever.py
```
> `src/`에는 이 외에도 실험용 파일(`evaluate_*.py`, `collect_*.py`, `hcx_*_experiment*.py` 등)이 다수 있습니다. **구동에는 불필요**하므로, develop 브랜치를 깔끔히 유지하려면 위 목록만 선별 커밋하는 것을 권장합니다.

### 1-D. 설정·의존성
```
requirements.txt          # tracked. 백엔드/파이프라인 파이썬 의존성
requirements-bge.txt      # ⚠️ untracked. BGE 인코더/리랭커 서비스용 (라이브 판정 인프라)
configs/pipeline_operational_v2.json   # tracked. develop 라이브 판정 설정 (코드가 직접 참조)
.env.example              # tracked. 백엔드 환경변수 템플릿
```
> `configs/`의 나머지 파일(`fewshot_*`, `hcx_*`, `sample600_*` 등)은 실험용이라 구동에 불필요합니다.

---

## 2. Git으로 올리면 안 되는 것 → **별도 채널로 전달**

이건 `.gitignore`로 이미 막혀 있고, 용량/보안상 git에 넣으면 안 됩니다. AWS 담당자에게 **따로** 전달·주입해야 합니다.

### 2-A. 시크릿 (`.env`) → AWS Secrets Manager / 환경변수 주입
`.env.example`을 복사해 실제 값 채움. 필요한 키:

| 변수 | 용도 | 필수 여부 |
|---|---|---|
| `KOSIS_API_KEY` | KOSIS Open API | 필수 |
| `NCP_CLOVASTUDIO_API_KEY` | HCX(Clova) LLM·OCR | 필수 |
| `QDRANT_URL` | 벡터 검색 | 라이브 판정 필수* |
| `BGE_QUERY_ENCODER_URL` | 쿼리 임베딩 서비스 | 라이브 판정 필수* |
| `BGE_RERANKER_URL` | 리랭커 서비스 | 라이브 판정 필수* |
| `GOOGLE_/KAKAO_/NAVER_*` | 소셜 로그인 OAuth | 쓰는 제공자만 |
| `FRONTEND_ORIGIN`, `CORS_ALLOWED_ORIGINS` | CORS·콜백 | 배포 도메인에 맞게 |

> *세 개(`QDRANT_URL`, `BGE_QUERY_ENCODER_URL`, `BGE_RERANKER_URL`)가 **모두** 있어야 라이브 판정(stage 04)까지 수행. 없으면 백엔드는 구조화·라우팅까지만 하고 정상 응답합니다(죽지 않음).

### 2-B. 대용량 데이터 → S3 등으로 전달 (git 불가)
`data/`는 gitignore 대상. 서버에서 아래 경로에 위치시켜야 함:

| 파일 | 크기 | 용도 | 없으면 |
|---|---|---|---|
| `data/kosis_catalog/tables_v5.sqlite` | ~477 MB | 통계표 카탈로그(28.7만개) 검색 | sample600으로 폴백 |
| `data/kosis_catalog/kosis_catalog_v5_260817.jsonl` | ~1.86 GB | 카탈로그 원본/인덱싱 소스 | 인덱스 재생성 불가 |
| `data/kosis_catalog_enriched_sample600.jsonl` | ~1.3 MB | 소규모 폴백(600개) | 카탈로그 검색 축소 |

> `tables_v5.sqlite`는 `backend/build_table_index.py`로 jsonl에서 재생성 가능. sqlite만 전달하면 검색은 동작.

### 2-C. 인프라 (파일 아님 — 서버에 구축)
- **Qdrant** 벡터 DB: 컬렉션 `kosis_tables_v5` (기본 로컬 `http://127.0.0.1:6333`). 클라우드 Qdrant면 `QDRANT_URL`로 지정.
- **BGE-M3 인코더 / BGE reranker v2** 서비스: `requirements-bge.txt` 기반으로 별도 배포, URL을 위 env에 주입.

---

## 3. AWS 담당자 세팅 순서 (요약)

```bash
# 1) 프론트엔드
cd frontend && npm install && npm run build   # 또는 npm run dev (로컬 확인)

# 2) 백엔드 파이썬 환경
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# (Linux: .venv/bin/pip install -r requirements.txt)

# 3) 시크릿
cp .env.example .env   # 값 채우기 (2-A 표 참고)

# 4) 데이터 배치 (2-B: S3에서 data/ 아래로)

# 5) 인프라 (2-C: Qdrant + BGE 서비스), URL을 .env에 주입

# 6) 서버 실행 (저장소 루트에서)
./.venv/Scripts/python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
#   http://<host>:8000/docs 에서 엔드포인트 확인
```

### 배포 후 스모크 테스트 (import 누락 즉시 확인)
```bash
./.venv/Scripts/python.exe -c "import backend.app, backend.team_mcp_service; from src.run_pipeline import answer_natural_query; from src.develop.run_article_body_pipeline_trace_v1 import run_trace; print('imports OK')"
```
`imports OK`가 뜨면 코드 파일 배선은 완전합니다. 그 다음 `.env`·데이터·인프라만 채우면 됩니다.

---

## 4. 한눈에 보는 체크리스트

- [ ] `frontend/` 커밋 (node_modules 제외)
- [ ] `backend/` 커밋 + **`backend/team_mcp_service.py` 추가**
- [ ] `src/develop/`, `src/hcx_ocr.py` (tracked) + **1-C의 미트래킹 파일 21개 추가**
- [ ] `requirements.txt` + **`requirements-bge.txt` 추가**, `configs/pipeline_operational_v2.json`, `.env.example`
- [ ] (별도) `.env` 실제 값 → Secrets Manager
- [ ] (별도) `data/` 대용량 파일 → S3
- [ ] (별도) Qdrant 컬렉션 + BGE 서비스 배포, URL 주입
- [ ] 스모크 테스트 `imports OK` 확인
