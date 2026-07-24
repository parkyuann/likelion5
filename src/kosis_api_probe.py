# -*- coding: utf-8 -*-
"""KOSIS API 라이브 탐색 스크립트 (크롤링 전략 수립용).

각 엔드포인트가 실제로 어떤 필드를 주는지 소량 호출로 확인한다.
결과 근거 문서: reports/KOSIS_API_크롤링전략.md

실행: venv\\Scripts\\python.exe src\\kosis_api_probe.py
호출량: 약 15콜 (분당 200콜 제한에 안전)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kosis_client as kc

SAMPLE_ORG, SAMPLE_TBL = "101", "DT_1DA7104S"  # 행정구역(시도)/성별 실업률


def brief(obj, n=400):
    return json.dumps(obj, ensure_ascii=False)[:n]


def main():
    print("### 1. statisticsSearch.do — 키워드 검색")
    for kw in ["실업률", "소비자물가지수", "출생아 수"]:
        rows = kc.search_tables(kw, result_count=3)
        print(f"  '{kw}' -> {len(rows)}건, 첫 건 필드: {sorted(rows[0].keys())}")

    print("\n### 2. statisticsList.do — 서비스뷰별 최상위 (빈 parentListId)")
    for vw in ["MT_RTITLE", "MT_BUKHAN", "MT_OTITLE", "MT_GTITLE01"]:
        rows = kc.list_tables(vw_cd=vw, parent_id="")
        names = [r.get("LIST_NM") for r in rows[:4]] if isinstance(rows, list) else rows
        print(f"  {vw}: {len(rows)}개 최상위 — {names}")

    print("\n### 3. getMeta — type별 응답 크기")
    for t in ["TBL", "ITM", "PRD", "UNIT", "SOURCE", "NCD", "CMMT"]:
        try:
            d = kc.get_meta(SAMPLE_ORG, SAMPLE_TBL, t)
            print(f"  {t}: {len(d)} rows, {len(json.dumps(d, ensure_ascii=False))} chars")
        except Exception as e:
            print(f"  {t}: ERR {str(e)[:80]}")

    print("\n### 4. getData — 소량 on-demand 조회 비용")
    t0 = time.time()
    d = kc.get_data(SAMPLE_ORG, SAMPLE_TBL, obj_l1="00", itm_id="T80",
                    prd_se="Y", start_prd_de="2023", end_prd_de="2025", objL2="0")
    print(f"  전국x계 3개년: {len(d)} rows, {time.time()-t0:.2f}s")
    print("  응답 필드:", sorted(d[0].keys()))


if __name__ == "__main__":
    main()
