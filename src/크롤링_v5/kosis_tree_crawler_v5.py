"""KOSIS 서비스뷰별 분류 트리를 전수 크롤링해 리프 통계표 목록을 만든다 (v5).

기존 `src/kosis_tree_crawler.py`와 무엇이 다른가
-----------------------------------------------
기존 크롤러는 네 가지가 막혀 재사용할 수 없었다(계획서 §5.1).

  ① `TOP_CATEGORIES`에 MT_ZTITLE 대분류 30개가 하드코딩 → 다른 뷰를 못 훑는다
  ② `list_tables(parent_id=...)`로 vw_cd를 안 넘김 → 항상 주제별 뷰
  ③ 단일 출력 파일 + `result[top_id]` 구조 → 뷰를 섞으면 키가 충돌한다
  ④ err 응답을 "표 없음"으로 오해 → 서브트리를 조용히 버린다 (실패 기록도 없음)

v5는 뷰를 인자로 받고(R1/R8), 실패를 파일로 남기며(R3), 노드 단위로 재개하고(R5),
지금까지 버리던 필드를 함께 저장한다(R9/R10).

수집 필드 (계획서 §3)
---------------------
API가 통계표 행에 8개 필드를 주는데 기존 트리는 4개만 담고 있었다. v5는 전부 담는다.

  기존: org_id · tbl_id · tbl_nm · stat_id · path(이름)
  추가: send_de · rec_tbl_se · vw_cd · path_ids(LIST_ID 경로)

  · send_de   — 표 단위 최종갱신일. 이게 있어야 기존 kosis_table_meta_v4.jsonl을
                승계할지 재보강할지 판정할 수 있다(계획서 §6.2, 63시간 → 8시간).
  · rec_tbl_se — KOSIS 검색결과의 「추천」 배지. 표본 4.12%가 Y이고, 뉴스가 실제로
                인용하는 대표표들이다. 랭킹 가산점 피처(하드 필터로 쓰지 말 것).
  · path_ids  — 분류명은 개편되지만 LIST_ID는 안정적이라, 재크롤 diff에서
                "경로가 바뀐 것"과 "표가 옮겨진 것"을 구분할 수 있다.

출력 (계획서 §7)
----------------
    data/크롤링_v5/tree_v5/kosis_table_tree_{vwCd}_v5.json        완주본
    data/크롤링_v5/tree_v5/kosis_table_tree_{vwCd}_v5.leaves.jsonl 진행 중 리프(append)
    data/크롤링_v5/tree_v5/kosis_table_tree_{vwCd}_v5.checkpoint.json 큐 상태
    data/크롤링_v5/tree_v5/kosis_table_tree_manifest_v5.json      뷰별 진행 요약
    data/크롤링_v5/kosis_crawl_failures_v5.jsonl                  실패 노드(전 뷰 공통)

리프를 메모리에 쌓아 두고 통째로 다시 쓰지 않는 이유: MT_ZTITLE은 리프가 26만 건
(약 100MB)이라 체크포인트마다 재직렬화하면 그 자체가 병목이 된다. 리프는 발견 즉시
JSONL에 append 하고, 체크포인트는 큐·방문·실패 목록만 담는다(수 MB).

사용 예 (레포 루트에서):
    # 공식 통계목록(개발가이드 게재분) 중 작은 뷰부터 검증
    venv/Scripts/python.exe src/크롤링_v5/kosis_tree_crawler_v5.py --views MT_GTITLE03

    # 공식 목록 전체 (영문 미러 제외) — 큰 뷰는 시간이 오래 걸린다
    venv/Scripts/python.exe src/크롤링_v5/kosis_tree_crawler_v5.py --preset official

    # 중단 후 이어하기 (기본 동작). 처음부터 다시 하려면 --restart
    venv/Scripts/python.exe src/크롤링_v5/kosis_tree_crawler_v5.py --views MT_OTITLE

    # 실패 노드만 재시도
    venv/Scripts/python.exe src/크롤링_v5/kosis_tree_crawler_v5.py --retry-failures

    # 진행 상황만 보기 (호출 없음)
    venv/Scripts/python.exe src/크롤링_v5/kosis_tree_crawler_v5.py --status
"""
import argparse
import json
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kosis_client_v5 import list_tables  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent  # likelion5/
OUT_DIR = ROOT / "data" / "크롤링_v5" / "tree_v5"
FAILURES_PATH = ROOT / "data" / "크롤링_v5" / "kosis_crawl_failures_v5.jsonl"
MANIFEST_PATH = OUT_DIR / "kosis_table_tree_manifest_v5.json"

