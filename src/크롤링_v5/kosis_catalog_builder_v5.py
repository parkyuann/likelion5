"""마스터(표 목록) + 메타(차원·항목)를 합쳐 검색용 카탈로그 v5를 만든다.

v2/v4 카탈로그(kosis_catalog_builder.py)의 3층 구조를 계승한다:
  · doc_meta_text  — 임베딩 전용. 짧게. 표명 + 대표경로 + 차원'명'.
  · doc_item_index — BM25 전용. 길게. 항목명 + 차원'값 이름' 전부.
  · payload        — 필터·표시·리랭킹용. 텍스트 아닌 것.

v4에서 바뀐 점
--------------
1. **입력이 마스터/메타 v5**. 트리(평면)가 아니라 병합 마스터(뷰별 category_paths dict).
2. **대표 경로(primary_path)** — 겹치는 표는 뷰마다 경로 맥락이 다르다(실측 100%).
   MT_ZTITLE(주제별=KOSIS 기준분류) 우선으로 하나만 임베딩에 넣는다. 다중뷰 표
   266,426건 중 266,399건(99.99%)이 ZTITLE 보유라 자동 확정. 대표 아닌 나머지 경로는
   doc_item_index/doc_meta_text 어느 검색 텍스트에도 넣지 않고 category_paths(payload)에만
   원본 보존한다(v4 동작으로 복귀, 2026-08-12 결정). doc_item_index는 v4와 동일하게
   항목명 + 차원값 이름만 담는다.
3. **latest_period 재계산** — v4는 max(문자열)이라 "2026.06"(월) vs "2025"(연) 혼재 시
   부정확했다. periods 원본에서 연도 우선으로 다시 계산한다.
4. **신규 payload**: send_de · rec_tbl_se · view_codes · status.
5. **retired 표 제외** — 이번 판 크롤에서 사라진 표는 색인하지 않는다.

차원값 '코드'(id·up_id)를 payload 에 담는다 (A방식 번복, 2026-08-14)
------------------------------------------------------------------
당초(2026-08-12)엔 검증(get_data)만 코드로 하면 되니 메타 참조로 충분하다 보고
카탈로그에서 코드를 뺐다(A방식). 그러나 Qdrant structured retrieval(유안)에서
item·dimension·period·unit 기반 shortlist 를 만들려면 차원값 코드가 payload 에
있어야 한다 — 그래야 표를 특정하기 전에 구조적 필터로 후보를 좁힌다. 그래서 이번 판부터
dimensions[].values[]{id,nm,up_id} 를 payload 에 포함한다.
★ 검색 텍스트(doc_meta_text/doc_item_index)에는 코드를 넣지 않는다 — 이름만 유지.
  임베딩 입력이 불변이라 기존 임베딩 캐시(text-hash)가 그대로 재사용된다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/크롤링_v5/kosis_catalog_builder_v5.py
    venv/Scripts/python.exe src/크롤링_v5/kosis_catalog_builder_v5.py --only-enriched
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
V5 = ROOT / "data" / "크롤링_v5"
DEFAULT_MASTER = V5 / "kosis_table_tree_master_v5.jsonl"
DEFAULT_OUTPUT = V5 / f"kosis_catalog_v5_{datetime.now():%y%m%d}.jsonl"
DEFAULT_MANIFEST = V5 / f"kosis_catalog_v5_{datetime.now():%y%m%d}_manifest.json"

CATALOG_VERSION = "kosis-catalog-v5"
EMBEDDING_TOKEN_LIMIT = 8192
CHARS_PER_TOKEN = 1.41

# 대표 경로 우선순위. KOSIS 기준분류(ZTITLE)를 최우선, 큐레이션/재배열 뷰는 뒤로.
# OTITLE(기관축)·TM1/TM2(대상·이슈)는 원본 주제 맥락이 아니라 대표에서 뺀다.
PRIMARY_VIEW_PRIORITY = [
    "MT_ZTITLE", "MT_STOP_TITLE", "MT_RTITLE", "MT_RTITLE01", "MT_BUKHAN",
    "MT_CHOSUN_TITLE", "MT_HANKUK_TITLE", "MT_GTITLE01", "MT_GTITLE03",
    "MT_GTITLE02", "MT_TM1_TITLE", "MT_TM2_TITLE", "MT_OTITLE", "INDICATOR",
]


# KOSIS 항목·차원명에 HTML 태그(<br>, <sub>, </a> 등)가 섞여 온다 — 검색 토큰을 오염시켜 제거.
# 단 "신장 < 표준편차", "< 5세" 같은 부등호는 실제 항목명이므로 지우면 안 된다.
# 그래서 '< 또는 </ 뒤에 곧바로 영문자'로 시작하는 진짜 태그만 매칭한다.
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def norm(v: object) -> str:
    text = _HTML_TAG.sub(" ", str(v or ""))
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def est_tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def find_latest_period(periods: list[dict]) -> str | None:
    """periods 원본에서 최신 시점을 제대로 고른다 (v4 max(문자열) 버그 수정).

    "2026.06"(월)과 "2025"(연)가 섞이면 문자열 max는 "2026.06"을 고르지만, 형식이
    더 복잡한 경우(반기 "2026H1" 등) 어긋난다. 여기선 end 문자열의 앞 4자리(연도)를
    정수로 뽑아 연도 최대를 먼저 고르고, 같은 연도면 원래 문자열 max로 세부 비교한다.
    """
    ends = [norm(p.get("end")) for p in periods if norm(p.get("end"))]
    if not ends:
        return None

    def year_of(s: str) -> int:
        m = re.match(r"(\d{4})", s)
        return int(m.group(1)) if m else 0

    max_year = max(year_of(e) for e in ends)
    same_year = [e for e in ends if year_of(e) == max_year]
    return max(same_year)


def load_meta(path: Path) -> dict[str, dict]:
    """메타를 table_key → 레코드로 인덱싱. status=ok 인 마지막 레코드만."""
    meta: dict[str, dict] = {}
    if not path.exists():
        return meta
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("status") == "ok" and r.get("table_key"):
                meta[r["table_key"]] = r
    return meta


def pick_primary_path(category_paths: dict) -> tuple[str | None, list[str]]:
    """우선순위에서 가장 높은 뷰의 첫 경로를 대표로. (뷰코드, 이름경로) 반환."""
    for vw in PRIMARY_VIEW_PRIORITY:
        if vw in category_paths and category_paths[vw]:
            names = category_paths[vw][0].get("names", [])
            return vw, [norm(x) for x in names if norm(x)]
    # 우선순위 밖 뷰만 있는 경우: 아무거나 첫 번째
    for vw, paths in category_paths.items():
        if paths:
            return vw, [norm(x) for x in paths[0].get("names", []) if norm(x)]
    return None, []


def build_doc_meta_text(tbl_name: str, primary_path: list[str], dim_names: list[str]) -> str:
    """임베딩 대상. 표명 + 대표경로 하나 + 차원명. 짧게 유지."""
    parts = [tbl_name]
    if primary_path:
        parts.append(" > ".join(primary_path))
    if dim_names:
        parts.append(", ".join(dim_names))
    return norm(" | ".join(p for p in parts if p))


def build_doc_item_index(item_names: list[str], dim_values: list[str]) -> str:
    """BM25 대상. 항목명 + 차원값 이름. 길이 제한 없음. (v4와 동일 구성)"""
    seen: set[str] = set()
    out: list[str] = []
    for v in item_names + dim_values:
        v = norm(v)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return " ".join(out)


def build_record(row: dict, meta: dict | None) -> dict:
    cp = row.get("category_paths", {})
    primary_view, primary_path = pick_primary_path(cp)

    dims = (meta or {}).get("dimensions", [])
    items = (meta or {}).get("items", [])
    periods = (meta or {}).get("periods", [])

    dim_names = [norm(d.get("obj_nm")) for d in dims if norm(d.get("obj_nm"))]
    dim_values = [
        norm(v.get("nm") if isinstance(v, dict) else v)
        for d in dims for v in d.get("values", [])
    ]
    item_names = [norm(i.get("itm_nm")) for i in items if norm(i.get("itm_nm"))]

    # 대표 아닌 나머지 경로는 검색 텍스트에 넣지 않고 category_paths(payload)에만 보존한다
    # (v4 동작으로 복귀, 2026-08-12 결정).

    rec = {
        "table_key": row["table_key"],
        "org_id": row["org_id"],
        "org_name": row.get("org_name"),
        "tbl_id": row["tbl_id"],
        "tbl_name": norm(row.get("tbl_nm")) or row["tbl_id"],
        "stat_id": norm(row.get("stat_id")) or None,
        "catalog_version": CATALOG_VERSION,

        # 검색 3층
        "doc_meta_text": build_doc_meta_text(norm(row.get("tbl_nm")) or row["tbl_id"],
                                             primary_path, dim_names),
        "doc_item_index": build_doc_item_index(item_names, dim_values),

        # 경로 (전체 보존 + 대표 표시)
        "category_paths": cp,
        "primary_view": primary_view,
        "primary_path": primary_path,

        # payload — 차원 축(id·명칭·개수)에 더해 차원'값' 코드(id·명칭·상위id)까지 담는다.
        # 유안 structured retrieval: item·dimension·period·unit 기반 shortlist 를 만들려면
        # 차원값 코드가 payload 에 있어야 한다(2026-08-14, A방식 번복). 검색 텍스트는 불변 —
        # doc_meta_text/doc_item_index 는 그대로이므로 임베딩 캐시(text-hash)가 유지된다.
        "dimensions": [
            {"obj_id": d.get("obj_id"), "obj_nm": norm(d.get("obj_nm")) or None,
             "value_count": len(d.get("values", [])),
             "values": [
                 {"id": v.get("id"), "nm": norm(v.get("nm")) or None, "up_id": v.get("up_id")}
                 for v in d.get("values", []) if isinstance(v, dict)
             ]}
            for d in dims
        ],
        "items": [
            {"itm_id": i.get("itm_id"), "itm_nm": norm(i.get("itm_nm")) or None,
             "unit_nm": norm(i.get("unit_nm")) or None}
            for i in items
        ],
        "units": (meta or {}).get("units", []),
        "period_types": (meta or {}).get("period_types", []),
        "latest_period": find_latest_period(periods),

        # v5 신규 payload
        "send_de": row.get("send_de"),
        "rec_tbl_se": row.get("rec_tbl_se"),
        "view_codes": row.get("view_codes", []),
        "status": row.get("status", "active"),

        "meta_status": "enriched" if meta else "missing",
        "doc_meta_text_tokens_est": est_tokens(
            build_doc_meta_text(norm(row.get("tbl_nm")) or row["tbl_id"], primary_path, dim_names)
        ),
    }
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description="KOSIS 카탈로그 v5 빌더")
    ap.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    ap.add_argument("--meta", type=Path, default=None,
                    help="메타 파일. 미지정 시 data/크롤링_v5/kosis_table_meta_v5_*.jsonl 최신본")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--only-enriched", action="store_true",
                    help="메타 있는 표만 출력")
    ap.add_argument("--include-retired", action="store_true",
                    help="retired 표도 포함 (기본은 제외)")
    args = ap.parse_args()

    meta_path = args.meta
    if meta_path is None:
        cands = sorted(V5.glob("kosis_table_meta_v5_*.jsonl"))
        cands = [c for c in cands if ".checkpoint" not in c.name]
        if not cands:
            raise SystemExit("[ERROR] 메타 파일을 찾을 수 없습니다 (--meta 로 지정)")
        meta_path = cands[-1]
    print(f"[INFO] 마스터: {args.master.name}")
    print(f"[INFO] 메타:   {meta_path.name}")

    meta = load_meta(meta_path)
    print(f"[INFO] 메타 로드 {len(meta):,}건 (status=ok)")

    n_total = n_retired = n_missing = 0
    over_limit = 0
    tokens_sum = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.master.open(encoding="utf-8") as fin, args.output.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") == "retired" and not args.include_retired:
                n_retired += 1
                continue
            rec = build_record(row, meta.get(row["table_key"]))
            if args.only_enriched and rec["meta_status"] != "enriched":
                continue
            if rec["meta_status"] == "missing":
                n_missing += 1
            tok = rec["doc_meta_text_tokens_est"]
            tokens_sum += tok
            if tok > EMBEDDING_TOKEN_LIMIT:
                over_limit += 1
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_total += 1

    manifest = {
        "catalog_version": CATALOG_VERSION,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "master": args.master.name,
        "meta": meta_path.name,
        "output": args.output.name,
        "written_rows": n_total,
        "meta_missing_rows": n_missing,
        "retired_excluded": n_retired if not args.include_retired else 0,
        "doc_meta_text_tokens_mean": round(tokens_sum / n_total, 1) if n_total else 0,
        "doc_meta_text_over_limit": over_limit,
        "primary_view_priority": PRIMARY_VIEW_PRIORITY,
        "notes": {
            "dim_codes": "차원값 코드(id·nm·up_id)를 payload dimensions[].values[]에 포함 (2026-08-14, A방식 번복 — structured retrieval용). 검색 텍스트에는 미포함.",
            "latest_period": "periods 원본에서 연도우선 재계산 (v4 max(문자열) 버그 수정)",
        },
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
