# KOSIS 메타데이터 크롤링·활용 점검 보고서

- 작성일: 2026-07-30
- 대상: `kosis_client.py` → 크롤링(`kosis_table_tree.json`, `kosis_table_meta.jsonl`) → 카탈로그(`kosis_catalog_builder.py`) → 벡터 색인(`kosis_v2_indexer.py`) 전 구간
- 근거: 소스코드 실측 + `KOSIS 공유서비스 개발가이드.pdf`(158p) 원문
- 목적: KOSIS가 제공하는 전체 메타데이터 중 **무엇을 크롤링했고, 무엇을 벡터DB에서 실제로 썼는지**, 그리고 **가지고 있으나 안 쓴 데이터 / 안 크롤링한 유용 데이터**를 식별

---

## 0. 요약 (TL;DR)

1. **크롤링은 충실**했다: `getMeta`의 ITM(분류·항목)·PRD(시점)·UNIT(단위)을 표당 수집해 `kosis_table_meta.jsonl`(515건 확인)에 저장.
2. **카탈로그 설계도 좋다**: dense용 `doc_meta_text` / BM25용 `doc_item_index` / 필터용 payload로 3분할.
3. **그러나 색인 단계에서 대량 유실**: `kosis_v2_indexer.py`는 dense 벡터 2개와 payload 7개 필드만 저장하고, **BM25(sparse)·units·period·dimensions를 통째로 버린다.**
4. **트리 단계에서 공짜 데이터를 버렸다**: 통계목록 API가 주는 `SEND_DE`(최종갱신일)·`REC_TBL_SE`(추천 통계표 여부)를 저장하지 않았다.
5. **서술형 메타 금맥을 미사용**: `getMeta` CMMT(주석)와 **별도 API인 통계설명(`statisticsExplData.do`)**의 조사목적·용어해설 등을 전혀 안 썼다. 이 중 상당수는 **이미 호출 중인 통합검색 API(`statisticsSearch.do`)의 `CONTENTS`/`ITEM03` 응답만 파싱해도** 얻을 수 있다.

---

## 1. `kosis_client.py`가 크롤링할 수 있는 메타데이터

클라이언트는 4개 엔드포인트를 감싼다. 통계표 메타의 원천은 사실상 `get_meta()`(getMeta) 하나이며, 나머지는 목록·검색·수치 조회다.

| 함수 | 엔드포인트 | 얻는 것 |
|---|---|---|
| `list_tables()` | `statisticsList.do` | 분류 트리 노드 및 리프 표(ORG_ID/TBL_ID/TBL_NM/STAT_ID 등) |
| `search_tables()` | `statisticsSearch.do` | 키워드 검색 결과(RANK/DATE 순) |
| `get_data()` | `statisticsParameterData.do` | 실제 통계 수치 |
| `get_meta()` | `statisticsData.do` (getMeta) | `meta_type`별 표 메타 (TBL/ITM/CMMT/UNIT/SOURCE/PRD/WGT/NCD/ORG) |

> 코드 주석에는 getMeta 8종이 적혀 있고, 가이드 2.5절에서 TBL·ORG·PRD·ITM·CMMT·UNIT·SOURCE·WGT·NCD 로 실제 확인된다.

---

## 2. 내가 실제로 크롤링(추출)한 메타데이터

### 2.1 `kosis_table_tree.json` — `kosis_tree_crawler.py` (통계목록 BFS)

리프 표마다 저장한 필드:

- `org_id`, `tbl_id`, `tbl_nm`, `stat_id`, `path`(카테고리 이름 경로)

### 2.2 `kosis_table_meta.jsonl` — `kosis_meta_enricher.py` (getMeta ITM+PRD+조건부 UNIT)

실측 515건 전부 아래 필드 보유:

- `dimensions`: `obj_id, obj_nm, values[{id, nm, up_id(상위코드), sn(순번)}], value_count`
- `items`: `itm_id, itm_nm, unit_nm`
- `units`
- `periods`: `[{prd_se, start, end}]`, `period_types`, `latest_period`
- (버린 값) 영문명 `OBJ_NM_ENG/ITM_NM_ENG/UNIT_ENG_NM`, `UNIT_ID` — 한국어 검색에 불필요

> **호출 안 한 getMeta type: CMMT(주석), SOURCE(출처), NCD(자료갱신일), WGT(가중치).**

---

## 3. 벡터DB 파이프라인이 실제로 사용한 데이터

### 3.1 `kosis_catalog_builder.py` → `kosis_catalog_v2.jsonl` (활용도 높음)

크롤링한 메타를 거의 다 활용해 3분할한다.

