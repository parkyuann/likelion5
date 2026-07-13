"""
KOSIS 대분류(MT_ZTITLE) 30개 전체를 재귀적으로 탐색해 리프 통계표(TBL_ID) 전체 목록을 만드는 도구.

지금까지 kosis_table_summary.md는 뉴스에 자주 나오는 주제(물가/고용/부동산 등)를
골라서 부분적으로만 트리를 내려갔다. 이 스크립트는 대신 30개 대분류 전부를
BFS로 끝까지(리프 TBL_ID가 나올 때까지) 훑어서, 카테고리별 리프 표 개수와
전체 목록을 data/kosis_table_tree.json에 저장한다 — "몇 개 주제를 골라서 본 지도"가
아니라 "전체 지도"를 만드는 것이 목적.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/kosis_tree_crawler.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.kosis_client import list_tables  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "kosis_table_tree.json"
SLEEP_SEC = 0.15  # API 부하를 줄이기 위한 호출 간격

TOP_CATEGORIES = [
    ("A", "인구"), ("B", "사회일반"), ("C", "범죄ㆍ안전"), ("D", "노동"),
    ("E", "소득ㆍ소비ㆍ자산"), ("F", "보건"), ("G", "복지"), ("H1", "교육ㆍ훈련"),
    ("H2", "문화ㆍ여가"), ("I1", "주거"), ("I2", "국토이용"), ("J1", "경제일반ㆍ경기"),
    ("J2", "기업경영"), ("K1", "농림"), ("K2", "수산"), ("L", "광업ㆍ제조업"),
    ("M1", "건설"), ("M2", "교통ㆍ물류"), ("N1", "정보통신"), ("N2", "과학ㆍ기술"),
    ("O", "도소매ㆍ서비스"), ("P1", "임금"), ("P2", "물가"), ("Q", "국민계정"),
    ("R", "정부ㆍ재정"), ("S1", "금융"), ("S2", "무역ㆍ국제수지"), ("T", "환경"),
    ("U", "에너지"), ("V", "지역통계"),
]


def crawl_category(top_id: str, top_nm: str) -> dict:
    """BFS로 한 대분류 아래를 끝까지 훑어 리프 표 목록을 모은다."""
    leaves = []
    queue = [top_id]
    calls = 0
    while queue:
        parent = queue.pop(0)
        try:
            items = list_tables(parent_id=parent)
        except Exception as e:  # noqa: BLE001 - 크롤링 중 개별 노드 실패는 건너뛰고 계속
            print(f"    [WARN] {parent} 조회 실패: {e}")
            continue
        calls += 1
        time.sleep(SLEEP_SEC)
        if not isinstance(items, list):
            print(f"    [WARN] {parent} 응답이 리스트가 아님, 건너뜀: {items!r}")
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("TBL_ID"):
                leaves.append({
                    "org_id": it.get("ORG_ID"),
                    "tbl_id": it.get("TBL_ID"),
                    "tbl_nm": it.get("TBL_NM"),
                    "stat_id": it.get("STAT_ID"),
                })
            elif it.get("LIST_ID"):
                queue.append(it["LIST_ID"])
    return {"top_id": top_id, "top_nm": top_nm, "leaf_count": len(leaves), "calls": calls, "leaves": leaves}


def main():
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    result = {}
    if OUTPUT_PATH.exists():
        result = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))

    for top_id, top_nm in TOP_CATEGORIES:
        if top_id in result:
            print(f"[SKIP] {top_id} {top_nm} — 이미 완료됨 ({result[top_id]['leaf_count']}개)")
            continue
        t0 = time.time()
        print(f"[START] {top_id} {top_nm}")
        cat_result = crawl_category(top_id, top_nm)
        result[top_id] = cat_result
        OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[DONE] {top_id} {top_nm} — 리프 표 {cat_result['leaf_count']}개, "
              f"API 호출 {cat_result['calls']}회, {time.time()-t0:.1f}초")

    total = sum(v["leaf_count"] for v in result.values())
    print(f"\n=== 전체 완료: {len(result)}개 대분류, 리프 표 총 {total}개 ===")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
