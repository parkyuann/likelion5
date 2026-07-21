"""
kosis_table_tree.json의 표 목록에 차원(분류)·항목명 메타데이터를 보강하는 도구.

kosis_tree_crawler.py는 statisticsList.do(목록 API)로 트리를 훑어 표 목록을 만든다.
그런데 이 엔드포인트는 표의 "차원(시도별/연령별 등)"과 "항목(가맹점수/매출액 등)"을
반환하지 않는다 — 그 정보는 statisticsData.do?method=getMeta&type=ITM에만 있다
(kosis_client.get_meta 참고, openApi_manual_v1.0.pdf 2.5절).

그 결과 kosis_catalog_v1의 document_text는 "표명 + 카테고리 경로"가 전부였고,
표명 중복률이 51.4%(유니크 128,832 / 전체 265,094)라 표명만으로는 후보를 좁힐 수
없었다. 이 스크립트는 표별로 getMeta를 호출해 차원명·차원값·항목명·단위를 모으고,
build_kosis_catalog.py(v2)가 doc_meta_text / doc_item_index를 나눠 만들 수 있게 한다.

트리는 다시 크롤링하지 않는다 — 표 목록은 이미 있으므로 getMeta만 덧붙인다.
출력은 트리를 덮어쓰지 않고 별도 JSONL에 append하며, 재실행하면 이미 받은 표는
건너뛴다(중단·재개 가능). 표 1개당 API 1회이므로 전량 보강은 265,094회다 —
--categories/--org-id/--limit으로 범위를 좁혀 나눠 돌리는 것을 전제로 한다.

사용 예 (레포 루트에서):
    # 0) 응답 형태부터 확인 (3건만, 원본 JSON 그대로 출력)
    venv/Scripts/python.exe src/kosis_tree_crawler_v2.py --limit 3 --dump-raw

    # 1) 대분류 하나만 보강
    venv/Scripts/python.exe src/kosis_tree_crawler_v2.py --categories P2

    # 2) 이어서 전체 (중단해도 재실행하면 이어짐)
    venv/Scripts/python.exe src/kosis_tree_crawler_v2.py
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.kosis_client import get_meta  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "kosis_table_tree.json"
DEFAULT_OUTPUT = ROOT / "data" / "kosis_table_meta.jsonl"

SLEEP_SEC = 0.15  # kosis_tree_crawler.py와 동일한 호출 간격
MAX_RETRY = 3
RETRY_BACKOFF_SEC = 2.0
ABORT_AFTER_CONSECUTIVE_ERRORS = 20  # 일일 호출 한도 소진 등으로 전량 실패할 때 조기 중단


def format_elapsed(seconds: float) -> str:
    """경과 시간을 HH:MM:SS 형식으로 반환한다."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def iter_tables(tree: dict, categories: set[str] | None) -> list[dict]:
    """트리에서 (org_id, tbl_id) 단위 유니크 표 목록을 뽑는다.

    같은 표가 여러 카테고리 경로에 중복 등장하므로 build_kosis_catalog.py와 같은
    기준(org_id:tbl_id)으로 중복을 제거한다 — getMeta를 두 번 부르지 않기 위함.
    """
    seen: dict[str, dict] = {}
    for top_id, category in tree.items():
        if categories and top_id not in categories:
            continue
        if not isinstance(category, dict):
            continue
        for leaf in category.get("leaves", []):
            if not isinstance(leaf, dict):
                continue
            org_id = str(leaf.get("org_id") or "").strip()
            tbl_id = str(leaf.get("tbl_id") or "").strip()
            if not org_id or not tbl_id:
                continue
            key = f"{org_id}:{tbl_id}"
            if key not in seen:
                seen[key] = {
                    "table_key": key,
                    "org_id": org_id,
                    "tbl_id": tbl_id,
                    "tbl_nm": leaf.get("tbl_nm"),
                    "top_id": top_id,
                }
    return list(seen.values())


def load_done_keys(output_path: Path) -> set[str]:
    """이미 수집한 table_key를 읽어 재실행 시 건너뛸 수 있게 한다.

    중단 시점에 half-written 라인이 남을 수 있으므로 깨진 줄은 조용히 무시한다.
    """
    if not output_path.exists():
        return set()
    done: set[str] = set()
    with output_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = record.get("table_key")
            if key:
                done.add(key)
    return done


ITEM_AXIS_ID = "ITEM"  # 항목(itmId) 축을 가리키는 OBJ_ID. 나머지 OBJ_ID는 분류(차원) 축이다.


