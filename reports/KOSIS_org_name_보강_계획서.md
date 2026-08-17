# KOSIS 기관명(org_name) 결측 보강 계획서

작성일: 2026-08-09
대상 산출물: `data/kosis_catalog_v4.jsonl`, `data/kosis_org_names.json`
대상 코드: `src/kosis_catalog_builder.py`, `src/label_retrieval_eval_codex.py`, `src/source_scope_analysis.py`
근거: 2026-08-09 KOSIS OpenAPI 라이브 실측

---

## 0. 결론 세 줄

1. `kosis_catalog_v4.jsonl` 265,094건 중 **`org_name` 결측 170,902건 (64.5%)**.
2. 원인은 **두 개**다 — ① 카탈로그(7/29)가 기관명 사전(7/30)보다 먼저 만들어져 생긴 **조인 시점 문제**(단순 재조인으로 63,917건 = 24% 해결), ② 사전 자체가 **기초자치단체(시·군·구)를 아예 안 담고 있는 커버리지 문제**(106,985건 = 40.4% 잔존).
3. 해결책은 실측 완료됐다. **`statisticsSearch.do`에 `orgId` 필터를 걸면 응답에 `ORG_NM`이 그대로 온다.** 등장 기관 380개 × 1콜 = **약 2분이면 결측 0%**가 된다.

---

## 1. 현황 실측

### 1.1 결측 규모

| 지표 | 값 |
|---|---|
| 카탈로그 레코드 | 265,094 |
| `org_name` 결측 | **170,902 (64.5%)** |
| 카탈로그에 등장하는 `org_id` | 380개 |
| 그중 이름 없는 `org_id` | 295개 |
| `data/kosis_org_names.json` 항목 수 | **181개** |

결측 상위 기관: `217`(10,638) `203`(5,842) `213`(4,414) `218`(3,916) `212`(3,430) `202`(3,071) … 전부 광역·기초자치단체다.

### 1.2 원인 ① — 조인 시점 (24%)

```
data/kosis_catalog_v4.jsonl   2026-07-29 10:24
data/kosis_org_names.json     2026-07-30 17:18   ← 카탈로그보다 나중
```

`kosis_catalog_builder.py:146`은 `org_names.get(org_id)`로 단순 조인한다. 카탈로그를 빌드할 당시 사전에 없던 기관은 그대로 `null`이 됐다. **지금 사전으로 재조인만 해도 63,917건이 채워진다** (217 경상남도, 203 대구광역시, 213 충청남도 등 광역단체 대부분).

### 1.3 원인 ② — 사전 커버리지 (40.4%)

`kosis_org_names.json`은 `MT_OTITLE` 뷰의 **최상위 노드 182개를 그대로 받아 적은 것**이다 (실측 확인: 파일 키 집합과 `list_tables(vw_cd="MT_OTITLE", parent_id="")` 결과가 3건 차이로 일치 — 파일에만 `967`, API에만 `432`·`182`).

그런데 `MT_OTITLE` 최상위는 중앙행정기관·공공기관·**광역**자치단체까지만 있다. **기초자치단체(시·군·구)는 광역단체 노드의 자식으로 한 단계 아래에 있다.** 그래서 사전에 없고, 재조인 후에도 **203개 기관 / 106,985건(40.4%)**이 남는다:

`205`(2,274) `711`(2,167) `714`(1,714) `619`(1,667) `620`(1,650) `791`(1,459) `611`(1,307) …

### 1.4 해결 경로 3가지 — 전부 실측함

#### 경로 A. `MT_OTITLE` 2단 크롤 (시군구 노드 수집)

광역단체 노드 16개의 자식을 조회하면 시군구 노드 **221개**가 나오고, `LIST_ID`의 마지막 언더스코어 뒤가 곧 `org_id`다.

```
parentListId=217 →  217A_797  김해시     → org_id 797
                    217A_791  창원시     → org_id 791
parentListId=201 →  201_201A_523  강남구 → org_id 523
```

- 호출: 16회 (약 10초)
- 성과: 미해결 205개 중 **178개 해결**
- 잔여 27개: `146` `432` `581~585` `731~752` — 광주·전남 관련 기관. 2026년 **전남광주통합특별시** 출범으로 광역 노드(`205` 광주, `215` 전북)가 `MT_OTITLE` 최상위에서 빠져 자식에 도달할 수 없다.

#### 경로 B. `statisticsSearch.do` + `orgId` 필터 ← **권장**

검색 API 응답에 `ORG_NM`이 필드로 포함된다. `orgId` 파라미터로 기관을 못 박고 아무 키워드나 주면 그 기관의 표가 나오고, 거기서 이름을 읽는다.

