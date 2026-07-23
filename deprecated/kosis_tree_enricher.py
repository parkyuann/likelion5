"""이미 크롤링해 둔 표 목록에, KOSIS API로 '차원·항목·시점'만 덧붙이는 보강 크롤러.

    kosis_tree_crawler.py      : statisticsList.do 로 트리를 훑어 '표 목록'을 만든다(전체 크롤링).
    kosis_tree_enricher.py(이) : 그 목록의 표마다 statisticsData.do?getMeta 를 불러
                                 '트리에 없던 정보'만 채운다(추가 보강).

트리(kosis_table_tree.json)에는 표당 org_id / 표이름 / stat_id / 카테고리경로 뿐이라,
검색 색인을 만들려면 아래가 더 필요하다. 이 둘은 목록 API에 없고 getMeta에만 있다.

  ① type=ITM  차원명·차원값·항목명·단위   → T2-1 doc_meta_text / doc_item_index / units
  ② type=PRD  수록주기·시작~최신시점       → T2-2 period_types / latest_period
              (표이름·차원·항목까지 똑같고 조사연도만 다른 표들을 가르는 유일한 단서)

표 하나당 API 2회(ITM+PRD)다. 분당 호출은 200건으로 제한되며(2026-07-15 시행,
개발가이드 1.4.2 에러코드 40), 이 제한은 API 키 단위라 여러 대로 나눠도 합산된다 —
같은 키로 동시에 두 곳에서 돌리지 말 것. 전체(약 26.5만 표 × 2회)는 약 49시간이다.

출력은 트리를 덮어쓰지 않고 별도 JSONL(kosis_table_meta.jsonl)에 append 하며,
재실행하면 이미 받은 표는 건너뛴다(중단·재개 가능). 범위는 --categories / --org-id /
--limit 으로 좁힌다. 나중에 kosis_tree_crawler.py 와 한 파이프라인으로 합칠 수 있도록
get_meta 를 그대로 재사용하고 트리를 입력으로만 읽는다.

사용 예 (레포 루트에서):
    # 0) 응답 형태부터 확인 (원본 JSON, 몇 건만)
    venv/Scripts/python.exe src/kosis_tree_enricher.py --limit 2 --dump-raw

    # 1) 대분류 하나만 보강 (인구 = A)
    venv/Scripts/python.exe src/kosis_tree_enricher.py --categories A

    # 2) 이어서 전체 (중단해도 재실행하면 이어짐)
    venv/Scripts/python.exe src/kosis_tree_enricher.py
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

# KOSIS 분당 호출 제한(200)에서 여유를 둔 값. 이 스크립트는 표당 2회(ITM+PRD)를
# 부르므로 아래 값은 '표 수'가 아니라 'API 호출 수' 기준이다.
MAX_CALLS_PER_MIN = 180
MAX_RETRY = 3
RETRY_BACKOFF_SEC = 2.0
RATE_LIMIT_COOLDOWN_SEC = 65  # 에러코드 40(분당 한도)을 만나면 다음 '분'까지 넘긴다
ABORT_AFTER_CONSECUTIVE_ERRORS = 20  # API 장애로 전량 실패할 때 조기 중단
CHECKPOINT_EVERY = 100  # 이 표 수마다 체크포인트 파일을 갱신한다

ITEM_AXIS_ID = "ITEM"  # getMeta(ITM)에서 이 OBJ_ID면 '항목', 그 외면 '분류(차원)'


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class RateLimiter:
    """API 호출 사이 최소 간격을 강제한다. 호출당 1회 wait() 한다.

    응답 지연이 이미 간격을 채웠으면 추가로 자지 않는다 — 지연이 빠른 구간에서만
    실제 대기가 걸린다.
    """

    def __init__(self, per_min: int) -> None:
        self.interval = 60.0 / max(1, per_min)
        self._last = 0.0

    def wait(self) -> None:
        remaining = self.interval - (time.time() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.time()


def is_rate_limited(message: str) -> bool:
    """분당 호출 한도(에러코드 40)로 실패했는지 판별한다.

    kosis_client.get_meta()는 API 오류 응답을 통째로 문자열에 담아 RuntimeError로
    올리므로, 그 안에서 코드 40 / '호출가능건수' 문구를 찾는다.
    """
    return "'40'" in message or '"40"' in message or "호출가능건수" in message


def iter_tables(tree: dict, categories: set[str] | None) -> list[dict]:
    """트리에서 (org_id, tbl_id) 단위 유니크 표 목록을 뽑는다.

    같은 표가 여러 카테고리 경로에 중복 등장하므로 build_kosis_catalog 와 같은
    기준(org_id:tbl_id)으로 중복을 제거한다 — getMeta 를 두 번 부르지 않기 위함.
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
                raw_path = [str(p).strip() for p in (leaf.get("path") or []) if str(p).strip()]
                seen[key] = {
                    "table_key": key,
                    "org_id": org_id,
                    "tbl_id": tbl_id,
                    "tbl_nm": leaf.get("tbl_nm"),
                    "top_id": top_id,
                    "path": raw_path,
                    "path_key": " > ".join(raw_path) if raw_path else top_id,
                }
    return list(seen.values())