# ── 서비스뷰 프리셋 (계획서 §2) ────────────────────────────────────────────────
# official : 공유서비스 개발가이드 '통계목록'의 vwCd 설명에 게재된 것 중 영문 미러 제외.
#            먼저 이것부터 돌려 표 수를 확인하고, 그 다음 미게재 뷰로 넘어간다.
# web_only : 웹 메뉴에는 있는데 개발가이드에서 빠진 것. 둘 다 ZTITLE 대비 100% 신규다.
# unofficial: 웹·JS·문서 어디에도 없는 순수 API 전용(구 분류·큐레이션). P2'에서 판정.
OFFICIAL_VIEWS = [
    "MT_ZTITLE", "MT_OTITLE", "MT_GTITLE01", "MT_GTITLE02",
    "MT_CHOSUN_TITLE", "MT_HANKUK_TITLE", "MT_STOP_TITLE",
    "MT_RTITLE", "MT_BUKHAN", "MT_TM1_TITLE", "MT_TM2_TITLE",
]
WEB_ONLY_VIEWS = ["MT_GTITLE03", "MT_RTITLE01"]
UNOFFICIAL_VIEWS = ["MT_PTITLE", "MT_ATITLE01", "MT_ATITLE02"]
MIRROR_VIEWS = ["MT_ETITLE"]  # 영문 미러. 기본 수집 대상 아님

PRESETS = {
    "official": OFFICIAL_VIEWS,
    "web_only": WEB_ONLY_VIEWS,
    "web": OFFICIAL_VIEWS + WEB_ONLY_VIEWS,   # 계획서가 말하는 '13개 뷰'
    "unofficial": UNOFFICIAL_VIEWS,
    "mirror": MIRROR_VIEWS,
}

MAX_CALLS_PER_MIN = 180        # KOSIS 상한 200에서 여유를 둔 값
MAX_RETRY = 4                  # R2
RETRY_BACKOFF_SEC = 2.0
RATE_LIMIT_COOLDOWN_SEC = 65   # err 40을 만나면 다음 '분'까지 넘긴다
CHECKPOINT_EVERY = 200         # 이 호출 수마다 큐 상태를 디스크에 남긴다
PROGRESS_EVERY = 50


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class RateLimiter:
    """호출 간 최소 간격을 강제한다 (kosis_meta_enricher.py의 패턴을 그대로 가져옴).

    슬롯 예약만 잠금 안에서 하고 sleep은 밖에서 하므로, 나중에 병렬화해도 그대로 쓴다.
    KOSIS 분당 한도는 API 키 단위라 여러 스크립트를 동시에 돌리면 합산된다 — 크롤 담당은
    한 사람으로 고정할 것.
    """

    def __init__(self, per_min: int) -> None:
        self.interval = 60.0 / max(1, per_min)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            slot = max(time.time(), self._next)
            self._next = slot + self.interval
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)


def is_rate_limited(message: str) -> bool:
    """분당 호출 한도(에러코드 40)로 실패했는지 판별한다.

    kosis_client_v5.list_tables()는 오류 응답을 통째로 문자열에 담아 RuntimeError로
    올리므로, 그 안에서 코드 40 / '호출가능건수' 문구를 찾는다.
    """
    return "'40'" in message or '"40"' in message or "호출가능건수" in message


