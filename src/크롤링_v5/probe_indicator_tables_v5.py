"""통계주요지표 1,473개를 검색 티커로 훑어 '트리 밖 표'를 회수한다 (API 키 불필요).

배경
----
뷰 전수 크롤(11개 뷰, 287,671건) 후에도 KOSIS 공식 총량 289,906과 약 2,235건 차이가
남는다. 이 표들은 어떤 서비스뷰에도 없어 statisticsList.do 로 열거되지 않는다.
대표 사례가 301:DT_731Y001(주요국 통화의 대원화환율)이다.

발견한 경로
-----------
KOSIS 검색 결과 상단의 '통계지표 티커'는 검색어를 지표 DB와 매칭해 대표 표 하나를
generator_htmlLink('orgId','tblId',...) 로 렌더한다(서버 사이드 HTML). 지표명으로
검색하면 그 지표가 가리키는 표의 orgId/tblId 를 얻는다.

우리는 통계주요지표 코드표(openApiCodeList.do XLS)에서 지표명 1,473개를 이미 갖고 있다.
이 이름들로 검색 → 티커의 표 → 마스터에 없는 것만 골라내면 트리 밖 표를 회수한다.

한계 (반드시 인지)
------------------
- 지표(1,473) ≠ 트리 밖 표(2,235). 이 방법은 '지표로 노출되는' 트리 밖 표만 건진다.
- 표본 30개 실측: 22개 표 추출, 그중 트리 밖 0개 — 대부분 지표는 트리 안 표를 가리킨다.
  즉 회수량은 수십~수백 규모로 예상되며, 2,235 전부를 커버하지 못한다.
- 검색 티커는 KOSIS 비공식 렌더링이라 구조가 바뀌면 깨진다.

사용:
    venv/Scripts/python.exe src/크롤링_v5/probe_indicator_tables_v5.py
출력:
    data/크롤링_v5/indicator_outside_tables_v5.jsonl   트리 밖으로 판정된 표
"""
import io
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests
import xlrd

ROOT = Path(__file__).resolve().parent.parent.parent
MASTER = ROOT / "data" / "크롤링_v5" / "kosis_table_tree_master_v5.jsonl"
XLS = ROOT / "data" / "크롤링_v5" / "kosis_indicator_codes_v5.xls"
OUT = ROOT / "data" / "크롤링_v5" / "indicator_outside_tables_v5.jsonl"

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
LINK = re.compile(r"generator_htmlLink\('([^']*)','([^']*)'")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    S = requests.Session()
    S.headers.update(H)

    # 지표 코드표 확보 (없으면 내려받음)
    if not XLS.exists():
        XLS.parent.mkdir(parents=True, exist_ok=True)
        XLS.write_bytes(S.get("https://kosis.kr/openapi/openApiCodeList.do", timeout=30).content)
    sheet = xlrd.open_workbook(str(XLS)).sheet_by_index(0)
    indicators = []
    for i in range(4, sheet.nrows):
        row = sheet.row_values(i)
        nm = str(row[4]).strip()
        jid = str(row[5]).strip().split(".")[0]
        sector = str(row[2]).strip()
        if nm:
            indicators.append((jid, sector, nm))
    print(f"[STATUS] 지표 {len(indicators)}개")

    master = set()
    with MASTER.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                master.add((r["org_id"], r["tbl_id"]))
    print(f"[STATUS] 마스터 표 {len(master):,}개\n")

    outside = {}          # (org,tbl) -> {지표명들}
    seen_tbl = set()
    t0 = time.time()
    for i, (jid, sector, nm) in enumerate(indicators, 1):
        u = "https://kosis.kr/search/search.do?query=" + urllib.parse.quote(nm)
        try:
            t = S.get(u, timeout=15).text
        except Exception:
            continue
        # 티커 최상단 표 1개
        m = LINK.search(t)
        if m:
            key = (m.group(1), m.group(2))
            seen_tbl.add(key)
            if key not in master:
                outside.setdefault(key, {"indicators": [], "jipyo_ids": []})
                outside[key]["indicators"].append(nm)
                outside[key]["jipyo_ids"].append(jid)
        if i % 100 == 0:
            print(f"  {i}/{len(indicators)} | 표 발견 {len(seen_tbl)} | 트리밖 {len(outside)} | {time.time()-t0:.0f}초", flush=True)
        time.sleep(0.25)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for (org, tbl), meta in outside.items():
            f.write(json.dumps({"org_id": org, "tbl_id": tbl, **meta}, ensure_ascii=False) + "\n")

    print(f"\n=== 완료 ({time.time()-t0:.0f}초) ===")
    print(f"  지표 티커에서 표 {len(seen_tbl)}개 발견")
    print(f"  그중 트리(마스터) 밖: {len(outside)}개  → {OUT}")
    for (org, tbl), meta in list(outside.items())[:20]:
        print(f"    {org}:{tbl}  ← 지표 '{meta['indicators'][0]}'")


if __name__ == "__main__":
    main()