- `doc_meta_text` (임베딩용) = `tbl_name` + `category_paths` + **차원명(obj_nm)**
- `doc_item_index` (BM25용) = 항목명 + **차원값(nm)**
- payload = `org_id/org_name`, `stat_id`, `category_paths`, `dimensions(obj_nm/value_count)`, `items`, `units`, `period_types`, `latest_period`

### 3.2 `kosis_v2_indexer.py` (Qdrant 색인) — **여기서 대량 유실**

카탈로그가 만들어 둔 것 중 극히 일부만 색인한다.

- 벡터: `doc_meta_vector`←`doc_meta_text`, `tbl_name_vector`←`tbl_name` (**둘 다 dense**)
- payload: `table_key, org_id, org_name, tbl_id, tbl_name, doc_meta_text, catalog_version` **뿐**

---

## 4. 가지고 있는데 벡터DB에서 안 쓴 데이터 ⚠️

| 데이터 | 어디까지 만들었나 | 색인 여부 | 영향 |
|---|---|---|---|
| **`doc_item_index` (BM25/sparse)** | 카탈로그에 생성 | ❌ **sparse 벡터 코드 자체가 indexer에 없음** | "종로구 인구" 같은 차원값 토큰 매칭 불가 |
| `units` | 카탈로그 payload | ❌ 미포함 | "건수(명) vs 비율(%)" 구분 필터 불가 |
| `period_types` / `latest_period` | 카탈로그 payload | ❌ 미포함 | 최신 시점·주기 프리필터 불가 |
| `dimensions` / `items` | 카탈로그 payload | ❌ 미포함 | 검색 후 표 식별·리랭킹 근거 접근 불가 |
| `stat_id` / `category_paths` | 카탈로그 payload | ❌ 미포함 | 조사 단위 그룹핑·경로 표시 불가 |
| 차원값 코드 `id/up_id/sn` | meta.jsonl에는 있음 | ❌ 카탈로그에서 `nm`만 남기고 제거 | 검색→실제 수치조회(`get_data` objL1)로 잇는 코드 단절 |

**핵심**: 카탈로그 주석은 "dense/sparse 요구가 반대라 둘로 갈랐다"고 설계했으나, **indexer에 sparse 색인 자체가 구현돼 있지 않다.** 필터용 payload도 대부분 실려 있지 않다.

---

## 5. KOSIS 전체 메타데이터 인벤토리 (개발가이드 PDF 기준)

### 5.1 통계목록 `statisticsList.do` (2.1)

출력 필드: `VW_CD, VW_NM, LIST_ID, LIST_NM, ORG_ID, TBL_ID, TBL_NM, STAT_ID, `**`SEND_DE(최종갱신일)`**`, `**`REC_TBL_SE(추천 통계표 여부)`**

> 트리 크롤러는 `SEND_DE`·`REC_TBL_SE`를 **저장하지 않고 버렸다** (아래 6절).

### 5.2 통계자료 메타 `statisticsData.do?method=getMeta` (2.5)

| type | 주요 출력 필드 | 우리 사용 |
|---|---|---|
| TBL | TBL_NM (+영문) | tbl_nm은 트리에서 확보 |
| ORG | ORG_NM (기관 국문명) | `kosis_org_names.json`로 별도 확보 |
| PRD 수록정보 | PRD_SE(수록주기), PRD_DE(수록시점) | ✅ 사용 |
| ITM 분류항목 | OBJ_ID, OBJ_NM, ITM_ID, ITM_NM, UP_ITM_ID, OBJ_ID_SN, UNIT_ID, UNIT_NM | ✅ 사용 |
| UNIT 단위 | UNIT_NM | ✅ 조건부 사용 |
| **CMMT 주석** | **CMMT_NM(주석유형), CMMT_DC(주석 3000자)**, OBJ/ITM scope | ❌ **미사용** |
| **SOURCE 출처** | **JOSA_NM(조사명), DEPT_NM(담당부서), DEPT_PHONE, STAT_ID** | ❌ **미사용** |
| **NCD 자료갱신일** | ORG_NM, TBL_NM, PRD_SE, PRD_DE, **SEND_DE(자료갱신일)** | ❌ **미사용** |
| WGT 가중치 | C1~C8, ITM, WGT_CO | ❌ (검색엔 불필요) |

### 5.3 통계설명 `statisticsExplData.do` (2.4) — ⭐코드에 아예 없는 별도 API

`statId`(통계조사 ID) 단위로 **약 27개 서술형 필드** 제공:

