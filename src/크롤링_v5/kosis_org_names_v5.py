"""수집한 트리에 등장하는 모든 기관코드의 이름을 받아 저장한다.

무엇을 하나
-----------
  1) 크롤한 뷰별 트리에서 org_id 를 전부 모은다 (파일 읽기, API 호출 0)
  2) 기관코드마다 getMeta type=ORG 로 이름을 묻는다 (기관당 1콜)
  3) {코드: {ko, en}} 로 저장한다

왜 이렇게 하나
--------------
`getMeta type=ORG` 는 통계표를 보지 않는다. 기관 코드 대장만 본다 — 실측으로 확인했다:

    orgId=301 + tblId=NO_SUCH_TABLE_XYZ  →  "한국은행"   (없는 표를 넣어도 정상 응답)
    orgId=101 + tblId=DT_103Y002         →  "국가데이터처" (남의 표를 넣어도 orgId 기준)

그래서 표 수(29만)와 무관하게 **기관 수(389)만큼만** 부르면 된다. 2분이면 끝난다.

기존 `data/kosis_org_names.json` 은 MT_OTITLE 최상위 노드 182개를 받아 적은 것이라
181개만 갖고 있고, 실제 등장 기관 389개 중 **214개의 이름이 비어 있었다**.
`KOSIS_org_name_보강_계획서.md` 가 P5(MT_OTITLE 전수 ~20,000콜)로 풀려던 문제이며,
같은 문서 §5의 "의사 기관코드 999/999S 상수 매핑"도 불필요하다 — 실측 결과
999 → 조선총독부통계연보, 999S → 대한민국통계연감 으로 정상 응답한다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/크롤링_v5/kosis_org_names_v5.py
    venv/Scripts/python.exe src/크롤링_v5/kosis_org_names_v5.py --include-v4-tree   # 기존 트리 기관까지
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kosis_client_v5 import API_KEY, META_URL, _loads_lenient  # noqa: E402

import requests  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
TREE_DIR = ROOT / "data" / "크롤링_v5" / "tree_v5"
OUT_PATH = ROOT / "data" / "크롤링_v5" / "kosis_org_names_v5.json"
V4_TREE = ROOT / "data" / "kosis_table_tree.json"

MAX_PER_MIN = 170
MAX_RETRY = 4


def collect_org_ids(include_v4: bool) -> list[str]:
    """트리 파일들에서 org_id를 전부 모은다 (API 호출 없음)."""
    orgs: set[str] = set()
    for p in sorted(TREE_DIR.glob("*.leaves.jsonl")):
        with p.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    o = json.loads(line).get("org_id")
                    if o:
                        orgs.add(str(o))
    if include_v4 and V4_TREE.exists():
        tree = json.loads(V4_TREE.read_text(encoding="utf-8"))
        for cat in tree.values():
            for lf in cat.get("leaves", []):
                if lf.get("org_id"):
                    orgs.add(str(lf["org_id"]))
    return sorted(orgs)


def fetch_org_name(org_id: str) -> tuple[str | None, str | None, str]:
    """기관코드 하나의 이름을 받는다. (국문, 영문, 오류) 반환.

    tblId 는 보내지 않는다 — type=ORG 는 읽지 않으며, 개발가이드 요청변수에도 없다.
    """
    params = {
        "method": "getMeta", "apiKey": API_KEY,
        "orgId": org_id, "type": "ORG",
        "format": "json", "jsonVD": "Y",
    }
    last = ""
    for attempt in range(1, MAX_RETRY + 1):
        try:
            res = requests.get(META_URL, params=params, timeout=15)
            res.raise_for_status()
            data = _loads_lenient(res.text)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(2.0 * attempt)
            continue
        if isinstance(data, dict) and "err" in data:
            if data["err"] == "40":          # 분당 한도 — 다음 분까지 넘긴다
                print("    [RATE] 분당 한도 — 65초 대기", flush=True)
                time.sleep(65)
                continue
            return None, None, f"err {data['err']}: {data.get('errMsg')}"
        if isinstance(data, list) and data:
            return data[0].get("ORG_NM"), data[0].get("ORG_NM_ENG"), ""
        return None, None, f"예상 밖 응답: {data!r}"
    return None, None, last


def main() -> None:
    ap = argparse.ArgumentParser(description="KOSIS 기관코드 → 기관명 수집 (v5)")
    ap.add_argument("--include-v4-tree", action="store_true",
                    help="기존 data/kosis_table_tree.json 의 기관까지 포함")
    ap.add_argument("--max-per-min", type=int, default=MAX_PER_MIN)
    args = ap.parse_args()

    orgs = collect_org_ids(args.include_v4_tree)
    print(f"[STATUS] 대상 기관 {len(orgs)}개 — 예상 {len(orgs)/args.max_per_min:.1f}분", flush=True)

    # 이어하기: 이미 받은 기관은 건너뛴다.
    result: dict[str, dict] = {}
    if OUT_PATH.exists():
        result = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        print(f"[RESUME] 기존 {len(result)}개 유지, {len(orgs)-len(result)}개 남음", flush=True)

    interval = 60.0 / args.max_per_min
    t0 = time.time()
    ok = err = 0
    for i, org in enumerate(orgs, 1):
        if org in result and not result[org].get("error"):
            continue
        t = time.time()
        ko, en, e = fetch_org_name(org)
        result[org] = {"ko": ko, "en": en} if not e else {"ko": None, "en": None, "error": e}
        if e:
            err += 1
            print(f"  [FAIL] {org}: {e}", flush=True)
        else:
            ok += 1
        if i % 50 == 0:
            print(f"  {i}/{len(orgs)} | 성공 {ok} 실패 {err} | {time.time()-t0:.0f}초", flush=True)
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        d = interval - (time.time() - t)
        if d > 0:
            time.sleep(d)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    filled = sum(1 for v in result.values() if v.get("ko"))
    print(f"\n=== 완료: {filled}/{len(orgs)} 기관명 확보 (실패 {err}) | "
          f"{time.time()-t0:.0f}초 | {OUT_PATH} ===")
    if err:
        print("  실패분은 다시 실행하면 그것만 재시도합니다.")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
