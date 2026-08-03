# KOSIS 메타데이터 크롤링·활용 점검 보고서

- 작성일: 2026-07-30 / 개정: 2026-08-03 (실측 검증 반영 — §6 신설, §5 대폭 축소)
- 대상: `kosis_client.py` → 크롤링(`kosis_table_tree.json`, `kosis_table_meta_v4.jsonl`) → 카탈로그(`kosis_catalog_builder.py`) → 벡터 색인(`kosis_v2_indexer.py`)
- 근거: `KOSIS 공유서비스 개발가이드.pdf`(158p) 원문 + **실제 API 호출·데이터 실측**
- 목적: KOSIS 전체 메타데이터를 분모로 놓고, 무엇을 수집했고 무엇을 쓰는지, **추가 크롤링이 실제로 필요한지** 판정

---

## 0. 요약 (TL;DR)

1. **추가 크롤링은 사실상 불필요하다.** 초판에서 유망하다고 본 CMMT·통합검색·통계설명을 실측한 결과, 대부분 **이미 보유한 데이터의 중복이거나 수집 불가**였다 (§6).
2. **남은 가치는 3가지뿐이고 모두 저렴하다**: `org_name` 완성(2분), `SEND_DE`(트리 재크롤 2.5h), 통계설명 서술형 4필드(7분).
3. **진짜 문제는 수집이 아니라 적재다.** 카탈로그 용량의 62%(`doc_item_index`)가 색인되지 않고, `units`·`periods`·`dimensions` payload가 전부 누락됐다 (§4).
4. **검증 단계 연결은 이미 가능하다.** v4 메타의 차원 코드로 `get_data()` 실제 호출에 성공했다 — 단 카탈로그가 코드를 버려 벡터DB 경로로는 불가 (§6.7).
5. **복구 불가능한 결손 1건**: 단위(`units`)가 29.1% 비어 있는데 KOSIS 원본 부재(`err:30`)라 재크롤로도 안 채워진다.

---

## 1. KOSIS 메타데이터 전체 인벤토리

이 절이 이후 격차 분석의 **분모**다. 두 열이 핵심이다.

- **클라이언트 지원** — `src/kosis_client.py`에 호출 함수가 있는가
- **우리 크롤링** — 실제로 저장했는가 (상세 §2)

### 1.0 API 계열

| API | 엔드포인트 | 클라이언트 지원 | 우리 크롤링 |
|---|---|---|---|
| 통계목록 (2.1) | `statisticsList.do` | ✅ `list_tables()` | ⚠️ 일부 필드만 |
| 통계자료 메타 (2.5) | `statisticsData.do` (getMeta) | ✅ `get_meta()` — type 9종 | ⚠️ 3종만 |
| 통계설명 (2.4) | `statisticsExplData.do` | ❌ 함수 없음 | ❌ |
| 통합검색 (2.6) | `statisticsSearch.do` | ✅ `search_tables()` | ❌ 응답 미파싱 |

`get_data()`(`statisticsParameterData.do`)는 수치 조회용. **이 URL은 가이드 PDF에 없다** — 문서 버전 차이로 보이며 확인 필요.
2.7 지표(`pkNumberService.do` 등 8종)는 출력 키가 `statJipyoId`이고 **`tblId`/`orgId`/`statId`가 전혀 없어** 통계표와 조인 불가 → 대상 제외 (p.149~158 전수 확인).

### 1.0.1 서비스뷰 (VW_CD) — 크롤링 범위를 결정하는 축

`statisticsList.do`의 필수 입력값. 같은 통계표 창고를 여러 방식으로 진열한 메뉴판이다.