실측(경로 A가 못 채운 27개 포함, 전부 성공):

| orgId | 반환된 ORG_NM |
|---|---|
| 731 | 전남광주통합특별시 목포시 |
| 581 | 전남광주통합특별시 동구 |
| 205 | 전남광주통합특별시 |
| 215 | 전남광주통합특별시 |
| 146 | 해양수산부 |
| 432 | 대한법률구조공단 |

- 호출: **기관 1개당 1회** → 380개 전부 해도 **380콜 ≈ 2분**
- 장점: 계층 구조를 몰라도 되고, KOSIS가 실제로 표에 붙여 놓은 공식 명칭이 그대로 온다
- 부수 확보 필드: `STAT_NM`(조사명), `MT_ATITLE`(분류경로), `CONTENTS`, `STRT_PRD_DE`/`END_PRD_DE`

#### 경로 C. `MT_OTITLE` 전수 크롤

표가 어느 기관 노드 아래에 있는지를 **표 단위로** 확정한다. 다만 `org_id`는 이미 모든 표에 붙어 있으므로 기관명 채우기에는 과잉이다. 이건 **`reports/KOSIS_전체표_크롤링_계획서.md` P5(커버리지 교차검증)에서 하고, 여기서는 검증용으로만 쓴다.**

---

## 2. 권장 설계

### 2.1 3단 파이프라인

```
1차: 경로 B — 등장 org_id 전부(380개) 1콜씩 → 정식 기관명 확보 (권위 소스)
2차: 경로 A — 16콜로 시군구 이름을 따로 받아 1차 결과와 대조 → 불일치 리포트
3차: 잔여 결측이 있으면 트리 path에서 후보 추출("경기도안산시기본통계" → 안산시)
     → 자동 채움 금지. 사람이 확인할 리뷰 목록으로만 출력
```

2차를 두는 이유: 경로 B는 "그 기관 표 중 아무거나 1건"의 `ORG_NM`을 믿는 방식이라, 표 단위로 값이 흔들릴 가능성을 배제할 수 없다. 두 소스가 일치하면 그대로 확정, 다르면 사람이 본다.

### 2.2 사전 스키마 확장

지금은 `{"217": "경상남도"}` 형태의 평면 맵이다. 출처와 계층을 남기도록 바꾼다.

```json
{
  "schema_version": "org-names-v2",
  "built_at": "2026-08-09T...",
  "orgs": {
    "797": {
      "org_name": "김해시",
      "org_name_full": "경상남도 김해시",
      "org_level": "basic",
      "org_parent_id": "217",
      "source": "statisticsSearch.orgId",
      "cross_checked": true
    }
  }
}
```

| 필드 | 값 | 용도 |
|---|---|---|
| `org_level` | `central` / `metro` / `basic` / `public` / `special` | `source_scope` 분류(`src/source_scope_analysis.py`)에서 "중앙부처 발표 vs 지자체 발표"를 가르는 신호 |
| `org_parent_id` | 기초 → 광역 | 지역 단위 필터·집계 |
| `source` / `cross_checked` | 근거 추적 | 감사 |

> 하위호환: 기존 코드 3곳(`kosis_catalog_builder.py`, `label_retrieval_eval_codex.py`, `source_scope_analysis.py`)이 평면 맵을 전제로 `.items()` / `.get()` 한다. **평면 맵(`kosis_org_names.json`)을 v2에서 파생 생성해 함께 유지**하고, 호출부를 순차 이관한다.

### 2.3 의사 기관 코드

전체표 크롤링 계획서 P2에서 들어올 표들은 `org_id`가 기관이 아니다 — 실측: 광복이전통계 `999`, 대한민국통계연감 `999S`. 이런 코드는 검색 API로 이름이 안 나오므로 **상수 매핑을 코드에 명시**한다.

```python
PSEUDO_ORGS = {
    "999":  {"org_name": "광복이전통계(조선총독부 통계연보 등)", "org_level": "special"},
    "999S": {"org_name": "대한민국통계연감",                    "org_level": "special"},
}
```

### 2.4 카탈로그 반영 — 전체 재빌드 대신 경량 패치

`kosis_catalog_v4.jsonl`은 533MB다. `org_name`만 바꾸자고 전체를 다시 만들 필요가 없다.

```
src/patch_catalog_org_name.py   # 스트리밍 read → org_name만 교체 → 임시파일 → 원자적 교체
```

