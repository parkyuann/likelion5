"""뷰별 트리를 하나의 마스터 표 목록으로 병합한다 (P7).

무엇을 하나
-----------
뷰마다 따로 크롤한 트리를 읽어 `org_id:tbl_id` 기준으로 접는다.
같은 표가 여러 뷰에 등장하므로 **표 1건 = 레코드 1건**으로 만들고, 뷰마다 다른 경로만
다중으로 담는다.

왜 이렇게 접어도 되나 (계획서 §4.1 실측)
----------------------------------------
MT_TM1_TITLE 과 MT_ZTITLE(A 인구)의 교집합 51건을 필드 단위로 비교한 결과
`ORG_ID`·`TBL_ID`·`TBL_NM`·`STAT_ID`·`SEND_DE`·`REC_TBL_SE` 가 **전부 동일**했다.
뷰마다 달라지는 것은 `VW_CD`/`VW_NM` 과 경로뿐이다. 그래서 경로 외 필드는
아무 뷰 값이나 써도 된다.

다만 뷰별 크롤 시점이 다르면 `send_de` 가 갈릴 수 있다 — 최신값을 채택하되
**불일치 건수를 로그로 남긴다**(그 자체가 드리프트 신호다).

경로는 왜 리스트의 리스트인가 (계획서 §4.2 실측)
------------------------------------------------
한 뷰 안에서도 같은 표가 여러 경로에 걸린다. 실측:

    [MT_TM1_TITLE] DT_1DA7088S 행정구역(시도)/성별 실업자
      - 남성                  | 101_A11
      - 여성 > 여성의 경제활동  | A06 > A0601

그래서 `category_paths` 는 {뷰: [경로, 경로, ...]} 구조여야 한다.
기존 트리의 설명 없던 리프 265,213 vs 유니크 265,094 차이(119건)가 이 현상이다.

MT_GTITLE02 를 제외하는 이유
----------------------------
이 뷰는 **243개 지역 표가 같은 tbl_id 를 공유한다**(DT_1YL0000_1 이 종로구·중구·…).
`org_id:tbl_id` 를 유일키로 쓰는 이 파이프라인의 전제가 이 뷰에서만 깨져서, 병합하면
221개 지역 표가 2건으로 뭉개진다. 지역 구분자가 tbl_id 밖에 있어 별도 조사가 필요하다.
`--include-gtitle02` 로 강제 포함할 수 있으나 권장하지 않는다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/크롤링_v5/kosis_merge_views_v5.py
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TREE_DIR = ROOT / "data" / "크롤링_v5" / "tree_v5"
OUT_PATH = ROOT / "data" / "크롤링_v5" / "kosis_table_tree_master_v5.jsonl"
ORG_NAMES = ROOT / "data" / "크롤링_v5" / "kosis_org_names_v5.json"
V4_TREE = ROOT / "data" / "kosis_table_tree.json"

EXCLUDE_DEFAULT = {"MT_GTITLE02"}
KOSIS_OFFICIAL_TOTAL = 289906  # 공유서비스 첫 화면 게시 수치


def main() -> None:
    ap = argparse.ArgumentParser(description="뷰별 트리 → 마스터 표 목록 (v5)")
    ap.add_argument("--include-gtitle02", action="store_true",
                    help="MT_GTITLE02 도 포함 (tbl_id 가 유일키가 아니라 권장하지 않음)")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--prev-master", type=Path, default=None,
                    help="직전 세대 마스터. 이번 크롤에서 사라진 표를 status=retired 로 "
                         "이어받는다(삭제 금지, 과거 검증 이력 보존). 정기 업데이트용.")
    ap.add_argument("--indicator-tables", type=Path, default=None,
                    help="지표 경로로 회수한 뷰 밖 표(indicator_outside_tables_v5.jsonl). "
                         "매 세대 다시 병합되게 여기서 흡수. 없으면 add_indicator_tables_v5.py 별도 실행")
    args = ap.parse_args()

    exclude = set() if args.include_gtitle02 else set(EXCLUDE_DEFAULT)

    org_names = {}
    if ORG_NAMES.exists():
        org_names = json.loads(ORG_NAMES.read_text(encoding="utf-8"))
        print(f"[INFO] 기관명 {len(org_names)}개 로드")
    else:
        print(f"[WARN] {ORG_NAMES.name} 없음 — org_name 을 비운 채로 진행합니다 "
              f"(kosis_org_names_v5.py 를 먼저 실행하세요)")

    files = sorted(TREE_DIR.glob("kosis_table_tree_MT_*_v5.json"))
    if not files:
        sys.exit(f"[ERROR] 트리 파일이 없습니다: {TREE_DIR}")

    tables: dict[tuple, dict] = {}
    per_view_unique: dict[str, int] = {}
    send_de_conflicts = 0
    rec_conflicts = 0

    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        vw = d["vw_cd"]
        if vw in exclude:
            print(f"  [SKIP] {vw} — 제외 (모듈 docstring 참고)")
            continue
        if d.get("status") != "completed":
            print(f"  [WARN] {vw} status={d.get('status')} — 미완주 데이터를 병합합니다")
        seen_here = set()
        for lf in d["leaves"]:
            key = (lf["org_id"], lf["tbl_id"])
            seen_here.add(key)
            rec = tables.get(key)
            if rec is None:
                rec = tables[key] = {
                    "table_key": f"{lf['org_id']}:{lf['tbl_id']}",
                    "org_id": lf["org_id"],
                    "org_name": (org_names.get(lf["org_id"]) or {}).get("ko"),
                    "tbl_id": lf["tbl_id"],
                    "tbl_nm": lf["tbl_nm"],
                    "stat_id": lf.get("stat_id"),
                    "send_de": lf.get("send_de"),
                    "rec_tbl_se": lf.get("rec_tbl_se"),
                    "status": "active",   # 이번 크롤에서 실제로 발견됨
                    "view_codes": [],
                    "category_paths": {},
                }
            else:
                # 뷰별 크롤 시점 차이로 갈릴 수 있다 — 최신값 채택 + 불일치 계수
                a, b = rec.get("send_de"), lf.get("send_de")
                if a and b and a != b:
                    send_de_conflicts += 1
                    rec["send_de"] = max(a, b)
                elif b and not a:
                    rec["send_de"] = b
                if rec.get("rec_tbl_se") != lf.get("rec_tbl_se") and lf.get("rec_tbl_se"):
                    rec_conflicts += 1
                    if lf["rec_tbl_se"] == "Y":   # 한 뷰에서라도 추천이면 추천으로 본다
                        rec["rec_tbl_se"] = "Y"

            if vw not in rec["category_paths"]:
                rec["category_paths"][vw] = []
                rec["view_codes"].append(vw)
            entry = {"names": lf["path"], "ids": lf["path_ids"]}
            if entry not in rec["category_paths"][vw]:
                rec["category_paths"][vw].append(entry)

        per_view_unique[vw] = len(seen_here)
        print(f"  [{vw}] 유니크 표 {len(seen_here):,} | 누적 {len(tables):,}")

    # ── 지표 경로 뷰 밖 표 흡수 (매 세대 재병합되도록) ──────────────────────
    indicator_added = 0
    if args.indicator_tables and args.indicator_tables.exists():
        for line in args.indicator_tables.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            key = (r["org_id"], r["tbl_id"])
            if key in tables:
                continue
            tables[key] = {
                "table_key": f"{r['org_id']}:{r['tbl_id']}",
                "org_id": r["org_id"],
                "org_name": (org_names.get(r["org_id"]) or {}).get("ko"),
                "tbl_id": r["tbl_id"], "tbl_nm": r.get("tbl_nm"),
                "stat_id": None, "send_de": r.get("send_de"), "rec_tbl_se": None,
                "status": "active",
                "view_codes": ["INDICATOR"],
                "category_paths": {"INDICATOR": [{"names": r.get("indicators", []), "ids": []}]},
                "source_note": "KOSIS 분류 미등록, 지표 티커로 회수",
            }
            indicator_added += 1
        print(f"  [INDICATOR] 뷰 밖 표 {indicator_added}건 흡수")

    # ── 직전 세대와 대조: 사라진 표를 retired 로 이어받기 (정기 업데이트) ────
    retired = 0
    if args.prev_master and args.prev_master.exists():
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for line in args.prev_master.open(encoding="utf-8"):
            if not line.strip():
                continue
            old = json.loads(line)
            key = (old["org_id"], old["tbl_id"])
            if key in tables:
                continue   # 이번에도 존재 → active 로 이미 담김
            if old.get("status") == "retired":
                tables[key] = old   # 이미 은퇴한 표는 그대로 유지
            else:
                # 이번 크롤에서 사라짐 → 삭제하지 않고 은퇴 마킹(과거 검증 이력 보존)
                old["status"] = "retired"
                old["retired_at"] = now
                tables[key] = old
                retired += 1
        print(f"  [RETIRE] 이번에 사라진 표 {retired}건 → status=retired 로 보존")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for rec in tables.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    active_n = sum(1 for r in tables.values() if r.get("status") == "active")
    retired_n = sum(1 for r in tables.values() if r.get("status") == "retired")

    # ── 요약 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print(f"마스터 생성: {args.out}")
    print(f"  유니크 표 {len(tables):,}건 (active {active_n:,} / retired {retired_n:,})")
    print(f"  단순 합 {sum(per_view_unique.values()):,} → 뷰 간 중복 "
          f"{sum(per_view_unique.values()) - len(tables):,}건 제거")
    if send_de_conflicts:
        print(f"  ⚠ send_de 뷰 간 불일치 {send_de_conflicts:,}건 (최신값 채택) — 드리프트 신호")
    if rec_conflicts:
        print(f"  ⚠ rec_tbl_se 뷰 간 불일치 {rec_conflicts:,}건 (Y 우선)")

    multi = sum(1 for r in tables.values() if len(r["view_codes"]) > 1)
    multipath = sum(1 for r in tables.values()
                    if sum(len(v) for v in r["category_paths"].values()) > 1)
    print(f"  뷰 2개 이상에 등장: {multi:,}건 ({multi/len(tables)*100:.1f}%)")
    print(f"  경로 2개 이상:      {multipath:,}건 ({multipath/len(tables)*100:.1f}%)")

    filled = sum(1 for r in tables.values() if r["org_name"])
    print(f"  org_name 채움:      {filled:,}/{len(tables):,} ({filled/len(tables)*100:.1f}%)")

    rec_y = sum(1 for r in tables.values() if r["rec_tbl_se"] == "Y")
    print(f"  rec_tbl_se=Y:       {rec_y:,}건 ({rec_y/len(tables)*100:.2f}%)")

    print("\n■ 기존 v4 트리와 대조")
    if V4_TREE.exists():
        old = set()
        for cat in json.loads(V4_TREE.read_text(encoding="utf-8")).values():
            for lf in cat["leaves"]:
                old.add((lf["org_id"], lf["tbl_id"]))
        new = set(tables) - old
        gone = old - set(tables)
        print(f"  기존 {len(old):,} | 신규 {len(new):,} | 기존에만 있고 사라짐 {len(gone):,}")
        if gone:
            print(f"    사라진 표 예시: {sorted(gone)[:5]}")
            print("    → 삭제하지 말고 status: retired 로 마킹할 것(과거 검증 이력 보존)")

    print("\n■ KOSIS 공식 총량 대조")
    gap = KOSIS_OFFICIAL_TOTAL - len(tables)
    print(f"  공식 {KOSIS_OFFICIAL_TOTAL:,} | 수집 {len(tables):,} | 차이 {gap:,}")
    if gap > 0:
        print("  → 이 차이가 '어느 뷰에도 없는 표'의 규모다 (예: 301:DT_731Y001 주요국 통화의 대원화환율)")
    print("=" * 74)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