| VW_CD | VW_NM | 루트 수 | 크롤링 | 판정 |
|---|---|---|---|---|
| **`MT_ZTITLE`** | 주제별 | 30 | ✅ **이것만** | — |
| `MT_OTITLE` | 기관별 | 181 | ❌ | 기관명 사전 소스로만 사용 |
| `MT_ATITLE01/02` | 지역통계(주제/기관별) | 15 / 71 | ❌ | 주제별 `V 지역통계`와 중복 추정, 미확인 |
| `MT_STOP_TITLE` | 작성중지통계 | 177 | ❌ | **불필요 확정** (§6.5) |
| `MT_CHOSUN_TITLE` | 광복이전(1908~43) | 14 | ❌ | 불필요 |
| `MT_HANKUK_TITLE` | 대한민국통계연감 | 16 | ❌ | 불필요 |
| `MT_GTITLE01` | e-지방지표 | 12 | ❌ | 지표 축(별개) |
| `MT_ETITLE` | 영문 KOSIS | 29 | ❌ | 한국어 검색에 불필요 |
| `MT_RTITLE` | **국제기구별 통계** | — | ❌ | **가이드 9종 목록에 없음** — 통합검색 응답에서 발견 |

### 1.1 통계목록 `statisticsList.do` (가이드 p.19)

| 출력 필드 | 설명 | 우리 크롤링 |
|---|---|---|
| `ORG_ID` | 기관코드 | ✅ |
| `TBL_ID` / `TBL_NM` | 통계표ID / 명 | ✅ |
| `STAT_ID` | 통계조사ID | ✅ |
| `LIST_ID` / `LIST_NM` | 목록ID / 명 | ✅ (경로 조립에 사용) |
| `VW_CD` / `VW_NM` | 서비스뷰 | ❌ (단일 뷰라 불필요) |
| **`SEND_DE`** | **최종갱신일** | ❌ **버림 — §5-B** |
| **`REC_TBL_SE`** | **추천 통계표 여부** | ❌ 버림 (가치 낮음, §6.4) |

⚠️ `SEND_DE` 실측 형식은 `2026-07-02`(**10자**). 가이드 스펙 `VARCHAR2(8)`과 다르므로 파싱 주의.

### 1.2 통계자료 메타 getMeta (가이드 p.138~146) — 정확히 9종

| type | 주요 출력 필드 | 우리 크롤링 |
|---|---|---|
| TBL | `TBL_NM` | △ 트리에서 확보 |
| **ORG** | **`ORG_NM`** | △ 사전으로 우회 — **직접 호출이 정답 (§6.6)** |
| PRD | `PRD_SE`, `PRD_DE` | ✅ |
| ITM | `OBJ_ID, OBJ_NM, ITM_ID, ITM_NM, UP_ITM_ID, OBJ_ID_SN, UNIT_ID, UNIT_NM` | ✅ |
| UNIT | `UNIT_NM` | ✅ 조건부 |
| CMMT | `CMMT_NM`(주석유형), `CMMT_DC`(주석) | ❌ **가치 낮음 (§6.3)** |
| SOURCE | `JOSA_NM, DEPT_NM, DEPT_PHONE, STAT_ID` | ❌ 미사용 |
| NCD | `SEND_DE` 등 | ❌ 미사용 (트리 재크롤이 더 쌈) |
| WGT | `C1~C8, ITM_ID, WGT_CO` | ❌ 검색에 불필요 |

### 1.3 통계설명 `statisticsExplData.do` (가이드 p.111~116)

**`statId`(조사) 단위**로 약 27개 서술형 필드. 조사는 1,256개뿐이라 **7분이면 전량 수집** (§6.2).

`statsNm, statsKind, basisLaw, writingPurps(조사목적), statsPeriod, writingSystem, statsField, examinObjrange(조사대상범위), examinObjArea, josaUnit, applyGroup, josaItm(조사항목), pubPeriod, pubExtent, pubDate, dataUserNote(유의사항), mainTermExpl(용어해설), dataCollectMth, examinHistory, confmNo/confmDt` 등

### 1.4 통합검색 `statisticsSearch.do` (가이드 p.148)

