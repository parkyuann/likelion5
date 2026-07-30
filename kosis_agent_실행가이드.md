# kosis_agent 실행 가이드

뉴스 기사의 **수치 주장 → KOSIS 공식 통계표 매핑 → 실제 수치 비교·판정**까지 수행하는
대화형 사실검증 파이프라인. 코드는 [`src/kosis_agent/`](src/kosis_agent) 폴더 14개 모듈로 구성.

---

## 1. 준비물

| 항목 | 내용 |
|---|---|
| 가상환경 | `likelion5-jihyeon/.venv` (시스템 파이썬 금지) |
| API 키 | 레포 루트 `.env` 에 `HCX_API_KEY`(또는 `NCP_CLOVASTUDIO_API_KEY`), KOSIS 키 |
| 로컬 벡터 색인 | `output/kosis_qdrant_v2/` (하이브리드 dense 검색용) |
| 카탈로그 | `data/kosis_catalog_enriched_sample600.jsonl` (BM25·표 설명·payload용) |
| 하이브리드 검색기 | `archive/260722/src/v2/search_hybrid_v2.py` (팀원 파일, subprocess로 호출) |

> 키·색인·카탈로그가 없으면 로컬 검색이 실패하고 KOSIS 라이브 검색으로 폴백한다.

---

## 2. 실행 방법

진입점은 [`agent_chat.py`](src/kosis_agent/agent_chat.py). 자기 폴더를 자동으로 `sys.path`에 넣으므로
**폴더째 직접 실행**하면 된다.

```bash
# 방법 A — 진입점 직접 실행 (권장)
./.venv/Scripts/python.exe src/kosis_agent/agent_chat.py

# 방법 B — 다른 스크립트에서 모듈로 쓸 때는 경로 지정
#   PYTHONPATH=src/kosis_agent 로 잡고 import
```

> ⚠️ 이 파이프라인은 이제 `src`가 아니라 **`src/kosis_agent`** 를 경로로 잡아야 한다.
> (2026-07-28 폴더 정리 시 `PROJECT_ROOT` 경로 `parents[1]→parents[2]` 로 보정 완료)

---

## 3. 내부 처리 흐름

```
질의(자연어 / 기사 문장)
  │
  ├─ agent_query_parser  연도·시점 파싱 (기준연도 2025, "전년"→base/target 자동)
  ├─ agent_slots         지표·한정어 슬롯 추출 + unresolved 판단(주어명사 기반)
  ├─ keyword_extractor   검색 키워드 추출
  │
  ├─ agent_map  ★검색·매핑 지휘
  │    1) 로컬 하이브리드 검색  (search_hybrid_v2: BM25 + dense + HyDE → RRF)
  │    2) 실패 시 KOSIS 라이브 검색 폴백 (k=20)
  │    3) getMeta 재점수  → feasible(조회 가능) + score(순위, SCORE_FN 교체 가능)
  │    4) rank_candidates 정렬: feasible → score → RRF tiebreak
  │    5) 상위 동점그룹만 남김 (명목/실질·전국/도시 등 맥락 정합)
  │    6) agent_reason: 리랭커(NCP) → RAG Reasoning(HCX) 로 최종 1표 확정 + 근거
  │    7) 부적합이면 다음 후보로 자동 재검색
  │
  ├─ agent_clarify       슬롯 미해소 시 재질의 / 자동 선택
  │
  └─ agent_pipeline      getData로 실제 수치 조회 → 비교 → 판정
       - 판정: 상대오차 ≤0.5% 일치 / ≤2% 대체로일치 / 그 외 불일치
       - 허용오차: 반올림(자리 기반) + 표본오차 RSE (kosis_statistical_grade)
       - agent_explain: KOSIS 원자료(수치·시점·출처) 근거로 설명 생성
```

### 모듈 역할 요약
| 파일 | 역할 |
|---|---|
| `agent_chat` | 진입점(대화 오케스트레이션) |
| `agent_query_parser` | 연도·시점 파싱 |
| `agent_slots` | 지표·한정어 슬롯 추출 |
| `keyword_extractor` | 검색 키워드 추출 |
| `agent_map` | 검색 + getMeta 재점수 + 표 선택 지휘 |
| `agent_reason` | 리랭커(재정렬) + RAG Reasoning(최종 선택) |
| `agent_clarify` | 재질의 / 자동 선택 |
| `agent_pipeline` | 조회·비교·판정 총괄 |
| `agent_explain` | 판정 설명 생성 |
| `table_ops` | 표 연산(증감·비율·합계) |
| `tolerance_judge` | 허용오차 판정 |
| `kosis_statistical_grade` | RSE(상대표준오차) 기준 |
| `kosis_call_tool` | KOSIS 호출 래퍼(get_data/get_meta 재사용) |
| `kosis_client` | KOSIS Open API 클라이언트 |

---

## 4. 판정 결과

| 판정 | 의미 |
|---|---|
| **일치(MATCH)** | 뉴스 수치가 KOSIS 실측과 허용오차 내 일치 |
| **대체로 일치** | 반올림·표본오차 감안 시 근사 일치 |
| **불일치(MISMATCH)** | 허용오차를 벗어남 |
| **판단불가** | 매핑 실패 / KOSIS에 해당 데이터 없음 / 검증 대상 외 주장 |

수치 비교는 LLM이 아니라 **결정론적 계산 모듈**(table_ops·tolerance_judge)이 수행한다.

---

## 5. 팀 공유 시 (다른 사람이 돌리려면)

Qdrant 로컬 색인은 **폴더 통째가 곧 DB**(서버 불필요). 아래만 있으면 각자 독립 실행된다.

1. `output/kosis_qdrant_v2/` (또는 v4) — 색인 폴더 (한 번 구운 걸 나눠 쓰면 재색인 불필요)
2. `data/kosis_catalog_*.jsonl` — 카탈로그
3. `src/kosis_agent/` — 코드
4. **각자 API 키**(.env) — 질의 임베딩·리랭커·RAG 호출에 필요

> 주의: Qdrant 로컬 모드는 한 번에 한 프로세스만 폴더를 연다(파일 락).
> 공유 드라이브는 전달용으로만 쓰고, 각자 로컬 디스크로 복사해 실행할 것.

---

## 6. 상태 (2026-07-28)

- 파일 병합 15→13(실측 폐포 14) 완료, 리랭커+RAG 라이브 연결·정상 작동 확인
- 200 골드셋 표본 평가: 매핑 정확도는 **카탈로그 커버리지**에 종속 → 26만(v4) 색인으로 개선 예정
- 후속: v4 색인 완성 후 검색 배선 v4 전환 → 재평가, 판단불가 게이트 강화