def call_with_retry(limiter: RateLimiter, vw_cd: str, parent_id: str) -> tuple[list | None, str]:
    """list_tables 한 번을 rate limit·재시도와 함께 호출한다. (rows, error) 반환.

    err 30(자식 없는 정상 노드)은 클라이언트가 이미 []로 바꿔주므로 여기 오지 않는다.
    err 40은 분당 한도라 65초 쉬고 재시도한다 — 이걸 실패로 처리하면 기존 크롤러와
    똑같이 서브트리를 잃는다(계획서 §1.3).
    """
    last_error = ""
    for attempt in range(1, MAX_RETRY + 1):
        limiter.wait()
        try:
            return list_tables(vw_cd, parent_id), ""
        except Exception as e:  # noqa: BLE001 - 노드 단위 실패는 기록하고 계속
            last_error = f"{type(e).__name__}: {e}"
            if is_rate_limited(last_error):
                print(f"    [RATE] 분당 한도 초과 — {RATE_LIMIT_COOLDOWN_SEC}초 대기", flush=True)
                time.sleep(RATE_LIMIT_COOLDOWN_SEC)
                continue
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
    return None, last_error


# ── 경로 헬퍼 ────────────────────────────────────────────────────────────────
def view_paths(vw_cd: str) -> dict[str, Path]:
    base = OUT_DIR / f"kosis_table_tree_{vw_cd}_v5"
    return {
        "final": base.with_suffix(".json"),
        "leaves": Path(str(base) + ".leaves.jsonl"),
        "checkpoint": Path(str(base) + ".checkpoint.json"),
    }


