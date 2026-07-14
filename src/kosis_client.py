import os

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ["KOSIS_API_KEY"]

LIST_URL = "https://kosis.kr/openapi/statisticsList.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
META_URL = "https://kosis.kr/openapi/statisticsData.do"
SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"


def search_tables(keyword: str, result_count: int = 10) -> list[dict]:
    """키워드로 통계표를 직접 검색한다 (statisticsSearch.do).

    list_tables()/kosis_tree_crawler.py는 대분류부터 트리를 내려가며 표를 찾는
    방식(BFS)이라 "이 키워드를 다루는 표가 있는가"를 확인하려면 트리 전체를
    돌아야 했다. 이 엔드포인트는 관련도(RANK)순으로 바로 검색해준다 — 뉴스에서
    뽑은 지표명으로 후보 표를 즉시 좁힐 때(벡터DB 색인 전 1차 후보 확보, 또는
    벡터DB 없이도 쓸 수 있는 대안) 유용하다. 단, 트리 크롤링과 달리 상위
    카테고리 경로(path)는 응답에 없다.
    """
    params = {
        "method": "getList", "apiKey": API_KEY,
        "searchNm": keyword, "sort": "RANK",
        "startCount": 1, "resultCount": result_count,
        "format": "json", "jsonVD": "Y",
    }
    res = requests.get(SEARCH_URL, params=params, timeout=10)
    res.raise_for_status()
    data = res.json()
    if isinstance(data, dict) and "err" in data:
        raise RuntimeError(f"KOSIS API 오류: {data}")
    return data


def list_tables(vw_cd: str = "MT_ZTITLE", parent_id: str = "A") -> list[dict]:
    """분류 트리를 한 단계씩 내려가며 통계표(TBL_ID) 후보를 탐색한다."""
    params = {
        "method": "getList",
        "apiKey": API_KEY,
        "vwCd": vw_cd,
        "parentListId": parent_id,  # 문서상 이름은 parentId지만 실제로 동작하는 건 parentListId (실측 확인)
        "format": "json",
        "jsonVD": "Y",  # 이게 없으면 키에 큰따옴표가 빠진 비표준 JSON이 돌아옴
    }
    res = requests.get(LIST_URL, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


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
    data = res.json()
    if isinstance(data, dict) and "err" in data:
        raise RuntimeError(f"KOSIS API 오류: {data}")
    return data


def get_meta(org_id: str, tbl_id: str, meta_type: str = "ITM", **extra_params) -> list[dict]:
    """표의 메타정보(분류/항목 코드, 단위, 출처 등)를 공식 API로 조회한다.

    이전에는 objL1~8/itmId 코드값을 알아내려고 kosis_code_explorer.py(Selenium)로
    표 화면을 렌더링해 fancytree 노드를 읽었으나, 이 getMeta 엔드포인트가 그 코드값을
    (OBJ_ID, OBJ_NM, ITM_ID, ITM_NM, UNIT_NM 등) 그대로 반환한다는 걸
    openApi_manual_v1.0.pdf(2.5절)에서 뒤늦게 확인해 대체함.

    meta_type: TBL(통계표명) / ITM(분류·항목 코드, 기본값) / CMMT(주석) / UNIT(단위)
               / SOURCE(출처) / PRD(수록정보) / WGT(가중치) / NCD(자료갱신일)
    extra_params: type별 선택 파라미터. ITM은 objId/itmId, PRD는 detail,
                  NCD는 prdSe, WGT는 분류코드1~8/ITEM 등 (매뉴얼 2.5.2절 참고).
    """
    params = {
        "method": "getMeta", "apiKey": API_KEY,
        "orgId": org_id, "tblId": tbl_id, "type": meta_type,
        "format": "json", "jsonVD": "Y", **extra_params,
    }
    res = requests.get(META_URL, params=params, timeout=10)
    res.raise_for_status()
    data = res.json()
    if isinstance(data, dict) and "err" in data:
        raise RuntimeError(f"KOSIS API 오류: {data}")
    return data