def parse_meta_rows(rows: list) -> dict:
    """getMeta(type=ITM) 응답을 차원/항목/단위로 정리한다.

    응답의 모든 행은 (OBJ_ID = 어느 축인가) + (ITM_ID/ITM_NM = 그 축 안의 코드·이름)
    쌍이다. 축이 곧 구분자다 — OBJ_ID가 "ITEM"인 행은 진짜 항목(세대수, 총인구수)이고,
    그 외 축(A=행정구역(시군구)별, YRE=연령별 …)의 행은 그 차원의 '값'(전국, 서울특별시,
    종로구 …)이다. 실측 예: DT_1B040B3은 387행 중 항목은 T1(세대수) 1건뿐이고
    나머지 386건은 전부 행정구역 차원값이었다.

    UNIT_NM은 항목 행에만 채워져 오고 차원값 행에서는 null이다.
    OBJ_NM_ENG(영문 축 이름, "By Administrative District")는 검색에 쓰지 않으므로 버린다.
    """
    axes: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue

        obj_id = str(row.get("OBJ_ID") or "").strip()
        obj_nm = str(row.get("OBJ_NM") or "").strip()
        itm_id = str(row.get("ITM_ID") or "").strip()
        itm_nm = str(row.get("ITM_NM") or "").strip()
        unit_nm = str(row.get("UNIT_NM") or "").strip()

        axis = axes.setdefault(obj_id, {"obj_id": obj_id or None, "obj_nm": obj_nm or None, "rows": []})
        if not axis["obj_nm"] and obj_nm:
            axis["obj_nm"] = obj_nm
        if itm_id or itm_nm:
            axis["rows"].append({"id": itm_id or None, "nm": itm_nm or None, "unit_nm": unit_nm or None})

    item_axis = axes.pop(ITEM_AXIS_ID, None)
    items = [
        {"itm_id": r["id"], "itm_nm": r["nm"], "unit_nm": r["unit_nm"]}
        for r in (item_axis or {}).get("rows", [])
    ]

    dimensions = []
    for axis in axes.values():
        values = [r["nm"] for r in axis["rows"] if r["nm"]]
        dimensions.append({
            "obj_id": axis["obj_id"],
            "obj_nm": axis["obj_nm"],
            "values": values,
            "value_count": len(values),
        })

    units = sorted({item["unit_nm"] for item in items if item["unit_nm"]})

    return {"dimensions": dimensions, "items": items, "units": units}


def fetch_one(table: dict, *, dump_raw: bool) -> dict:
    """표 1개의 메타를 조회한다. 실패해도 예외를 올리지 않고 status=error로 기록한다."""
    base = {
        "table_key": table["table_key"],
        "org_id": table["org_id"],
        "tbl_id": table["tbl_id"],
        "tbl_nm": table.get("tbl_nm"),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    last_error = ""
    for attempt in range(1, MAX_RETRY + 1):
        try:
            rows = get_meta(table["org_id"], table["tbl_id"], meta_type="ITM")
        except Exception as e:  # noqa: BLE001 - 개별 표 실패는 건너뛰고 계속
            last_error = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
            continue

        if dump_raw:
            print(json.dumps({"table_key": table["table_key"], "raw": rows}, ensure_ascii=False, indent=2), flush=True)

        if not isinstance(rows, list):
            return {**base, "status": "error", "error": f"응답이 리스트가 아님: {rows!r}"}

        return {**base, "status": "ok", **parse_meta_rows(rows)}

    return {**base, "status": "error", "error": last_error}


def main() -> None:
    parser = argparse.ArgumentParser(description="KOSIS 표별 차원·항목 메타 보강 크롤러")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--categories", type=str, default=None,
                        help="대분류 ID 쉼표 구분 (예: A,P2,V). 미지정 시 전체")
    parser.add_argument("--org-id", type=str, default=None, help="특정 기관 코드만 (예: 101)")
    parser.add_argument("--limit", type=int, default=None, help="이번 실행에서 처리할 최대 표 수")
    parser.add_argument("--sleep", type=float, default=SLEEP_SEC, help="호출 간격(초)")
    parser.add_argument("--dump-raw", action="store_true",
                        help="응답 원본 JSON을 그대로 출력 (파서 검증용, --limit과 함께 쓸 것)")
    args = parser.parse_args()

    categories = {c.strip() for c in args.categories.split(",")} if args.categories else None

    tree = json.loads(args.input.read_text(encoding="utf-8"))
    tables = iter_tables(tree, categories)
    if args.org_id:
        tables = [t for t in tables if t["org_id"] == args.org_id]

    done = load_done_keys(args.output)
    pending = [t for t in tables if t["table_key"] not in done]
    if args.limit is not None:
        pending = pending[: args.limit]

    print(
        f"[STATUS] 대상 표 {len(tables):,} | 이미 완료 {len(done):,} | "
        f"이번 실행 {len(pending):,} | 출력 {args.output}",
        flush=True,
    )
    if not pending:
        print("[DONE] 처리할 표가 없습니다.", flush=True)
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    ok_count = 0
    err_count = 0
    consecutive_errors = 0

    # append 모드 + 매 건 flush: 중단해도 그 시점까지는 보존되고 재실행 시 이어진다.
    with args.output.open("a", encoding="utf-8") as handle:
        for i, table in enumerate(pending, start=1):
            record = fetch_one(table, dump_raw=args.dump_raw)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

            if record["status"] == "ok":
                ok_count += 1
                consecutive_errors = 0
            else:
                err_count += 1
                consecutive_errors += 1
                print(f"    [WARN] {record['table_key']} 실패: {record.get('error')}", flush=True)
                if consecutive_errors >= ABORT_AFTER_CONSECUTIVE_ERRORS:
                    print(
                        f"[ABORT] 연속 {consecutive_errors}건 실패 — 일일 호출 한도 소진 또는 "
                        f"API 장애로 판단해 중단합니다. 재실행하면 이 지점부터 이어집니다.",
                        flush=True,
                    )
                    break

            if i % 50 == 0 or i == len(pending):
                elapsed = time.time() - started_at
                rate = i / elapsed if elapsed else 0
                remaining = (len(pending) - i) / rate if rate else 0
                print(
                    f"[PROGRESS] {i:,}/{len(pending):,} ({i / len(pending) * 100:5.1f}%) | "
                    f"성공 {ok_count:,} 실패 {err_count:,} | "
                    f"{rate:.1f}건/초 | 경과 {format_elapsed(elapsed)} | "
                    f"예상 잔여 {format_elapsed(remaining)}",
                    flush=True,
                )

            time.sleep(args.sleep)

    print(
        f"\n=== 종료: 성공 {ok_count:,} | 실패 {err_count:,} | "
        f"소요 {format_elapsed(time.time() - started_at)} ===",
        flush=True,
    )


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