def write_atomic(path: Path, text: str) -> None:
    """쓰다가 꺼져도 파일이 깨지지 않도록 temp에 쓰고 교체한다."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def truncate_leaves_to_checkpoint(leaves_path: Path, leaf_count: int) -> None:
    """재개 전에 리프 파일을 체크포인트 시점 길이로 되돌린다.

    왜 필요한가 — 리프는 발견 즉시 append 하지만 체크포인트는 CHECKPOINT_EVERY(200)콜마다
    저장한다. 그래서 중간에 죽으면 리프 파일이 체크포인트보다 앞서 있다(최대 200콜치).
    그대로 재개하면 크롤러는 체크포인트를 믿고 그 구간을 다시 훑는데, 거기서 나온 표들이
    파일에 한 번 더 붙어 leaf_count 가 부풀어 오른다(표를 잃는 건 아니지만 검증이 깨진다).

    앞에서부터 leaf_count 줄까지가 정확히 체크포인트 시점의 상태다 — save_checkpoint()가
    leaves_fp.flush() 를 먼저 하고 나서 체크포인트를 쓰기 때문에 이 대응이 보장된다.
    뒤쪽 잉여분은 버려도 안전하다. 어차피 재크롤로 같은 자리에 다시 채워진다.

    '같은 표는 지운다' 식의 사후 중복제거를 쓰지 않는 이유: 한 표가 여러 분류 경로에
    걸리는 건 정상이라(실측 MT_TM1_TITLE 8건 등) 정상 데이터까지 지울 위험이 있다.
    """
    if not leaves_path.exists():
        return
    tmp = leaves_path.with_suffix(leaves_path.suffix + ".trunc")
    kept = extra = 0
    with leaves_path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
        for line in src:
            if kept < leaf_count:
                dst.write(line)
                kept += 1
            else:
                extra += 1
    if extra:
        tmp.replace(leaves_path)
        print(f"  [TRUNC] 체크포인트 이후 리프 {extra:,}건 제거 "
              f"(재크롤로 다시 채워짐) — {kept:,}건 유지", flush=True)
    else:
        tmp.unlink(missing_ok=True)


def append_failure(vw_cd: str, parent_id: str, path: list[str], error: str) -> None:
    """실패 노드를 영구 기록한다 (R3).

    기존 크롤러는 [WARN] 한 줄만 찍고 끝이라 어느 노드가 유실됐는지 사후에 알 방법이
    없었다. 여기 남겨두면 --retry-failures 로 그 노드들만 다시 칠 수 있다.
    """
    FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FAILURES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "failed_at": now_iso(), "vw_cd": vw_cd, "parent_id": parent_id,
            "path": path, "error": error, "resolved": False,
        }, ensure_ascii=False) + "\n")


# ── 크롤 본체 ────────────────────────────────────────────────────────────────
def crawl_view(vw_cd: str, limiter: RateLimiter, *, restart: bool = False) -> dict:
    """뷰 하나를 BFS로 전수 크롤한다. 중단 지점부터 재개한다(R5).

    큐 원소는 (list_id, 이름 경로, ID 경로) 3튜플이다 — 기존 크롤러는 이름만 누적하고
    LIST_ID를 버려서, 저장된 트리로는 특정 노드를 다시 찾아갈 수 없었다(R10).
    """
    paths = view_paths(vw_cd)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if restart:
        for p in paths.values():
            p.unlink(missing_ok=True)

    # 재개: 체크포인트가 있으면 큐/방문 목록을 복원한다.
    if paths["checkpoint"].exists():
        ck = json.loads(paths["checkpoint"].read_text(encoding="utf-8"))
        queue = deque(tuple(x) for x in ck["pending"])
        visited = set(ck["visited"])
        failed = [tuple(x) for x in ck["failed"]]
        calls = ck["calls"]
        started_iso = ck["started_at"]
        leaf_count = ck["leaf_count"]
        truncate_leaves_to_checkpoint(paths["leaves"], leaf_count)
        print(f"  [RESUME] {vw_cd} — 호출 {calls:,} / 리프 {leaf_count:,} / 대기 {len(queue):,}", flush=True)
    else:
        queue = deque([("", [], [])])  # 빈 parentListId = 그 뷰의 최상위 (R8)
        visited, failed, calls, leaf_count = set(), [], 0, 0
        started_iso = now_iso()
        paths["leaves"].unlink(missing_ok=True)

    vw_nm = None
    t0 = time.time()
    # 조용한 유실을 감시하는 계수기. 하나라도 0이 아니면 최종 결과에 남아
    # "완주했지만 뭔가 지나갔다"를 사람이 볼 수 있게 한다.
    audit = {
        "vw_cd_mismatch": 0,      # R12 — 응답 뷰가 요청 뷰와 다름
        "skipped_visited": 0,     # 같은 노드가 두 부모 아래 — 두 번째 경로를 버림
        "empty_nodes": 0,         # 응답이 빈 리스트 (err 30 포함)
        "unknown_rows": 0,        # TBL_ID도 LIST_ID도 없는 행
        "non_dict_rows": 0,       # dict가 아닌 행
    }
    skipped_paths: list[dict] = []   # 버린 두 번째 경로를 표본으로 남긴다
    leaves_fp = paths["leaves"].open("a", encoding="utf-8")

    def save_checkpoint(status: str) -> None:
        write_atomic(paths["checkpoint"], json.dumps({
            "vw_cd": vw_cd, "vw_nm": vw_nm, "status": status,
            "started_at": started_iso, "updated_at": now_iso(),
            "calls": calls, "leaf_count": leaf_count,
            "pending": list(queue), "visited": sorted(visited), "failed": failed,
        }, ensure_ascii=False))

    try:
        while queue:
            parent, path, path_ids = queue.popleft()
            if parent in visited:
                # 같은 노드를 두 번 크롤하지는 않는다(호출 낭비). 다만 그 노드 아래
                # 표들은 '두 번째 경로'를 갖게 되는데 그 경로가 기록되지 않는다 —
                # 표를 잃는 건 아니지만 category_paths 가 불완전해진다.
                # 실제로 몇 번 일어나는지 세고, 표본을 남겨 사후 판단할 수 있게 한다.
                audit["skipped_visited"] += 1
                if len(skipped_paths) < 200:
                    skipped_paths.append({"list_id": parent, "path": path, "path_ids": path_ids})
                continue
            visited.add(parent)

            rows, err = call_with_retry(limiter, vw_cd, parent)
            calls += 1
            if rows is None:
                failed.append((parent, path, err))
                append_failure(vw_cd, parent, path, err)
                print(f"    [FAIL] {vw_cd}/{parent or '<root>'}: {err[:100]}", flush=True)
                continue

            if not rows:
                # 자식이 없는 정상 노드이거나, 클라이언트가 err 30을 []로 흡수한 것이다.
                # 둘을 구분할 방법이 없으므로 최소한 개수는 남긴다.
                audit["empty_nodes"] += 1

            for it in rows:
                if not isinstance(it, dict):
                    audit["non_dict_rows"] += 1
                    continue
                if vw_nm is None:
                    vw_nm = it.get("VW_NM")
                # R12 — 응답이 요청한 뷰와 다르면 파라미터 오류이거나 서버 리다이렉트다.
                if it.get("VW_CD") and it["VW_CD"] != vw_cd:
                    audit["vw_cd_mismatch"] += 1

                if not it.get("TBL_ID") and not it.get("LIST_ID"):
                    # 표도 목록도 아닌 행. 무시하더라도 무엇을 무시했는지는 남긴다.
                    audit["unknown_rows"] += 1
                    if audit["unknown_rows"] <= 5:
                        print(f"    [UNKNOWN] {vw_cd}/{parent}: {it!r}", flush=True)

                if it.get("TBL_ID"):
                    leaves_fp.write(json.dumps({
                        "org_id": it.get("ORG_ID"),
                        "tbl_id": it.get("TBL_ID"),
                        "tbl_nm": it.get("TBL_NM"),
                        "stat_id": it.get("STAT_ID"),
                        "send_de": it.get("SEND_DE"),        # R9
                        "rec_tbl_se": it.get("REC_TBL_SE"),  # R9
                        "vw_cd": it.get("VW_CD"),            # R9
                        "path": path,
                        "path_ids": path_ids,                # R10
                    }, ensure_ascii=False) + "\n")
                    leaf_count += 1
                elif it.get("LIST_ID"):
                    queue.append((
                        it["LIST_ID"],
                        path + [it.get("LIST_NM", it["LIST_ID"])],
                        path_ids + [it["LIST_ID"]],
                    ))

            if calls % PROGRESS_EVERY == 0:
                done = len(visited)
                known = done + len(queue)
                print(
                    f"  [{vw_cd}] 호출 {calls:,} | 노드 {done:,}/{known:,} "
                    f"({done / known * 100:5.1f}%) | 대기 {len(queue):,} | "
                    f"리프 {leaf_count:,} | 실패 {len(failed)} | "
                    f"경과 {format_elapsed(time.time() - t0)}",
                    flush=True,
                )
            if calls % CHECKPOINT_EVERY == 0:
                leaves_fp.flush()
                save_checkpoint("running")
    except BaseException as e:
        # KeyboardInterrupt든 예상 못 한 예외든, 체크포인트를 남기지 않으면 최대
        # CHECKPOINT_EVERY(200)콜치 큐 상태가 날아간다. 그러면 재개 시 이미 수집한
        # 구간을 다시 돌아 leaves.jsonl 에 리프가 중복 적재된다(유실은 아니지만
        # leaf_count 가 부풀어 감사에서 걸린다). 그래서 어떤 예외든 저장하고 올린다.
        leaves_fp.flush()
        save_checkpoint("interrupted" if isinstance(e, KeyboardInterrupt) else "crashed")
        print(f"\n  [STOP] {vw_cd} 중단({type(e).__name__}) — 재실행하면 이어서 진행합니다.", flush=True)
        raise
    finally:
        if not leaves_fp.closed:
            leaves_fp.flush()
            leaves_fp.close()

    # R7 — 대기 0 그리고 실패 0일 때만 완주로 판정한다.
    status = "completed" if not failed else "completed_with_failures"
    save_checkpoint(status)

    leaves = [json.loads(x) for x in paths["leaves"].read_text(encoding="utf-8").splitlines() if x.strip()]
    unique = {(lf["org_id"], lf["tbl_id"]) for lf in leaves}
    result = {
        "vw_cd": vw_cd,
        "vw_nm": vw_nm,
        "crawled_at": now_iso(),
        "status": status,
        "calls": calls,
        # leaf_count(경로 중복 포함)와 unique_table_count는 다르다 — 같은 뷰 안에서도
        # 한 표가 여러 경로에 걸리기 때문이다(계획서 §4.2). 기존 트리의 설명 없던
        # 119건 차이가 이것이라, v5는 둘을 나란히 기록한다.
        "leaf_count": len(leaves),
        "unique_table_count": len(unique),
        # 조용한 유실 감시 (§크롤 감사). 전부 0이어야 "깨끗한 완주"다.
        # skipped_visited > 0 이면 표는 다 있으나 일부 표의 두 번째 경로가 누락됐다는 뜻.
        "audit": audit,
        "skipped_paths_sample": skipped_paths,
        "failed_nodes": [{"parent_id": p, "path": pa, "error": e} for p, pa, e in failed],
        "leaves": leaves,
    }
    # 저장 형식은 A안(경로를 이름·ID 둘 다 리프에 그대로 저장)으로 확정했다.
    # 실측 389 B/건 → MT_ZTITLE 265,094건이면 약 103 MB. indent를 넣으면 여기서 +10 MB에
    # 줄 수가 300만이 넘어 파싱도 느려지므로, 큰 뷰는 compact로 쓴다. 사람이 눈으로 볼
    # 파일은 manifest이지 26만 리프짜리 트리가 아니다.
    indent = 1 if len(leaves) < 5000 else None
    write_atomic(paths["final"], json.dumps(result, ensure_ascii=False, indent=indent))
    return result


def update_manifest(entry: dict) -> None:
    """뷰별 요약을 한 파일에 모은다 — 13개 파일을 열지 않고 진행 상황을 본다."""
    manifest = {}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest[entry["vw_cd"]] = {k: v for k, v in entry.items() if k != "leaves"}
    write_atomic(MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False, indent=1))


def print_status() -> None:
    """호출 없이 현재 진행 상황만 출력한다."""
    if not MANIFEST_PATH.exists():
        print(f"[STATUS] 아직 실행 이력이 없습니다 ({MANIFEST_PATH})")
        return
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def group_of(vw: str) -> str:
        """뷰가 어느 부류인지 — 공식 목록 결과와 그 밖의 것이 한 표에서 섞이면 안 된다."""
        if vw in OFFICIAL_VIEWS:
            return "official"
        if vw in WEB_ONLY_VIEWS:
            return "web_only"
        if vw in UNOFFICIAL_VIEWS:
            return "unofficial"
        return "mirror" if vw in MIRROR_VIEWS else "?"

    # 부류별로 나눠 찍고, 소계도 따로 낸다.
    LABEL = {
        "official": "공식 통계목록 (개발가이드 게재)",
        "web_only": "웹 메뉴 전용 (개발가이드 미게재)",
        "unofficial": "API 전용 (웹·문서 모두 없음)",
        "mirror": "영문 미러",
        "?": "미분류",
    }
    grand_c = grand_l = 0
    for grp in ("official", "web_only", "unofficial", "mirror", "?"):
        items = {v: e for v, e in manifest.items() if group_of(v) == grp}
        if not items:
            continue
        expected = len(PRESETS.get(grp, [])) if grp in PRESETS else len(items)
        print(f"\n■ {LABEL[grp]} — {len(items)}/{expected} 뷰 수집")
        print(f"  {'vwCd':<18} {'status':<24} {'호출':>8} {'리프':>10} {'유니크표':>10} {'실패':>5}")
        print("  " + "-" * 80)
        sc = sl = su = 0
        for vw, e in sorted(items.items()):
            sc += e.get("calls", 0); sl += e.get("leaf_count", 0); su += e.get("unique_table_count", 0)
            print(f"  {vw:<18} {e.get('status',''):<24} {e.get('calls',0):>8,} "
                  f"{e.get('leaf_count',0):>10,} {e.get('unique_table_count',0):>10,} "
                  f"{len(e.get('failed_nodes',[])):>5}")
        print("  " + "-" * 80)
        print(f"  {'소계':<18} {'':<24} {sc:>8,} {sl:>10,} {su:>10,}")
        grand_c += sc; grand_l += sl
        if grp in PRESETS:
            missing = [v for v in PRESETS[grp] if v not in manifest]
            if missing:
                print(f"  남은 뷰: {', '.join(missing)}")

    print(f"\n총 호출 {grand_c:,} / 총 리프 {grand_l:,}")
    print("※ 뷰 간 중복은 여기서 제거되지 않는다 — 유니크 표의 진짜 총계는 병합(P7) 후에 나온다.")
    print("※ KOSIS 공식 통계표 수: 289,906건 (공유서비스 첫 화면)")


def retry_failures(limiter: RateLimiter) -> None:
    """실패 노드만 다시 친다 (R4).

    성공하면 그 서브트리가 leaves.jsonl 에 이어 붙는다. 완전한 해결은 해당 뷰를
    --restart 로 다시 도는 것이지만, 실패가 몇 건뿐이면 이쪽이 훨씬 싸다.
    """
    if not FAILURES_PATH.exists():
        print("[RETRY] 실패 기록이 없습니다.")
        return
    lines = [json.loads(x) for x in FAILURES_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    pending = [r for r in lines if not r.get("resolved")]
    print(f"[RETRY] 미해결 실패 노드 {len(pending)}건")
    for rec in pending:
        rows, err = call_with_retry(limiter, rec["vw_cd"], rec["parent_id"])
        if rows is None:
            print(f"  [FAIL] {rec['vw_cd']}/{rec['parent_id']}: {err[:80]}")
            continue
        rec["resolved"] = True
        rec["resolved_at"] = now_iso()
        print(f"  [OK] {rec['vw_cd']}/{rec['parent_id']} — {len(rows)}행 "
              f"(해당 뷰를 --restart 로 다시 돌려 트리에 반영할 것)")
    write_atomic(FAILURES_PATH, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines))


def main() -> None:
    ap = argparse.ArgumentParser(description="KOSIS 서비스뷰 트리 크롤러 v5")
    ap.add_argument("--views", type=str, default=None,
                    help="vwCd 쉼표 구분 (예: MT_RTITLE,MT_BUKHAN)")
    ap.add_argument("--preset", choices=sorted(PRESETS), default=None,
                    help="official(개발가이드 게재 11개) / web_only(문서 누락 2개) / "
                         "web(13개) / unofficial(API 전용 3개) / mirror(영문)")
    ap.add_argument("--restart", action="store_true", help="이어하지 않고 처음부터 다시")
    ap.add_argument("--retry-failures", action="store_true", help="실패 노드만 재시도")
    ap.add_argument("--status", action="store_true", help="호출 없이 진행 상황만 출력")
    ap.add_argument("--max-per-min", type=int, default=MAX_CALLS_PER_MIN)
    args = ap.parse_args()

    if args.status:
        print_status()
        return

    limiter = RateLimiter(args.max_per_min)

    if args.retry_failures:
        retry_failures(limiter)
        return

    if args.views:
        views = [v.strip() for v in args.views.split(",") if v.strip()]
    elif args.preset:
        views = PRESETS[args.preset]
    else:
        ap.error("--views 또는 --preset 중 하나가 필요합니다 (--status 로 현황 확인 가능)")

    print(f"[START] 뷰 {len(views)}개: {', '.join(views)}")
    print(f"[RATE] 분당 {args.max_per_min}콜 | 출력 {OUT_DIR}")
    t0 = time.time()
    for i, vw in enumerate(views, 1):
        print(f"\n=== [{i}/{len(views)}] {vw} ===", flush=True)
        res = crawl_view(vw, limiter, restart=args.restart)
        update_manifest(res)
        print(f"  [DONE] {vw} | {res['status']} | 호출 {res['calls']:,} | "
              f"리프 {res['leaf_count']:,} | 유니크 표 {res['unique_table_count']:,} | "
              f"실패 {len(res['failed_nodes'])} | 경과 {format_elapsed(time.time() - t0)}", flush=True)

    print("\n" + "=" * 82)
    print_status()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
