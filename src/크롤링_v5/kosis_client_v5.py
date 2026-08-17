"""KOSIS OpenAPI 클라이언트 (v5).

`src/kosis_client.py`를 복사해 온 것이다. 원본은 손대지 않는다 — 이미 그 파일에
의존하는 코드(`kosis_tree_crawler.py`, `kosis_meta_enricher.py`, `kosis_api_probe.py`)의
동작을 바꾸지 않기 위해서다. v5 파이프라인은 이 파일만 쓴다.

원본과 달라진 곳은 `list_tables()` 하나뿐이며, 무엇이 왜 바뀌었는지는 그 함수의
docstring에 적었다. `search_tables` / `get_data` / `get_meta` 와 응답 파서(`_loads_lenient`)는
원본 그대로다.

> 유지보수 주의: `_loads_lenient`의 백슬래시 교정 로직이 원본과 두 벌로 존재한다.
> KOSIS 응답 파싱 버그를 또 만나면 **두 파일 다 고쳐야 한다.**

경로 규약 (reports/260810_KOSIS_전체표_크롤링_계획서.md §7):
    스크립트 → src/크롤링_v5/
    산출물   → data/크롤링_v5/
"""
import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ["KOSIS_API_KEY"]

# KOSIS 응답에 섞이는 두 종류의 깨진 백슬래시를 교정하는 패턴.
# 둘 다 영문명 필드(ITM_NM_ENG/OBJ_NM_ENG)에서 발생하며, KOSIS가 JSON 직렬화 때
# 백슬래시를 규격대로(\\) 이스케이프하지 않아 생긴 서버측 버그다.
#
# ① 무효 이스케이프: 백슬래시 뒤에 규격 외 글자(\o 등). 예: "c\ondiments".
#    유효한 이스케이프(\" \\ \/ \b \f \n \r \t \uXXXX)는 건드리지 않는다.
_INVALID_ESCAPE = re.compile(r'\\(?![\\"/bfnrtu])')
# ② 값 끝의 홑 백슬래시: 문자열이 백슬래시로 끝나(\") 닫는 따옴표가 이스케이프돼 버린 것.
#    예: "...Trees\","ITM_ID"... → \" 를 종료자가 아니라 따옴표 글자로 읽어 'Expecting ,'.
#    \" 뒤에 곧바로 구조 토큰(, } ])이 오면 종료 자리의 군더더기 백슬래시로 보고 \\" 로 고친다.
_DANGLING_BACKSLASH = re.compile(r'\\"(?=\s*[,}\]])')