출력: `ORG_ID, ORG_NM, TBL_ID, TBL_NM, STAT_ID, STAT_NM, VW_CD, MT_ATITLE, FULL_PATH_ID, CONTENTS, STRT_PRD_DE, END_PRD_DE, ITEM03, REC_TBL_SE, TBL_VIEW_URL, LINK_URL, STAT_DB_CNT, QUERY`

> ⚠️ **`searchNm`이 필수라 전수 열거가 불가능하다** (§6.1). 오프라인 대량 수집 소스로 쓸 수 없고, 질의 시점(online)에만 유효하다.

---

## 2. 실제로 크롤링한 메타데이터

### 2.1 `kosis_table_tree.json` (통계목록 BFS, `MT_ZTITLE`만)

리프 표당: `org_id`, `tbl_id`, `tbl_nm`, `stat_id`, `path`

→ §1.1 대비 `SEND_DE`·`REC_TBL_SE` 누락. 유니크 표 **265,094**, 유니크 조사 **1,256**.

### 2.2 `kosis_table_meta_v4.jsonl` (getMeta ITM+PRD+조건부 UNIT)

265,094건 중 `ok` **265,082** / `error` 12.

- `dimensions`: `obj_id, obj_nm, values[{id, nm, up_id, sn}], value_count`
- `items`: `itm_id, itm_nm, unit_nm`
- `units`, `unit_source`
- `periods`: `[{prd_se, start, end}]`, `period_types`, `latest_period`
- (버림) 영문명, `UNIT_ID`

**결손 실측**

| 항목 | 결손 | 원인 |
|---|---|---|
| `units` | **77,143 (29.1%)** | **KOSIS 원본 부재** (`err:30` 77,136건) — 재크롤 불가 |
| `period_types` / `latest_period` | 0 | — |
| `dimensions` | 17 | — |
| `items` | 26 | — |

`prd_se` 종류 11가지: `년`(81,390) `3년`(14,909) `2년`(11,858) `5년`(8,811) `월`(2,246) `분기`(1,980) `반기`(551) `10년`(283) `부정기`(93) `4년`(85) `일`(44). **`end` 형식이 4/7/8자로 뒤섞임** → `latest_period` 문자열 max는 부정확(§4).

차원 개수 분포: 1개 29.5% / 2개 58.7% / 3개 9.9% / 4개 이상 1.9% → **표의 70%가 2개 이상 차원 조합을 요구**.

---

## 3. 벡터DB 파이프라인이 실제로 사용한 데이터

### 3.1 `kosis_catalog_builder.py` → 카탈로그 (설계는 좋음)

- `doc_meta_text` (임베딩용) = `tbl_name` + `category_paths` + **차원명**
- `doc_item_index` (BM25용) = 항목명 + **차원값**
- payload = `org_id/org_name`, `stat_id`, `category_paths`, `dimensions(obj_nm/value_count)`, `items`, `units`, `period_types`, `latest_period`

### 3.2 `kosis_v2_indexer.py` (Qdrant 색인) — 대량 유실

- 벡터: `doc_meta_vector`←`doc_meta_text`, `tbl_name_vector`←`tbl_name` (둘 다 dense)
- payload: `table_key, org_id, org_name, tbl_id, tbl_name, doc_meta_text, catalog_version` **뿐**

**실측(표본 40,000행): 색인기가 읽는 필드는 전체 용량의 8.8%, 나머지 91.2%를 읽고 버린다.**

### 3.3 설계 변경 이력 (nayeon 브랜치 `archive/260722`)

| | v1 | v2 (2026-07-22~23) |
|---|---|---|
| BM25 문서 | **`doc_item_index`** | **`doc_meta_text`** |
| BM25 범위 | 265,094 | 515 |
| Dense 범위 | 1,000 | 515 |

**당초 설계(dense=`doc_meta_text` / sparse=`doc_item_index`)는 v1에 실제로 구현돼 있었고, v2에서 의도적으로 변경됐다.** 다만 변경 사유는 검색 품질이 아니라 **경로 간 검색 범위 통일**이었고, Recall 비교 근거는 없다(골드셋 부재).

