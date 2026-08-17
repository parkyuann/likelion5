"""마스터 표 목록에 차원·항목·시점·단위 메타를 붙인다 (v5, 승계 지원).

무엇이 v4와 다른가
------------------
① 입력이 트리가 아니라 병합 마스터(kosis_table_tree_master_v5.jsonl)다.
② **직전 메타 승계(--reuse-meta)**: 안 바뀐 표는 getMeta 를 부르지 않고 물려받는다.
   판정은 직전 메타에 send_de 가 있느냐로 자동 분기한다(can_reuse 참고):
     · 정밀(2판 이후): 현재 send_de == 직전 send_de → 승계. 경계 없음.
     · 근사(첫 판 v4→v5): v4엔 send_de 가 없어 send_de <= fetched_at 로 근사.

   ★ 이번 판 메타에 **send_de 를 반드시 저장**한다(carry_over·fetch 양쪽). 다음 판이
     이 send_de 로 정밀 비교하기 위해서다. v4 의 send_de 부재 문제는 이번 판으로 끝난다.

   근거: 표의 차원·항목 구조는 갱신(send_de 변경) 없이는 바뀌지 않는다. 이것으로 메타
   보강 비용이 66시간 → 수 시간으로 준다(전량 대신 신규·변경분만).

③ **날짜 스냅샷**: 출력이 kosis_table_meta_v5_YYMMDD.jsonl 로 판별 보존된다.
   덮어쓰지 않으므로 과거 판을 참조할 수 있고, 다음 판이 직전 파일을 승계 기준으로 쓴다.

병렬화·재시도·파싱은 검증된 기존 코드(src/kosis_meta_enricher.py)를 그대로 import 한다
— 파싱 로직을 두 벌로 두지 않기 위해서다. 따라서 이 스크립트는 프로젝트 전체가 있어야
동작한다(src/kosis_meta_enricher.py, src/kosis_client.py 필요).

사용 예 (레포 루트에서):
    # 0) 형태 확인 (몇 건만)
    venv/Scripts/python.exe src/크롤링_v5/kosis_meta_enricher_v5.py --limit 5

    # 1) 전체 — v4 승계 켜고 (권장)
    venv/Scripts/python.exe src/크롤링_v5/kosis_meta_enricher_v5.py \
        --reuse-meta data/kosis_table_meta_v4.jsonl

    # 2) 진행 상황만
    venv/Scripts/python.exe src/크롤링_v5/kosis_meta_enricher_v5.py --status
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
# 검증된 기존 로직을 그대로 재사용한다 (병렬 RateLimiter, 재시도, ITM/PRD/UNIT 파싱).
from src.kosis_meta_enricher import (  # noqa: E402
    RateLimiter,
    checkpoint_path,
    fetch_one,
    format_elapsed,
    load_done_keys,
    write_checkpoint,
)

DEFAULT_MASTER = ROOT / "data" / "크롤링_v5" / "kosis_table_tree_master_v5.jsonl"
# 출력은 판(snapshot)별로 날짜를 붙여 보존한다 — 매 판을 덮어쓰지 않는다(§정기업데이트 가이드).
# 예: kosis_table_meta_v5_260811.jsonl. 다음 판이 이 파일을 --reuse-meta 로 가리킨다.
_TODAY = datetime.now().strftime("%y%m%d")
DEFAULT_OUTPUT = ROOT / "data" / "크롤링_v5" / f"kosis_table_meta_v5_{_TODAY}.jsonl"
DEFAULT_V4 = ROOT / "data" / "kosis_table_meta_v4.jsonl"

MAX_CALLS_PER_MIN = 180
DEFAULT_WORKERS = 8
CHECKPOINT_EVERY = 200
ABORT_AFTER_CONSECUTIVE_ERRORS = 20

# 승계 시 v4 메타에서 그대로 가져올 필드 (수집 메타 본문)
CARRY_FIELDS = ("dimensions", "items", "units", "unit_source",
                "periods", "period_types", "latest_period")


def iter_master(path: Path, views: set[str] | None) -> list[dict]:
    """마스터를 읽어 표 단위 목록을 만든다. fetch_one 이 받는 형식으로 정규화."""
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if views and not (set(r.get("view_codes", [])) & views):
                continue
            out.append({
                "table_key": r["table_key"],
                "org_id": r["org_id"],
                "tbl_id": r["tbl_id"],
                "tbl_nm": r.get("tbl_nm"),
                "send_de": r.get("send_de"),   # 승계 판정용
                "status": r.get("status", "active"),  # retired 는 getMeta 금지(삭제된 표)
            })
    return out


def parse_iso_date(s: str | None) -> str | None:
    """fetched_at(ISO datetime) → YYYY-MM-DD. send_de 와 문자열 비교 가능하게."""
    if not s:
        return None
    return s[:10]


def load_v4_meta(path: Path) -> dict[str, dict]:
    """v4 메타를 table_key → 레코드로 인덱싱한다 (status·fetched_at 포함)."""
    idx = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            key = r.get("table_key")
            if key:
                idx[key] = r
    return idx


def can_reuse(table: dict, prev: dict | None) -> tuple[bool, str]:
    """이 표의 직전 메타를 그대로 써도 되는가. (승계여부, 방식) 반환.

    두 방식을 자동 분기한다:

    ① **정밀 비교 (2판 이후, 권장)** — 직전 메타에 send_de 가 있으면
       현재 send_de == 직전 send_de 이면 승계. send_de(YYYY-MM-DD, 실측 100% 일단위)는
       표가 바뀌면 반드시 바뀌므로, 같으면 안 바뀐 게 확실하다. 경계 문제 없음.

    ② **근사 승계 (첫 판 v4→v5 한정)** — v4 메타엔 send_de 가 없다. 이때만
       현재 send_de <= v4 fetched_at 로 근사한다. 수집 당월(경계) 표는
       낡은 채 승계될 수 있으나, 다음 판에서 ①로 자동 교정된다.

    현재 send_de 가 없으면(분류 밖 표 등) 승계하지 않고 새로 받는다.
    """
    if prev is None or prev.get("status") != "ok":
        return False, "no_prev"
    send_de = table.get("send_de")
    if not send_de:
        return False, "no_send_de"
    prev_send_de = prev.get("send_de")
    if prev_send_de:
        # ① 정밀 비교
        return (send_de == prev_send_de), "exact"
    # ② 근사 (v4 등 send_de 없는 직전 메타)
    fetched = parse_iso_date(prev.get("fetched_at"))
    if not fetched:
        return False, "no_basis"
    return (send_de <= fetched), "approx"


def carry_over(table: dict, prev: dict) -> dict:
    """직전 메타 본문을 이번 판 레코드로 옮긴다. send_de 를 반드시 심는다."""
    rec = {
        "table_key": table["table_key"],
        "org_id": table["org_id"],
        "tbl_id": table["tbl_id"],
        "tbl_nm": table.get("tbl_nm"),
        "send_de": table.get("send_de"),   # ★ 다음 판 정밀 비교의 기준. 반드시 저장
        "fetched_at": prev.get("fetched_at"),
        "status": "ok",
        "meta_source": "reused",
    }
    for k in CARRY_FIELDS:
        if k in prev:
            rec[k] = prev[k]
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description="KOSIS 마스터 메타 보강 (v5, 승계 지원)")
    ap.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--reuse-meta", type=Path, default=None,
                    help=f"승계할 기존 메타 (예: {DEFAULT_V4.relative_to(ROOT)})")
    ap.add_argument("--views", type=str, default=None, help="특정 뷰만 (쉼표 구분)")
    ap.add_argument("--limit", type=int, default=None, help="이번 실행 최대 표 수")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--max-per-min", type=int, default=MAX_CALLS_PER_MIN)
    ap.add_argument("--skip-unit", action="store_true")
    ap.add_argument("--always-unit", action="store_true")
    ap.add_argument("--status", action="store_true", help="크롤 없이 진행 상황만 출력")
    args = ap.parse_args()

    ckpt = checkpoint_path(args.output)
    if args.status:
        print(ckpt.read_text(encoding="utf-8") if ckpt.exists()
              else f"[STATUS] 체크포인트 없음 ({ckpt})")
        return

    views = {v.strip() for v in args.views.split(",")} if args.views else None
    with_prd = True
    with_unit = not args.skip_unit

    tables = iter_master(args.master, views)
    print(f"[STATUS] 마스터 표 {len(tables):,}건", flush=True)

    # 이미 이번 출력에 쓴 것(재개) 제외
    done = load_done_keys(args.output)
    pending_all = [t for t in tables if t["table_key"] not in done]

    # 승계 vs 재보강 분리
    prev_idx = load_v4_meta(args.reuse_meta) if args.reuse_meta else {}
    if prev_idx:
        has_send = sum(1 for r in prev_idx.values() if r.get("send_de"))
        mode = "정밀비교(send_de==)" if has_send else "근사(fetched_at, 첫 판)"
        print(f"[REUSE] 직전 메타 {len(prev_idx):,}건 로드 | 승계방식: {mode}", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    reused = 0
    retired_skipped = 0
    reuse_methods = {"exact": 0, "approx": 0}
    to_fetch = []
    # 승계분은 즉시 기록(호출 0), 나머지는 fetch 큐로
    with args.output.open("a", encoding="utf-8") as handle:
        for t in pending_all:
            prev = prev_idx.get(t["table_key"])
            if t.get("status") == "retired":
                # KOSIS 에서 삭제된 표 — getMeta 하면 err. 직전 메타를 그대로 보존.
                if prev is not None:
                    rec = carry_over(t, prev)
                    rec["status"] = "retired"
                    rec["meta_source"] = "reused_retired"
                    handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                retired_skipped += 1
                continue
            ok, method = can_reuse(t, prev)
            if ok:
                handle.write(json.dumps(carry_over(t, prev), ensure_ascii=False) + "\n")
                reused += 1
                reuse_methods[method] = reuse_methods.get(method, 0) + 1
            else:
                to_fetch.append(t)
        handle.flush()
    if retired_skipped:
        print(f"[RETIRE] 삭제된 표 {retired_skipped:,}건 — getMeta 생략, 직전 메타 보존", flush=True)
    if reused:
        detail = " ".join(f"{k}={v:,}" for k, v in reuse_methods.items() if v)
        print(f"[REUSE] 승계 {reused:,}건 ({detail})", flush=True)

    if args.limit is not None:
        to_fetch = to_fetch[: args.limit]

    limiter = RateLimiter(args.max_per_min)
    base = 1 + (1 if with_prd else 0)
    est = len(to_fetch) * (base + (1 if with_unit else 0)) * limiter.interval
    print(f"[PLAN] 총 {len(tables):,} | 이미완료 {len(done):,} | "
          f"승계 {reused:,} | getMeta 대상 {len(to_fetch):,}", flush=True)
    print(f"[RATE] 분당 {args.max_per_min} | 워커 {args.workers} | "
          f"예상 최대 {format_elapsed(est)}", flush=True)
    if not to_fetch:
        print("[DONE] getMeta 대상 없음 — 승계로 완료.", flush=True)
        _save(ckpt, args, len(tables), len(done) + reused, "completed", reused, 0, 0)
        return

    fetch = partial(fetch_one, limiter=limiter, with_prd=with_prd, with_unit=with_unit,
                    always_unit=args.always_unit, dump_raw=False)
    ok = err = 0
    consec = 0
    started = time.time()
    processed = 0
    with args.output.open("a", encoding="utf-8") as handle, \
            ThreadPoolExecutor(max_workers=args.workers) as ex:
        chunk = max(args.workers * 8, 200)
        aborted = False
        for start in range(0, len(to_fetch), chunk):
            batch = to_fetch[start:start + chunk]
            # ex.map 은 제출 순서를 보존하므로 원본 표와 zip 해 send_de 를 주입한다
            # (send_de 는 getMeta 응답이 아니라 마스터에서 온다 — 다음 판 비교의 기준).
            for src, rec in zip(batch, ex.map(fetch, batch)):
                processed += 1
                rec["send_de"] = src.get("send_de")
                rec["meta_source"] = "fetched_v5"
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                handle.flush()
                if rec["status"] == "ok":
                    ok += 1; consec = 0
                else:
                    err += 1; consec += 1
                    print(f"    [WARN] {rec['table_key']} 실패: {rec.get('error')}", flush=True)
                    if consec >= ABORT_AFTER_CONSECUTIVE_ERRORS:
                        print(f"[ABORT] 연속 {consec}건 실패 — 중단(재실행 시 이어짐)", flush=True)
                        _save(ckpt, args, len(tables), len(done) + reused + ok, "aborted", reused, ok, err)
                        aborted = True
                        break
                if processed % CHECKPOINT_EVERY == 0:
                    _save(ckpt, args, len(tables), len(done) + reused + ok, "running", reused, ok, err)
                if processed % 100 == 0 or processed == len(to_fetch):
                    el = time.time() - started
                    rate = processed / el if el else 0
                    rem = (len(to_fetch) - processed) / rate if rate else 0
                    print(f"[PROGRESS] {processed:,}/{len(to_fetch):,} "
                          f"({processed/len(to_fetch)*100:5.1f}%) | 성공 {ok:,} 실패 {err:,} | "
                          f"{rate:.1f}표/초 | 잔여 {format_elapsed(rem)}", flush=True)
            if aborted:
                break
        else:
            _save(ckpt, args, len(tables), len(done) + reused + ok, "completed", reused, ok, err)

    print(f"\n=== 종료: 승계 {reused:,} | 신규수집 {ok:,} | 실패 {err:,} | "
          f"소요 {format_elapsed(time.time()-started)} ===", flush=True)


def _save(ckpt, args, total, completed, status, reused, ok, err):
    write_checkpoint(ckpt, {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status, "output": str(args.output.name),
        "target_tables": total, "completed_tables": completed,
        "reused": reused, "fetched_ok": ok, "fetched_error": err,
    })


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
