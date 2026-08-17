"""지표 경로로 회수한 '뷰 밖 표' 2건을 마스터에 추가한다.

배경
----
11개 뷰 전수 크롤 후에도 KOSIS 공식 총량과 약 2,235건 차이가 남는다. 이들은 어떤
서비스뷰에도 분류 노드가 연결되지 않은 표라 statisticsList.do 로 열거되지 않는다.
KOSIS 공유서비스 관리자 공식 답변(2026-08-11)으로 확인된 사실:
  - "통계목록에 지정되지 않은 주요 지표" 표가 존재한다 (DT_731Y001 등)
  - "목록에 달려있지 않은 통계표들이 더 존재할 수 있으나 KOSIS 서비스 기능으로는
     확인이 어렵다" — 즉 KOSIS 스스로도 열거 불가

회수 경로: 통계주요지표 1,473개 이름을 검색 티커에 넣어 generator_htmlLink 를 파싱
(probe_indicator_tables_v5.py). 트리 밖 표는 딱 2건이었다:
  301:DT_731Y001  주요국 통화의 대원화환율   (일별, 1964~현재, 15개+ 통화)
  301:DT_038Y001  외환보유액

이 표들은 원/엔·원/위안 등 다통화 최신 환율의 유일한 출처다 — 대체 표(DT_036Y 계열)는
2014년 작성중지됐다.

마스터 표기: view_codes=["INDICATOR"](가상 뷰), source_note 로 회수 경위를 남긴다.
재실행하면 이미 있는 표는 건너뛴다(멱등).
"""
import io
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kosis_client_v5 import get_meta  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
MASTER = ROOT / "data" / "크롤링_v5" / "kosis_table_tree_master_v5.jsonl"
ORG_NAMES = ROOT / "data" / "크롤링_v5" / "kosis_org_names_v5.json"

# 지표 티커 전수 조사(probe_indicator_tables_v5.py)에서 확정된 뷰 밖 표
TARGETS = [
    ("301", "DT_731Y001", "환율"),
    ("301", "DT_038Y001", "외환보유액"),
]

NOTE = ("KOSIS 분류 트리 미등록 표(공식 확인: 2026-08-11 공유서비스 관리자 답변). "
        "통계주요지표 검색 티커 경로로 회수")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    existing = set()
    with MASTER.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                existing.add((r["org_id"], r["tbl_id"]))
    org_names = json.loads(ORG_NAMES.read_text(encoding="utf-8"))

    added = 0
    with MASTER.open("a", encoding="utf-8") as out:
        for org, tbl, indicator in TARGETS:
            if (org, tbl) in existing:
                print(f"  [SKIP] {org}:{tbl} — 이미 마스터에 있음")
                continue
            tbl_nm = get_meta(org, tbl, "TBL")[0].get("TBL_NM")
            prd = get_meta(org, tbl, "PRD")
            latest = max((str(p.get("END_PRD_DE") or "") for p in prd), default=None)
            rec = {
                "table_key": f"{org}:{tbl}",
                "org_id": org,
                "org_name": (org_names.get(org) or {}).get("ko"),
                "tbl_id": tbl,
                "tbl_nm": tbl_nm,
                "stat_id": None,             # 분류 미등록 표 — getMeta STAT 도 err 30
                "send_de": str(date.today()),  # 수집일로 대체 (목록 API 밖이라 SEND_DE 없음)
                "rec_tbl_se": None,
                "view_codes": ["INDICATOR"],
                "category_paths": {"INDICATOR": [{"names": ["통계주요지표", indicator], "ids": []}]},
                "source_note": NOTE,
                "latest_period": latest,
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            added += 1
            print(f"  [ADD] {org}:{tbl} {tbl_nm} (수록 ~{latest})")

    total = len(existing) + added
    print(f"\n=== 완료: {added}건 추가 → 마스터 {total:,}건 ===")


if __name__ == "__main__":
    main()