`statsNm(조사명), statsKind(작성유형), basisLaw(법적근거), `**`writingPurps(조사목적)`**`, statsPeriod(조사주기), writingSystem(조사체계), statsField(통계활용분야), examinObjrange(조사대상범위), examinObjArea(조사대상지역), josaUnit(조사단위·규모), applyGroup(적용분류), `**`josaItm(조사항목)`**`, pubPeriod(공표주기), pubExtent(공표범위), pubDate(공표시기), dataUserNote(자료이용자 유의사항), `**`mainTermExpl(주요 용어해설)`**`, dataCollectMth(자료수집방법), examinHistory(조사연혁), confmNo/confmDt(승인번호/일자)` 등

### 5.4 통합검색 `statisticsSearch.do` (2.6) — ⭐검색 1회로 풍부한 메타

`search_tables()`가 **이미 호출**하지만 응답 필드를 대부분 버린다:

`ORG_NM, STAT_NM(조사명), `**`MT_ATITLE/FULL_PATH_ID(통계표 위치=카테고리 경로)`**`, `**`CONTENTS(통계표 주요내용 CLOB), ITEM03(통계표 주석 CLOB)`**`, STRT_PRD_DE/END_PRD_DE(수록기간), REC_TBL_SE(추천 통계표 여부), LINK_URL/TBL_VIEW_URL`

> 별도로 2.7 지표(indicator, `pkNumberService.do` 등) API가 있으나, 통계표와 다른 '지표' 축이라 이 프로젝트 대상 아님.

---

## 6. 안 크롤링한 것 중 벡터DB에 쓸 만한 데이터

### A. 이미 가진 데이터인데 색인만 안 함 (구현만 하면 즉시 활용)
- `doc_item_index`(BM25/sparse), `units`, `period_types`, `latest_period`, `dimensions`, `stat_id`, `category_paths` — 4절 참고.

### B. 한 번 받았다가 버린 것 (통계목록 응답에 원래 있었음)
- **`SEND_DE`(최종갱신일)** — 통계목록에 함께 오던 값. **getMeta 추가 호출 없이** 최신성 필터/정렬에 바로 쓸 수 있었다.
- **`REC_TBL_SE`(추천 통계표 여부)** — KOSIS가 지정한 '대표 표' 신호. 검색 랭킹 부스팅에 유용.

### C. 크롤링 안 했으나 가치 큰 서술형 메타
1. **CMMT `CMMT_DC`(주석) + 통계설명 `writingPurps`/`mainTermExpl`(조사목적·용어해설)** — 표명만으로 안 잡히는 의미를 임베딩에 넣는 **최우선 후보**. 검색 정확도 개선에 직결.
2. **SOURCE `JOSA_NM`/`DEPT_NM`(조사명·담당부서)** — 출처 표기·신뢰도 필터.
3. **NCD `SEND_DE`** — B의 SEND_DE와 동일 목적(최신성). 트리에서 못 받았으면 여기서 보강 가능.

### 💡 지름길
**통합검색 API 한 번 호출로 `CONTENTS`(주요내용) + `ITEM03`(주석) + 경로 + 수록기간 + 추천여부를 동시에** 받는다. 즉 C-1과 B의 상당 부분을 표당 getMeta 여러 번 대신 **`search_tables()` 응답 파싱만으로** 확보 가능하다. 현재는 이 응답을 전혀 파싱하지 않고 버리는 중.

---

## 7. 개선 권고 (우선순위)

| 순위 | 작업 | 근거 | 난이도 |
|---|---|---|---|
| 1 | **indexer에 sparse(BM25) 벡터 + 필터 payload 복원** | 카탈로그가 이미 `doc_item_index`·units·period를 만들어 둠. 데이터 재크롤링 불필요 | 낮음 |
| 2 | **트리 크롤러에 `SEND_DE`·`REC_TBL_SE` 추가** | 통계목록 응답에 원래 포함, 공짜 | 낮음 |
| 3 | **서술형 메타 크롤링**: CMMT + 통계설명(조사목적·용어해설), 또는 통합검색 `CONTENTS`/`ITEM03` 파싱 | 검색 정확도 개선 효과 최대 | 중간 |

---

## 부록: 근거 위치

- 통계목록 출력 필드: 개발가이드 p.19 (2.1.3.1 JSON)
- getMeta type별 출력: 개발가이드 p.138~146 (2.5.2)
- 통계설명 API: 개발가이드 p.111~116 (2.4.3)
- 통합검색 출력 필드: 개발가이드 p.148 (2.6.2.1)
- 코드: `src/kosis_client.py`, `src/kosis_tree_crawler.py`, `src/kosis_meta_enricher.py`, `src/kosis_catalog_builder.py`, `src/kosis_v2_indexer.py`
