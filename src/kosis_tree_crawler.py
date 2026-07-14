"""
KOSIS 대분류(MT_ZTITLE) 30개 전체를 재귀적으로 탐색해 리프 통계표(TBL_ID) 전체 목록을 만드는 도구.

지금까지 kosis_table_summary.md는 뉴스에 자주 나오는 주제(물가/고용/부동산 등)를
골라서 부분적으로만 트리를 내려갔다. 이 스크립트는 대신 30개 대분류 전부를
BFS로 끝까지(리프 TBL_ID가 나올 때까지) 훑어서, 카테고리별 리프 표 개수와
전체 목록을 data/kosis_table_tree.json에 저장한다 — "몇 개 주제를 골라서 본 지도"가
아니라 "전체 지도"를 만드는 것이 목적.

각 리프 표에는 중간 카테고리 이름 경로(path, 예: ["노동", "경제활동인구조사", "실업률"])도
함께 저장한다 — 벡터DB 색인 그레뉴러리티 실험(vector_search_experiment.md)에서
"표 이름만으로는 색인 정확도에 한계가 있다"는 결과가 나와서, 리랭커 이전 단계에서
"표 이름 + 대분류 경로"까지 색인 문서에 넣어볼 수 있도록 미리 확보해두는 것.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/kosis_tree_crawler.py
"""
import json
import sys
import time
from collections import deque
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


def format_elapsed(seconds: float) -> str:
    """경과 시간을 HH:MM:SS 형식으로 반환한다."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def print_progress(
    *,
    completed_categories: int,
    total_categories: int,
    top_id: str,
    top_nm: str,
    processed_nodes: int,
    pending_nodes: int,
    leaf_count: int,
    started_at: float,
) -> None:
    """현재까지 발견된 노드를 기준으로 진행 상황을 즉시 출력한다."""
    known_nodes = processed_nodes + pending_nodes
    node_percent = (processed_nodes / known_nodes * 100) if known_nodes else 100.0
    category_percent = completed_categories / total_categories * 100
    remaining_categories = total_categories - completed_categories
    print(
        f"[PROGRESS] 전체 {completed_categories}/{total_categories} "
        f"({category_percent:5.1f}%) | 남은 대분류 {remaining_categories} | "
        f"현재 {top_id} {top_nm} | 발견 노드 {processed_nodes}/{known_nodes} "
        f"({node_percent:5.1f}%) | 대기 {pending_nodes} | "
        f"수집 표 {leaf_count:,} | 경과 {format_elapsed(time.time() - started_at)}",
        flush=True,
    )


def crawl_category(
    top_id: str,
    top_nm: str,
    *,
    completed_categories: int,
    total_categories: int,
) -> dict:
    """BFS로 한 대분류 아래를 끝까지 훑어 리프 표 목록(중간 경로 포함)을 모은다."""
    leaves = []
    queue = deque([(top_id, [top_nm])])  # (LIST_ID, 여기까지의 카테고리 이름 경로)
    calls = 0
    processed_nodes = 0
    started_at = time.time()
    while queue:
        parent, path = queue.popleft()
        processed_nodes += 1
        try:
            items = list_tables(parent_id=parent)
        except Exception as e:  # noqa: BLE001 - 크롤링 중 개별 노드 실패는 건너뛰고 계속
            print(f"    [WARN] {parent} 조회 실패: {e}", flush=True)
            print_progress(
                completed_categories=completed_categories,
                total_categories=total_categories,
                top_id=top_id,
                top_nm=top_nm,
                processed_nodes=processed_nodes,
                pending_nodes=len(queue),
                leaf_count=len(leaves),
                started_at=started_at,
            )
            continue
        calls += 1
        time.sleep(SLEEP_SEC)
        if not isinstance(items, list):
            print(f"    [WARN] {parent} 응답이 리스트가 아님, 건너뜀: {items!r}", flush=True)
            print_progress(
                completed_categories=completed_categories,
                total_categories=total_categories,
                top_id=top_id,
                top_nm=top_nm,
                processed_nodes=processed_nodes,
                pending_nodes=len(queue),
                leaf_count=len(leaves),
                started_at=started_at,
            )
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
                    "path": path,
                })
            elif it.get("LIST_ID"):
                queue.append((it["LIST_ID"], path + [it.get("LIST_NM", it["LIST_ID"])]))
        print_progress(
            completed_categories=completed_categories,
            total_categories=total_categories,
            top_id=top_id,
            top_nm=top_nm,
            processed_nodes=processed_nodes,
            pending_nodes=len(queue),
            leaf_count=len(leaves),
            started_at=started_at,
        )
    return {"top_id": top_id, "top_nm": top_nm, "leaf_count": len(leaves), "calls": calls, "leaves": leaves}


def main():
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    result = {}
    if OUTPUT_PATH.exists():
        result = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))

    total_categories = len(TOP_CATEGORIES)
    completed_categories = sum(top_id in result for top_id, _ in TOP_CATEGORIES)
    print(
        f"[STATUS] 시작 시점: 완료 {completed_categories}/{total_categories}, "
        f"남은 대분류 {total_categories - completed_categories}",
        flush=True,
    )

    for top_id, top_nm in TOP_CATEGORIES:
        if top_id in result:
            print(
                f"[SKIP] {top_id} {top_nm} — 이미 완료됨 "
                f"(표 {result[top_id]['leaf_count']:,}개) | "
                f"전체 {completed_categories}/{total_categories} "
                f"({completed_categories / total_categories * 100:.1f}%)",
                flush=True,
            )
            continue
        t0 = time.time()
        print(
            f"[START] {top_id} {top_nm} | 전체 완료 "
            f"{completed_categories}/{total_categories} | "
            f"남은 대분류 {total_categories - completed_categories}",
            flush=True,
        )
        cat_result = crawl_category(
            top_id,
            top_nm,
            completed_categories=completed_categories,
            total_categories=total_categories,
        )
        result[top_id] = cat_result
        OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        completed_categories += 1
        print(
            f"[DONE] {top_id} {top_nm} | 표 {cat_result['leaf_count']:,}개 | "
            f"API 호출 {cat_result['calls']:,}회 | "
            f"소요 {format_elapsed(time.time() - t0)} | "
            f"전체 {completed_categories}/{total_categories} "
            f"({completed_categories / total_categories * 100:.1f}%) | "
            f"남음 {total_categories - completed_categories}",
            flush=True,
        )

    total = sum(v["leaf_count"] for v in result.values())
    print(f"\n=== 전체 완료: {completed_categories}/{total_categories}개 대분류, 표 총 {total:,}개 ===")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()