부작용 실측(300건): BM25가 빈 결과 — B2 26건, **B4 65건(21.7%)**. Dense는 0건. 보고서도 원인을 "형태소가 표명·분류·차원에 정확히 존재할 때만 점수를 얻는다"로 기록.

---

## 4. 수집했는데 벡터DB에서 안 쓴 데이터 ⚠️

| 데이터 | 상태 | 영향 |
|---|---|---|
| **`doc_item_index`** | 카탈로그 용량 **62.4%**, 색인 안 됨 | **"종로구" 토큰이 인덱스에 없음** — 지역·연령·성별 검색 불가 |
| `units` | payload 미포함 | 명/% 구분 필터 불가 |
| `periods`/`period_types`/`latest_period` | payload 미포함 | 시점 프리필터 불가 |
| `dimensions`/`items` | payload 미포함 | 표 식별·리랭킹 근거 접근 불가 |
| `stat_id`/`category_paths` | payload 미포함 | 조사 단위 그룹핑 불가 |
| 차원값 코드 `id/up_id/sn` | 카탈로그가 `nm`만 남기고 제거 | **검증 단계(`get_data`) 연결 단절** |

**추가 버그**: `latest_period = max(ends)`가 문자열 비교라 형식 혼재 시 부정확. 단 `periods` 원본이 보존돼 **오프라인 재계산으로 수정 가능**.

---

## 5. 추가 크롤링이 실제로 필요한 것 (실측 후 대폭 축소)

### ✅ 할 가치가 있는 것 — 3건, 총 약 2.7시간

| 항목 | 비용 | 근거 |
|---|---|---|
| **`org_name` 완성** | **약 2분** (380회) | `getMeta&type=ORG`로 누락 org_id 전부 조회 가능 (§6.6). 현재 40.6% 결손 → 사실상 0% |
| **`SEND_DE`** (+`REC_TBL_SE`) | 트리 재크롤 **2.5h** | 신선도 신호. 다른 대체 수단 없음 (§6.4) |
| **통계설명 서술형 4필드** | **7분** (1,256회) | `mainTermExpl`·`josaItm`·`dataUserNote`·`examinObjrange`만. `statsNm`은 제외 (§6.2) |

### ❌ 불필요 판정 — 실측 근거 있음

| 항목 | 판정 근거 |
|---|---|
| 통합검색 `CONTENTS` | **`doc_item_index`의 축약본** (271자 vs 최대 222,034자) — 중복 (§6.1) |
| 통합검색 전체 | **`searchNm` 필수라 전수 수집 불가** (§6.1) |
| CMMT (주석) | 24.5시간인데 **672행 중 664행이 행정구역 연혁 메모** (§6.3) |
| `STAT_NM` | **경로에 99.8% 포함** (§6.2) |
| 작성중지 뷰 | **2025년 주장 17,686건 중 언급 0건** (§6.5) |
| 광복이전·통계연감·영문 뷰 | 뉴스 검증과 무관 |

### ⚠️ 복구 불가

`units` 29.1% 결손 — KOSIS 원본에 데이터가 없다(`err:30`). **"단위 필터" 설계 시 29%가 빠진다는 걸 전제해야 한다.**

---

## 6. 실측 검증 기록

### 6.1 통합검색은 전수 수집이 불가능하다

```
searchNm 생략/빈값 → {"err":"20","errMsg":"필수요청변수값이 누락되었습니다."}
```

또한 `CONTENTS` 실제 값을 보면 우리 `doc_item_index`와 같은 성격(차원값·항목값 나열)이며 271자로 잘려 있다.

```
CONTENTS: "시도별 품목별 소비자물가지수 광주광역시 대구광역시 … 현미 보리쌀 배추 …"
```

**통합검색에서 유일하게 새로운 필드는 `ITEM03`(표 주석)과 `ORG_NM`(표 단위 작성기관)뿐이다.**

### 6.2 조사(stat_id)는 경로의 2번째 노드다