def take_whole_paths(tables: list[dict], target: int) -> list[dict]:
    """대략 target개가 될 때까지 path(카테고리 경로) 단위로 통째로 담는다.

    표를 target번째에서 뚝 자르면 같은 카테고리(예: '인구 > 주민등록인구현황')의 표
    일부만 들어가 표본이 어중간해진다. 대신 경로 단위로 담고, 누적이 target에 도달하는
    순간의 경로까지 마저 채운 뒤 멈춘다 — 결과는 target을 조금 넘길 수 있다(경계에서 끊기).
    """
    from collections import OrderedDict

    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for table in tables:
        groups.setdefault(table["path_key"], []).append(table)

    selected: list[dict] = []
    for group in groups.values():
        selected.extend(group)
        if len(selected) >= target:
            break
    return selected


def checkpoint_path(output_path: Path) -> Path:
    """체크포인트(상태 기록) 파일 경로. 출력 파일 옆에 나란히 둔다."""
    return output_path.with_name(output_path.name + ".checkpoint.json")


def write_checkpoint(path: Path, data: dict) -> None:
    """진행 상태를 작은 JSON으로 남긴다.

    데이터(kosis_table_meta.jsonl)는 표마다 append 되므로 그 자체가 '어디까지 했나'의
    진짜 근거다. 이 파일은 그 위에 '언제/얼마나/정상종료인지'를 요약해, 큰 파일을 열지
    않고도 상태를 보고 재개할 수 있게 한다. 원자적 교체(temp→replace)로 쓰다가 꺼져도
    깨지지 않게 한다.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_done_keys(output_path: Path) -> set[str]:
    """이미 수집한 table_key를 읽어 재실행 시 건너뛴다. 깨진 줄은 무시한다."""
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


def parse_itm_rows(rows: list) -> dict:
    """getMeta(type=ITM) 응답을 차원/항목/단위로 정리한다.

    모든 행은 (OBJ_ID=어느 축) + (ITM_ID/ITM_NM=그 축 안의 코드·이름) 쌍이다.
    OBJ_ID 가 'ITEM' 이면 진짜 항목(총인구수 …), 그 외 축(A=행정구역, YRE=연령 …)의
    행은 그 차원의 '값'(전국, 서울특별시, 종로구 …)이다. 차원값에는 UP_ITM_ID(상위코드),
    OBJ_ID_SN(순번)이 있어 계층·정렬을 복원할 수 있으므로 함께 보존한다.
    영문명(OBJ_NM_ENG/ITM_NM_ENG/UNIT_ENG_NM)·UNIT_ID 는 한국어 검색에 불필요해 버린다.
    """
    axes: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        obj_id = str(row.get("OBJ_ID") or "").strip()
        obj_nm = str(row.get("OBJ_NM") or "").strip()
        axis = axes.setdefault(obj_id, {"obj_id": obj_id or None, "obj_nm": obj_nm or None, "rows": []})
        if not axis["obj_nm"] and obj_nm:
            axis["obj_nm"] = obj_nm
        itm_id = str(row.get("ITM_ID") or "").strip()
        itm_nm = str(row.get("ITM_NM") or "").strip()
        if itm_id or itm_nm:
            axis["rows"].append({
                "id": itm_id or None,
                "nm": itm_nm or None,
                "unit_nm": str(row.get("UNIT_NM") or "").strip() or None,
                "up_id": str(row.get("UP_ITM_ID") or "").strip() or None,
                "sn": str(row.get("OBJ_ID_SN") or "").strip() or None,
            })

    item_axis = axes.pop(ITEM_AXIS_ID, None)
    items = [
        {"itm_id": r["id"], "itm_nm": r["nm"], "unit_nm": r["unit_nm"]}
        for r in (item_axis or {}).get("rows", [])
    ]

    dimensions = []
    for axis in axes.values():
        values = [
            {"id": r["id"], "nm": r["nm"], "up_id": r["up_id"], "sn": r["sn"]}
            for r in axis["rows"] if r["nm"]
        ]
        dimensions.append({
            "obj_id": axis["obj_id"],
            "obj_nm": axis["obj_nm"],
            "values": values,
            "value_count": len(values),
        })

    units = sorted({item["unit_nm"] for item in items if item["unit_nm"]})
    return {"dimensions": dimensions, "items": items, "units": units}


def parse_prd_rows(rows: list) -> dict:
    """getMeta(type=PRD) 응답을 수록주기/시점으로 정리한다.

    한 표가 여러 주기(월·년)를 가질 수 있어 리스트로 온다. 예:
      [{"PRD_SE":"월","STRT_PRD_DE":"2008.01","END_PRD_DE":"2026.06"},
       {"PRD_SE":"년","STRT_PRD_DE":"2008","END_PRD_DE":"2025"}]
    latest_period 는 END_PRD_DE 중 문자열상 최대값으로 근사한다 — 월("2026.06")과
    연("2025") 형식이 섞이지만 앞자리 연도부터 비교되어 대체로 최신을 가리킨다.
    정밀 비교가 필요하면 periods 원본을 쓰면 된다.
    """
    periods = []
    period_types = []
    ends = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prd_se = str(row.get("PRD_SE") or "").strip()
        start = str(row.get("STRT_PRD_DE") or "").strip()
        end = str(row.get("END_PRD_DE") or "").strip()
        periods.append({"prd_se": prd_se or None, "start": start or None, "end": end or None})
        if prd_se and prd_se not in period_types:
            period_types.append(prd_se)
        if end:
            ends.append(end)
    return {
        "periods": periods,
        "period_types": period_types,
        "latest_period": max(ends) if ends else None,
    }


def call_with_retry(limiter: RateLimiter, org_id: str, tbl_id: str, meta_type: str) -> tuple[list | None, str]:
    """getMeta 한 종류를 rate limit·재시도와 함께 호출한다. (rows, error) 반환."""
    last_error = ""
    for attempt in range(1, MAX_RETRY + 1):
        limiter.wait()
        try:
            rows = get_meta(org_id, tbl_id, meta_type=meta_type)
        except Exception as e:  # noqa: BLE001 - 개별 표 실패는 건너뛰고 계속
            last_error = f"{type(e).__name__}: {e}"
            if is_rate_limited(last_error):
                print(f"    [RATE] 분당 한도 초과 감지 — {RATE_LIMIT_COOLDOWN_SEC}초 대기", flush=True)
                time.sleep(RATE_LIMIT_COOLDOWN_SEC)
                continue
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
            continue
        if not isinstance(rows, list):
            return None, f"{meta_type} 응답이 리스트가 아님: {rows!r}"
        return rows, ""
    return None, last_error


def fetch_one(table: dict, limiter: RateLimiter, *, with_prd: bool, dump_raw: bool) -> dict:
    """표 1개의 메타(ITM, 선택적으로 PRD)를 조회한다. 실패해도 예외를 올리지 않는다."""
    base = {
        "table_key": table["table_key"],
        "org_id": table["org_id"],
        "tbl_id": table["tbl_id"],
        "tbl_nm": table.get("tbl_nm"),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    itm_rows, err = call_with_retry(limiter, table["org_id"], table["tbl_id"], "ITM")
    if dump_raw and itm_rows is not None:
        print(json.dumps({"table_key": table["table_key"], "ITM": itm_rows}, ensure_ascii=False, indent=1), flush=True)
    if itm_rows is None:
        return {**base, "status": "error", "error": f"ITM: {err}"}
    record = {**base, "status": "ok", **parse_itm_rows(itm_rows)}

    if with_prd:
        prd_rows, err = call_with_retry(limiter, table["org_id"], table["tbl_id"], "PRD")
        if dump_raw and prd_rows is not None:
            print(json.dumps({"table_key": table["table_key"], "PRD": prd_rows}, ensure_ascii=False, indent=1), flush=True)
        if prd_rows is None:
            # ITM은 받았으니 표는 살리되, 시점만 누락으로 표시한다.
            record.update({"periods": [], "period_types": [], "latest_period": None, "prd_error": err})
        else:
            record.update(parse_prd_rows(prd_rows))

    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="KOSIS 표별 차원·항목·시점 보강 크롤러(추가 크롤링)")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--categories", type=str, default=None,
                        help="대분류 ID 쉼표 구분 (예: A,P2,V). 미지정 시 전체")
    parser.add_argument("--org-id", type=str, default=None, help="특정 기관 코드만 (예: 101)")
    parser.add_argument("--limit", type=int, default=None, help="이번 실행에서 처리할 최대 표 수")
    parser.add_argument("--whole-paths", action="store_true",
                        help="--limit 을 표 단위가 아니라 path(카테고리 경로) 단위로 끊는다 "
                             "(표본이 target을 조금 넘길 수 있음)")
    parser.add_argument("--max-per-min", type=int, default=MAX_CALLS_PER_MIN,
                        help=f"분당 최대 API 호출 수 (KOSIS 상한 200, 기본 {MAX_CALLS_PER_MIN})")
    parser.add_argument("--skip-prd", action="store_true",
                        help="ITM(차원·항목)만 받고 PRD(시점)는 건너뛴다 — 호출·시간 절반")
    parser.add_argument("--dump-raw", action="store_true",
                        help="응답 원본 JSON을 그대로 출력 (파서 검증용, --limit과 함께 쓸 것)")
    parser.add_argument("--status", action="store_true",
                        help="크롤링하지 않고 마지막 체크포인트(진행 상태)만 출력하고 종료")
    args = parser.parse_args()

    ckpt_path = checkpoint_path(args.output)

    # --status: 큰 파일을 열지 않고 진행 상태만 확인한다.
    if args.status:
        if ckpt_path.exists():
            print(ckpt_path.read_text(encoding="utf-8"))
        else:
            print(f"[STATUS] 체크포인트 없음 ({ckpt_path}) — 아직 실행 이력이 없습니다.")
        return

    categories = {c.strip() for c in args.categories.split(",")} if args.categories else None
    with_prd = not args.skip_prd

    tree = json.loads(args.input.read_text(encoding="utf-8"))
    tables = iter_tables(tree, categories)
    if args.org_id:
        tables = [t for t in tables if t["org_id"] == args.org_id]

    done = load_done_keys(args.output)
    pending = [t for t in tables if t["table_key"] not in done]
    if args.limit is not None:
        if args.whole_paths:
            pending = take_whole_paths(pending, args.limit)
        else:
            pending = pending[: args.limit]

    calls_per_table = 2 if with_prd else 1
    limiter = RateLimiter(args.max_per_min)
    est_seconds = len(pending) * calls_per_table * limiter.interval

    print(
        f"[STATUS] 대상 표 {len(tables):,} | 이미 완료 {len(done):,} | "
        f"이번 실행 {len(pending):,} | PRD {'포함' if with_prd else '생략'} | 출력 {args.output}",
        flush=True,
    )
    print(
        f"[RATE] 분당 {args.max_per_min} 호출 | 표당 {calls_per_table}회 | "
        f"예상 소요 {format_elapsed(est_seconds)}",
        flush=True,
    )
    if ckpt_path.exists():
        print(f"[CHECKPOINT] 이전 체크포인트 발견 — {ckpt_path.name} (이어서 진행)", flush=True)
    if not pending:
        print("[DONE] 처리할 표가 없습니다.", flush=True)
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok_count = 0
    err_count = 0
    consecutive_errors = 0
    run_params = {
        "categories": sorted(categories) if categories else None,
        "org_id": args.org_id,
        "with_prd": with_prd,
        "max_per_min": args.max_per_min,
    }

    def save_checkpoint(status: str, processed: int, last_key: str | None) -> None:
        write_checkpoint(ckpt_path, {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": status,  # running / completed / aborted
            "output": str(args.output.relative_to(ROOT)) if args.output.is_relative_to(ROOT) else str(args.output),
            "target_tables": len(tables),
            "completed_tables": len(done) + ok_count,  # 누적(이전 실행 포함) 성공 표 수
            "remaining_tables": len(tables) - (len(done) + ok_count),
            "this_run": {
                "started_at": started_iso,
                "processed": processed,
                "ok": ok_count,
                "error": err_count,
                "last_table_key": last_key,
                "elapsed": format_elapsed(time.time() - started_at),
            },
            "params": run_params,
        })

    # append 모드 + 매 건 flush: 중단해도 그 시점까지 보존되고 재실행 시 이어진다.
    with args.output.open("a", encoding="utf-8") as handle:
        for i, table in enumerate(pending, start=1):
            record = fetch_one(table, limiter, with_prd=with_prd, dump_raw=args.dump_raw)
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
                        f"[ABORT] 연속 {consecutive_errors}건 실패 — API 장애로 판단해 중단합니다. "
                        f"재실행하면 이 지점부터 이어집니다.",
                        flush=True,
                    )
                    save_checkpoint("aborted", i, record["table_key"])
                    break

            if i % CHECKPOINT_EVERY == 0:
                save_checkpoint("running", i, record["table_key"])

            if i % 50 == 0 or i == len(pending):
                elapsed = time.time() - started_at
                rate = i / elapsed if elapsed else 0
                remaining = (len(pending) - i) / rate if rate else 0
                print(
                    f"[PROGRESS] {i:,}/{len(pending):,} ({i / len(pending) * 100:5.1f}%) | "
                    f"성공 {ok_count:,} 실패 {err_count:,} | {rate:.1f}표/초 | "
                    f"경과 {format_elapsed(elapsed)} | 예상 잔여 {format_elapsed(remaining)}",
                    flush=True,
                )
        else:
            # for-else: break 없이 끝까지 돈 경우 = 이번 실행분 완주.
            save_checkpoint("completed", len(pending), pending[-1]["table_key"] if pending else None)

    print(
        f"\n=== 종료: 성공 {ok_count:,} | 실패 {err_count:,} | "
        f"소요 {format_elapsed(time.time() - started_at)} | "
        f"체크포인트 {ckpt_path.name} ===",
        flush=True,
    )


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