`doc_meta_text` / `doc_item_index`는 건드리지 않으므로 **벡터 재색인이 불필요**하다(임베딩 캐시 `kosis_embedding_cache.jsonl` 그대로 유효). 단, Qdrant payload의 `org_name`(`src/kosis_v2_indexer.py:226`)은 **payload만 갱신**하면 된다.

> 전체표 크롤링 계획서 P9에서 카탈로그 v5를 새로 빌드한다면 그때는 재빌드에 흡수시킨다. 그전에 검색 품질 평가를 돌려야 한다면 경량 패치로 먼저 막는다.

---

## 3. 실행 단계

| Phase | 내용 | 호출 | 시간 | 산출물 |
|---|---|---|---|---|
| **Q0** | 등장 `org_id` 전수 추출(트리 + 신규 뷰 포함) | 0 | 10분 | `org_id` 목록 |
| **Q1** | `src/build_org_names.py` 구현 — 경로 B(주), 경로 A(교차검증), 재시도·레이트리밋 | — | 0.5일(개발) | 신규 스크립트 |
| **Q2** | 실행 → `data/kosis_org_names_v2.json` + 평면 맵 파생 | ~400 | **~3분** | 사전 v2 |
| **Q3** | 불일치·잔여 결측 리뷰 목록 검토 | 0 | 0.5시간 | 리뷰 리포트 |
| **Q4** | `src/patch_catalog_org_name.py` 로 카탈로그 패치 | 0 | 20분(I/O) | `kosis_catalog_v4.jsonl` 갱신 |
| **Q5** | Qdrant payload `org_name` 갱신 | 0 | 30분 | 색인 |
| **Q6** | `org_level` 도입 → `source_scope_analysis.py` 재실행, 지표 변화 확인 | 0 | 0.5일 | 분석 리포트 |
| **Q7** | 감사 — `tests/test_audit_kosis_catalog_v4.py`에 `org_name` 결측률 0% 단언 추가 | 0 | 0.5일 | 테스트 |

**총 API 호출 약 400회 / 실행 시간 1시간 이내, 개발 공수 1.5~2일.**

Q1~Q4만 하면 **결측 64.5% → 0%**가 된다. Q5 이후는 후속 활용이다.

---

## 4. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 경로 B가 키워드 `'통계'` 매칭에 의존 | 그 기관 표에 해당 문자열이 없으면 0건 반환 | 폴백 키워드 체인 사용 — 우리 트리에서 **그 기관의 실제 `tbl_nm`을 하나 뽑아** 키워드로 씀(반드시 매칭됨) |
| 검색 API도 분당 200콜 합산 | 크롤과 동시 실행 시 충돌 | 크롤 미실행 시간대에 단독 실행(380콜이라 2분이면 끝남) |
| `전남광주통합특별시` 같은 행정구역 개편 | 기관명이 시점에 따라 다름 | `built_at` 기록 + 분기별 재실행. 과거 이름은 덮어쓰되 diff를 리포트로 남김 |
| 평면 맵을 쓰는 기존 코드 3곳 | 스키마 변경 시 breakage | v2와 평면 맵 **병행 유지**, 호출부 순차 이관 |
| 533MB 파일 패치 중 중단 | 파일 손상 | 임시파일 → `Path.replace()` 원자적 교체 (`kosis_meta_enricher.write_checkpoint` 패턴) |

---

## 5. 완료 기준 (DoD)

- [ ] `data/kosis_org_names_v2.json` 항목 수 ≥ 카탈로그에 등장하는 `org_id` 수 (현재 380, 신규 뷰 반영 시 증가)
- [ ] `kosis_catalog_*.jsonl`의 `org_name` 결측 **0건**
- [ ] 경로 A·B 교차검증 불일치 건이 전부 리뷰·확정됨
- [ ] 무작위 30건을 KOSIS 웹(`statHtml.do?orgId=…&tblId=…`)에서 직접 열어 기관명 대조, 정확도 100%
- [ ] `org_level` 분포가 상식과 일치(기초 > 광역 > 중앙 순으로 표 수가 많을 것)
- [ ] 감사 테스트에 결측률 단언 추가, CI 통과

---

## 부록. 실측 재현 명령

```bash
venv/Scripts/python.exe -c "import sys,requests; sys.path.insert(0,'.'); from src.kosis_client import API_KEY,SEARCH_URL,_loads_lenient; d=_loads_lenient(requests.get(SEARCH_URL,params={'method':'getList','apiKey':API_KEY,'searchNm':'통계','orgId':'731','startCount':1,'resultCount':1,'format':'json','jsonVD':'Y'}).text); print(d[0]['ORG_ID'], d[0]['ORG_NM'])"
```