통합검색 1,097건(국내 주제별)으로 `STAT_NM` vs `MT_ATITLE` 대조:

```
조사명이 경로에 포함: 1,095 / 1,097 (99.8%)
위치: 2번째 노드가 압도적 (2/3단계 553건, 2/4단계 264건, 2/5단계 124건)
```

KOSIS 주제별 트리 구조 = `대분류(30) > 조사명 > 세부주제 > … > 통계표`

따라서 **`STAT_NM`은 이미 `category_paths`를 통해 `doc_meta_text`에 들어가 있다.** 예외 0.2%는 진열 위치와 출처가 다른 경우(예: 「국내인구이동통계」가 「주민등록인구현황」 아래 진열).

**통계설명(조사 단위)의 규모**: 조사 1,256개 / 표 265,094개 → 조사 1개당 평균 211표(중위 94, 최대 9,767). 응답 2,769자, 342ms → **1,256회 ≈ 7분**.

⚠️ 단 같은 조사의 표는 통계설명이 **완전히 동일**하므로 dense 벡터에 그대로 붙이면 최대 9,767개 표가 동일 텍스트를 공유해 구별 불가능해진다. **sparse/부스팅 용도로 한정해야 한다.**

### 6.3 CMMT는 대부분 행정구역 연혁 메모다

| 표 | 행 | 고유 | 유형 | 내용 |
|---|---|---|---|---|
| 주민등록인구 | 672 | 304 | 자료코드 664 / 통계표 8 | `'25.10.20 아라동 폐지` |
| 주민등록세대수 | 91 | 76 | 자료코드 88 / 통계표 3 | `'95행정구역개편으로 광양시와 통합` |
| 다빈도 수술 | 3 | 2 | 통계표 3 | `수진기준(입원기준)`, `순위는 수술건수 기준` |

검색에 유용한 건 `CMMT_NM='통계표'` 유형(표당 3~8행)뿐이며, 이는 통합검색 `ITEM03`과 같은 내용이다. **24.5시간(265,094회)을 쓸 근거가 부족하다.**

### 6.4 `SEND_DE`는 유용하나 `REC_TBL_SE`는 아니다

표본 1,667건(P2/P1/I1/J1):

```
SEND_DE  범위 2007 ~ 2026-07-31 | 2024년 이후 갱신 948건(57%), 2016년 이전 230건(14%)
REC_TBL_SE  Y 10건 (0.6%) / N 1,657건
```

`REC_TBL_SE`의 문제:
- Y 비율 0.6%로 극히 희소
- **`소비자물가지수(2020=100)` = N**
- Y 10건 중 **8건이 2024년 이전 갱신** (구 기준년 표: `농가판매가격지수(2010=100)`)
- 가이드에 "추천 통계표 여부" 한 줄뿐, 의미 불명

→ **재크롤 정당화는 `SEND_DE` 하나로 충분**하며, `REC_TBL_SE`는 같은 응답에 오므로 함께 저장(비용 0)하되 기대는 낮게.

### 6.5 작성중지 통계는 크롤링할 필요가 없다

```
claims_v1.jsonl 17,686건 — 전부 2025년 기사
작성중지 조사명 175개 vs 주장 17,686건
  → 정확 매칭 0개 / 부분 매칭 0개
```

목록도 지자체 단위 조사(안양시여성통계, 강남구사회조사)와 오래 전 중단 조사(가계자산조사(2006년))가 대부분이라 전국 단위 뉴스와 무관하다. 지표가 이어지는 경우 후속 조사가 주제별 뷰에 존재한다(가구소비실태조사 → 가계동향조사).

### 6.6 `org_name`은 표에서 뽑은 게 아니라 사전 조인이다

통계목록 응답에는 `ORG_ID`만 오고 `ORG_NM`이 없다. `kosis_org_names.json`(181개)은 `MT_OTITLE` 루트에서 만든 **별도 사전**이다.

