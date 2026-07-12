import os

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ["KOSIS_API_KEY"]

LIST_URL = "https://kosis.kr/openapi/statisticsList.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"


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