def _loads_lenient(text: str):
    """KOSIS 응답 JSON을 파싱한다. 표준 파싱이 실패하면 백슬래시를 교정해 재시도한다.

    일부 표(주로 영문명 ITM_NM_ENG/OBJ_NM_ENG가 붙은 표)는 응답 안에 KOSIS가 규격대로
    이스케이프하지 않은 백슬래시가 섞여 표준 json.loads가 실패한다(위 ①·②). 데이터
    자체는 정상이므로, 깨진 백슬래시를 리터럴 백슬래시(\\\\)로 교정해 무손실로 파싱을
    살린다(값은 그대로 보존, 우리는 영문명을 어차피 버린다). 이 fallback은 표준 파싱이
    실패한 응답에서만 실행되므로 정상 응답의 동작은 바뀌지 않는다.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        patched = _INVALID_ESCAPE.sub(r'\\\\', text)
        patched = _DANGLING_BACKSLASH.sub(r'\\\\"', patched)
        return json.loads(patched)


LIST_URL = "https://kosis.kr/openapi/statisticsList.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
META_URL = "https://kosis.kr/openapi/statisticsData.do"
SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"

# KOSIS가 "데이터가 존재하지 않습니다"에 쓰는 코드. 트리 순회에서는 오류가 아니라
# '자식이 없는 정상 노드'를 뜻하므로 list_tables()에서 빈 리스트로 흡수한다.
ERR_NO_DATA = "30"
# 분당 호출 한도(200) 초과. 호출자가 쿨다운 후 재시도해야 하는 '일시적' 실패다.
ERR_RATE_LIMIT = "40"


def search_tables(keyword: str, result_count: int = 10) -> list[dict]:
    """키워드로 통계표를 직접 검색한다 (statisticsSearch.do).

    list_tables()/트리 크롤러는 대분류부터 트리를 내려가며 표를 찾는 방식(BFS)이라
    "이 키워드를 다루는 표가 있는가"를 확인하려면 트리 전체를 돌아야 했다. 이
    엔드포인트는 관련도(RANK)순으로 바로 검색해준다.

    통계목록(list_tables)보다 필드가 훨씬 많다 — ORG_NM(기관명), STAT_NM(조사명),
    MT_ATITLE(한글 경로), FULL_PATH_ID(ID 경로), CONTENTS(주요내용),
    STRT_PRD_DE/END_PRD_DE(수록기간), TBL_VIEW_URL/LINK_URL 등 18개.
    다만 searchNm이 필수라 **전수 열거에는 쓸 수 없다** — 보강 채널로만 쓴다.
    (계획서 부록 D)
    """
    params = {
        "method": "getList", "apiKey": API_KEY,
        "searchNm": keyword, "sort": "RANK",
        "startCount": 1, "resultCount": result_count,
        "format": "json", "jsonVD": "Y",
    }
    res = requests.get(SEARCH_URL, params=params, timeout=10)
    res.raise_for_status()
    data = _loads_lenient(res.text)
    if isinstance(data, dict) and "err" in data:
        raise RuntimeError(f"KOSIS API 오류: {data}")
    return data


def list_tables(vw_cd: str, parent_id: str, *, timeout: int = 10) -> list[dict]:
    """서비스뷰(vw_cd)의 분류 트리를 한 단계 내려가 자식 노드·통계표를 반환한다.

    v5에서 원본과 달라진 점 두 가지.

    ① **err 응답을 검사한다.**
       원본은 `err` 필드를 보지 않고 응답을 그대로 반환했다 — search_tables/get_data/
       get_meta 셋과 동작이 달랐다. 그래서 API가 {"err":"40","errMsg":"호출가능건수를
       초과..."}를 돌려주면 크롤러(kosis_tree_crawler.py:109-121)가 그것을 "표 없음"으로
       읽고 [WARN] 한 줄만 남긴 채 **그 노드 아래 서브트리를 통째로 버렸다.**
       실증(2026-08-09): E 소득ㆍ소비ㆍ자산이 재시도 없이 81건, 재시도를 넣으면 865건.

       이제 err 30(데이터 없음 = 자식이 없는 정상 노드)만 빈 리스트로 흡수하고 나머지는
       예외로 올린다 — 호출자가 재시도할지 실패 노드로 기록할지 판단할 수 있다.

    ② **vw_cd·parent_id에 기본값이 없다.**
       원본은 vw_cd="MT_ZTITLE", parent_id="A"가 기본값이라 뷰를 안 넘긴 호출이 조용히
       국내통계 주제별로 갔다. 실제로 kosis_tree_crawler.py:93이 vw_cd를 넘기지 않아
       서비스뷰 하나만 수집된 원인이 됐다. parent_id="A"(인구 대분류)도 근거 없는
       기본값이었다. 이제 둘 다 필수 인자라 빠뜨리면 즉시 TypeError가 난다.

    재시도·rate limit은 여기 넣지 않는다. 호출자마다 정책이 달라야 하기 때문이다
    (kosis_meta_enricher.py의 RateLimiter·call_with_retry 패턴을 v5 크롤러도 따른다).

    Args:
        vw_cd: 서비스뷰 코드. 예: "MT_ZTITLE"(국내통계 주제별), "MT_OTITLE"(기관별),
            "MT_RTITLE"(국제통계). 웹 메뉴 노출 13개가 v5 수집 범위다(계획서 §2).
        parent_id: 시작 목록 ID. 빈 문자열이면 그 뷰의 최상위 노드들이 열린다.
        timeout: 요청 타임아웃(초).

    Returns:
        응답 행 리스트. 행은 두 종류가 섞여 온다 —
          · 통계표 행: ORG_ID / TBL_ID / TBL_NM / STAT_ID / SEND_DE / REC_TBL_SE / VW_CD / VW_NM
          · 중간 목록 행: LIST_ID / LIST_NM (+ VW_CD / VW_NM)
        자식이 없는 노드는 빈 리스트.

    Raises:
        RuntimeError: err 30 이외의 KOSIS API 오류. 특히 err 40은 분당 한도(200) 초과라
            호출자가 쿨다운 후 재시도해야 한다.
    """
    params = {
        "method": "getList",
        "apiKey": API_KEY,
        "vwCd": vw_cd,
        "parentListId": parent_id,  # 문서상 이름은 parentId지만 실제로 동작하는 건 parentListId (실측 확인)
        "format": "json",
        "jsonVD": "Y",  # 이게 없으면 키에 큰따옴표가 빠진 비표준 JSON이 돌아옴
    }
    res = requests.get(LIST_URL, params=params, timeout=timeout)
    res.raise_for_status()
    data = _loads_lenient(res.text)
    if isinstance(data, dict) and "err" in data:
        if data["err"] == ERR_NO_DATA:
            return []
        raise RuntimeError(f"KOSIS API 오류: {data} (vwCd={vw_cd}, parentListId={parent_id!r})")
    return data


def get_data(org_id, tbl_id, obj_l1, itm_id, prd_se="Y",
             start_prd_de=None, end_prd_de=None, **extra_obj_levels):
    """실제 통계 수치를 조회한다 (통계표선택 방식)."""
    params = {
        "method": "getList", "apiKey": API_KEY,
        "orgId": org_id, "tblId": tbl_id,
        "objL1": obj_l1, "itmId": itm_id, "prdSe": prd_se,
        "format": "json", "jsonVD": "Y", **extra_obj_levels,
    }
    if start_prd_de:
        params["startPrdDe"] = start_prd_de
    if end_prd_de:
        params["endPrdDe"] = end_prd_de

    res = requests.get(DATA_URL, params=params, timeout=10)
    res.raise_for_status()
    data = _loads_lenient(res.text)
    if isinstance(data, dict) and "err" in data:
        raise RuntimeError(f"KOSIS API 오류: {data}")
    return data


def get_meta(org_id: str, tbl_id: str, meta_type: str = "ITM", **extra_params) -> list[dict]:
    """표의 메타정보(분류/항목 코드, 단위, 출처 등)를 공식 API로 조회한다.

    meta_type: TBL(통계표명) / ORG(기관명) / ITM(분류·항목 코드, 기본값) / CMMT(주석)
               / UNIT(단위) / SOURCE(출처) / PRD(수록정보) / WGT(가중치) / NCD(자료갱신일)
    extra_params: type별 선택 파라미터. ITM은 objId/itmId, PRD는 detail,
                  NCD는 prdSe, WGT는 분류코드1~8/ITEM 등.

    v5 실측 메모 (2026-08-10):
      · type="ORG" 는 tbl_id 가 필요 없다. 빈 문자열을 넘기면 동작한다 —
        get_meta("301", "", "ORG") → [{"ORG_NM":"한국은행","ORG_NM_ENG":"Bank of Korea"}].
        기관당 1콜이라 등장 기관 380개를 ~2분에 채울 수 있다(계획서 §3.4).
      · type="NCD" 는 개발가이드 요청변수 표에 tblId 가 빠져 있으나 **실제로는 필수**다
        (없으면 err 20). 그리고 표 1건에 (수록주기 × 수록시점)만큼 행이 온다 —
        DT_103Y002 실측 967행. 표 단위 갱신일이 필요하면 NCD 가 아니라
        list_tables() 의 SEND_DE 를 쓸 것(호출 비용 10배 차이).
    """
    params = {
        "method": "getMeta", "apiKey": API_KEY,
        "orgId": org_id, "tblId": tbl_id, "type": meta_type,
        "format": "json", "jsonVD": "Y", **extra_params,
    }
    res = requests.get(META_URL, params=params, timeout=10)
    res.raise_for_status()
    data = _loads_lenient(res.text)
    if isinstance(data, dict) and "err" in data:
        raise RuntimeError(f"KOSIS API 오류: {data}")
    return data