```
트리 유니크 org_id 380개 / 사전 181개
이름 붙는 표 157,595 (59.4%) / 이름 없는 표 107,499 (40.6%)
누락 앞자리: 6xx 46,268 · 7xx 43,366 · 5xx 9,380 · 8xx 4,487 (전부 기초자치단체)
```

**해결책 실증**: `getMeta&type=ORG`가 누락 org_id를 정상 반환한다.

```
611 → 경기도 수원시      711 → 전북특별자치도 전주시
205/215 → 전남광주통합특별시   146 → 해양수산부   432 → 대한법률구조공단
```

**380회 ≈ 2분이면 40.6% 결손이 사실상 0이 된다.** 사전 갱신 중 사라졌던 `146`·`432` 회귀도 함께 복구된다.

**단 한계**: `org_id`는 호스팅 기관, `ORG_NM`은 작성 기관이라 1:1이 아니다(`org_id=101`에 `ORG_NM` 17종). 표 단위 정확한 기관명은 통합검색에만 있으나 전수 수집 불가.

### 6.7 차원 코드로 실제 수치 조회에 성공했다

v4 메타의 코드만으로 `get_data()`를 조립해 호출한 결과:

```
요청: objL1=00, objL2=0, objL3=000, itmId=T10, prdSe=Y, 2023
응답: DT=51145884.5  (2023년 전국 주민등록연앙인구)
      C1=00/행정구역(시군구)별  C2=0/성별  C3=000/연령별
```

**`dimensions` 배열 순서가 `objL1→L2→L3`에 그대로 대응한다** (문서에 없던 사실, 실측 확인).

| 단계 | 가능 여부 |
|---|---|
| 표 특정 | ✅ 벡터DB |
| 항목 `itmId` | ✅ v4 |
| 차원값 `objL1~L8` | ✅ v4에 코드 있음 / ❌ **카탈로그·벡터DB엔 없음** |
| 시점 `prdSe` | ⚠️ 범위는 있으나 **한글 주기명→API 코드 변환표 없음** |
| 어떤 차원값을 고를지 | ❌ **미구현 — 별도 매칭 로직 필요** |

---

## 7. 개선 권고

### 즉시 (크롤링 없이, 오프라인)

| # | 조치 | 근거 |
|---|---|---|
| 1 | **`doc_item_index` sparse 색인 복원** — 카탈로그 62%가 사문 | §4 |
| 2 | **payload 확장** — `units`, `periods`, `latest_period`, `dimensions`, `stat_id`, `category_paths` | §4 |
| 3 | **차원값 코드 복원** — 검증 단계 연결 | §6.7 |
| 4 | **`latest_period` 재계산** — 주기별 정규화 파싱 | §2.2 |

### 저비용 크롤링 (총 약 2.7시간)

| # | 조치 | 비용 |
|---|---|---|
| 5 | `getMeta&type=ORG` 380회 → `org_name` 완성 | 2분 |
| 6 | 통계설명 1,256회 → 서술형 4필드 | 7분 |
| 7 | 트리 재크롤(`SEND_DE` 포함) — **`SLEEP_SEC` 0.15→0.34 선행 필수** (현재 분당 400회로 한도 2배 초과) | 2.5시간 |

### 하지 말 것

CMMT 전량 크롤링(24.5h), 통합검색 대량 수집(불가), 작성중지·광복이전·통계연감·영문 뷰 크롤링

---

## 부록: 근거 위치

- 통계목록 p.19 / getMeta p.138~146 / 통계설명 p.111~116 / 통합검색 p.148
- 코드: `src/kosis_client.py`, `kosis_tree_crawler.py`, `kosis_meta_enricher.py`, `kosis_catalog_builder.py`, `kosis_v2_indexer.py`
- 설계 변경 이력: `origin/nayeon:archive/260722/docs/HYBRID_V2_*.md`
- 데이터: `kosis_table_tree.json`(265,094표), `kosis_table_meta_v4.jsonl`(ok 265,082), `kosis_catalog_v4.jsonl`, `data/hybrid_top20_300.jsonl`(300건 검색 결과)
