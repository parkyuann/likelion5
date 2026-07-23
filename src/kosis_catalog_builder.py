"""크롤링한 트리와 표 메타를 합쳐 검색 색인용 카탈로그 v2를 만든다.

v1(build_kosis_catalog.py)은 표 하나를 ``document_text`` 한 필드로 표현했다:

    "누적 혼인 비율 101 2025010 인구 인구동태패널통계 혼인·출산 비율"

이 필드는 dense 임베딩과 sparse(BM25) 양쪽이 함께 쓰는데, 둘의 요구가 반대다 —
임베딩은 짧고 의미가 밀집해야 하고(길면 벡터가 흐려진다), BM25는 매칭될 토큰이
없으면 아예 못 찾으므로 길고 망라적이어야 한다. 게다가 v1에는 org_id/stat_id가
숫자 그대로 섞여 있어(101, 2025010) 임베딩에 아무 의미도 기여하지 못하면서
벡터만 끌어당겼다.

v2는 이를 세 갈래로 나눈다.

* ``doc_meta_text``  — 임베딩 전용. 표명 + 카테고리 경로 + 차원'명'.
* ``doc_item_index`` — BM25 전용. 항목명 + 차원'값' 전부.
* payload            — 필터 전용. org_id, units, dimensions 등 텍스트가 아닌 것.

차원명(연령별)은 doc_meta_text, 차원값(0세, 1세, …)은 doc_item_index로 가른다.
차원값은 표당 수백~수만 개라 임베딩에 넣으면 표의 정체가 목록에 파묻히고, HCX
임베딩 v2 한도(8,192토큰)도 넘긴다 — 실측(인구 카테고리 2,279건, 1.41자/토큰)에서
전부 합치면 1.7%가 한도를 초과했고, 분리하면 0%였다. 반대로 BM25에는 "종로구"라는
토큰이 인덱스에 있어야 "종로구 인구" 주장을 잡을 수 있으므로 반드시 필요하다.

사용 예 (레포 루트에서):
    # 메타가 확보된 표만 (권장 — 부분 크롤링 상태에서 확인용)
    venv/Scripts/python.exe src/kosis_catalog_builder.py --only-enriched

    # 전체 표 (메타 없는 표는 doc_item_index가 빈 값)
    venv/Scripts/python.exe src/kosis_catalog_builder.py
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TREE = ROOT / "data" / "kosis_table_tree.json"
DEFAULT_META = ROOT / "data" / "kosis_table_meta_enriched.jsonl"
DEFAULT_ORG_NAMES = ROOT / "data" / "kosis_org_names.json"
DEFAULT_OUTPUT = ROOT / "data" / "kosis_catalog_enriched.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "kosis_catalog_enriched_manifest.json"

CATALOG_VERSION = "kosis-catalog-enriched"

# HCX 임베딩 v2 사양(공식 문서) + 실측 토큰 비율.
# 비율은 임베딩 API가 돌려주는 result.inputTokens로 측정했다(1,000자 -> 709토큰).
# 어디까지나 추정용이며, 정확한 값은 T2-2 색인 때 inputTokens로 실측하면 된다.
EMBEDDING_TOKEN_LIMIT = 8192
CHARS_PER_TOKEN = 1.41


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def estimate_tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def flatten_leaves(tree: dict) -> list[dict]:
    leaves: list[dict] = []
    for category_id, category in tree.items():
        if not isinstance(category, dict):
            continue
        for leaf in category.get("leaves", []):
            if not isinstance(leaf, dict):
                continue
            item = dict(leaf)
            item.setdefault("top_category_id", category_id)
            leaves.append(item)
    return leaves


def load_meta(path: Path) -> dict[str, dict]:
    """kosis_meta_enricher.py가 만든 표별 메타를 table_key로 인덱싱한다.

    같은 표가 여러 번 기록돼 있으면(재실행 중 중복 append) 마지막 성공 레코드를
    쓴다. status가 error인 레코드는 차원·항목이 없으므로 무시한다.
    """
    if not path.exists():
        return {}
    meta: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") != "ok":
                continue
            key = record.get("table_key")
            if key:
                meta[key] = record
    return meta


def build_doc_meta_text(tbl_name: str, category_paths: list[list[str]], dimension_names: list[str]) -> str:
    """임베딩 대상 텍스트. 짧게 유지하는 것이 목적이므로 차원'명'까지만 넣는다."""
    parts = [tbl_name]
    if category_paths:
        parts.append(" ; ".join(" > ".join(path) for path in category_paths))
    if dimension_names:
        parts.append(", ".join(dimension_names))
    return normalize_text(" | ".join(part for part in parts if part))


def build_doc_item_index(item_names: list[str], dimension_values: list[str]) -> str:
    """BM25 대상 텍스트. 길이 제한이 없으므로 항목명과 차원값을 모두 넣는다."""
    seen: set[str] = set()
    tokens: list[str] = []
    for value in item_names + dimension_values:
        value = normalize_text(value)
        if value and value not in seen:
            seen.add(value)
            tokens.append(value)
    return " ".join(tokens)


def build_records(leaves: list[dict], meta: dict[str, dict], org_names: dict[str, str]) -> list[dict]:
    grouped: dict[str, dict] = {}
    paths: defaultdict[str, set[tuple[str, ...]]] = defaultdict(set)

    for leaf in leaves:
        org_id = normalize_text(leaf.get("org_id"))
        tbl_id = normalize_text(leaf.get("tbl_id"))
        if not org_id or not tbl_id:
            continue
        key = f"{org_id}:{tbl_id}"
        record = grouped.setdefault(
            key,
            {
                "table_key": key,
                "org_id": org_id,
                "org_name": org_names.get(org_id),
                "tbl_id": tbl_id,
                "tbl_name": normalize_text(leaf.get("tbl_nm")) or tbl_id,
                "stat_id": normalize_text(leaf.get("stat_id")) or None,
                "category_paths": [],
                "source": "data/kosis_table_tree.json + data/kosis_table_meta_enriched.jsonl",
                "catalog_version": CATALOG_VERSION,
            },
        )
        if not record["stat_id"] and normalize_text(leaf.get("stat_id")):
            record["stat_id"] = normalize_text(leaf.get("stat_id"))
        raw_path = leaf.get("path") or []
        path = tuple(normalize_text(value) for value in raw_path if normalize_text(value))
        if path:
            paths[key].add(path)

    for key, record in grouped.items():
        record["category_paths"] = [list(path) for path in sorted(paths[key])]

        table_meta = meta.get(key)
        dimensions = (table_meta or {}).get("dimensions", [])
        items = (table_meta or {}).get("items", [])

        dimension_names = [normalize_text(d.get("obj_nm")) for d in dimensions if normalize_text(d.get("obj_nm"))]
        # 차원값은 enricher가 {id,nm,up_id,sn} dict로 준다(구버전은 문자열이라 둘 다 처리).
        dimension_values = [
            (value.get("nm") if isinstance(value, dict) else value)
            for d in dimensions for value in d.get("values", [])
        ]
        item_names = [normalize_text(i.get("itm_nm")) for i in items if normalize_text(i.get("itm_nm"))]

        record["doc_meta_text"] = build_doc_meta_text(
            record["tbl_name"], record["category_paths"], dimension_names
        )
        record["doc_item_index"] = build_doc_item_index(item_names, dimension_values)

        # payload — 텍스트 검색이 아니라 메타 프리필터에 쓰는 값들.
        # 차원'값'은 doc_item_index에 이미 있으므로 여기엔 이름과 개수만 남긴다
        # (전부 중복 저장하면 파일이 두 배가 된다).
        record["dimensions"] = [
            {
                "obj_id": d.get("obj_id"),
                "obj_nm": normalize_text(d.get("obj_nm")) or None,
                "value_count": len(d.get("values", [])),
            }
            for d in dimensions
        ]
        record["items"] = [
            {"itm_id": i.get("itm_id"), "itm_nm": normalize_text(i.get("itm_nm")) or None}
            for i in items
        ]
        record["units"] = (table_meta or {}).get("units", [])
        # 시점(PRD): T2-2 payload 프리필터(period_types/latest_period)로 그대로 넘긴다.
        record["period_types"] = (table_meta or {}).get("period_types", [])
        record["latest_period"] = (table_meta or {}).get("latest_period")
        record["meta_status"] = "enriched" if table_meta else "missing"
        record["doc_meta_text_tokens_est"] = estimate_tokens(record["doc_meta_text"])

    return sorted(grouped.values(), key=lambda row: row["table_key"])


def summarize(records: list[dict]) -> dict:
    enriched = [r for r in records if r["meta_status"] == "enriched"]
    meta_tokens = sorted(r["doc_meta_text_tokens_est"] for r in records)
    item_chars = sorted(len(r["doc_item_index"]) for r in enriched)

    def percentile(values: list[int], q: float) -> int:
        if not values:
            return 0
        return values[min(len(values) - 1, int(len(values) * q))]

    over_limit = sum(1 for t in meta_tokens if t > EMBEDDING_TOKEN_LIMIT)
    return {
        "unique_tables": len(records),
        "enriched_tables": len(enriched),
        "missing_meta_tables": len(records) - len(enriched),
        "doc_meta_text_tokens_mean": round(sum(meta_tokens) / len(meta_tokens), 1) if meta_tokens else 0,
        "doc_meta_text_tokens_p99": percentile(meta_tokens, 0.99),
        "doc_meta_text_tokens_max": meta_tokens[-1] if meta_tokens else 0,
        "doc_meta_text_over_limit": over_limit,
        "doc_meta_text_over_limit_ratio": round(over_limit / len(records), 6) if records else 0,
        "doc_item_index_chars_mean": round(sum(item_chars) / len(item_chars), 1) if item_chars else 0,
        "doc_item_index_chars_p99": percentile(item_chars, 0.99),
        "doc_item_index_chars_max": item_chars[-1] if item_chars else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, default=DEFAULT_TREE)
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument("--org-names", type=Path, default=DEFAULT_ORG_NAMES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--only-enriched", action="store_true",
                        help="차원·항목 메타가 있는 표만 출력 (부분 크롤링 상태에서 확인용)")
    args = parser.parse_args()

    tree = json.loads(args.tree.read_text(encoding="utf-8"))
    meta = load_meta(args.meta)
    org_names = json.loads(args.org_names.read_text(encoding="utf-8")) if args.org_names.exists() else {}

    leaves = flatten_leaves(tree)
    records = build_records(leaves, meta, org_names)
    summary_all = summarize(records)

    if args.only_enriched:
        records = [r for r in records if r["meta_status"] == "enriched"]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def rel(p: Path) -> str:
        # 상대경로로 넘어온 인자도 ROOT 밖으로 벗어나지 않게 안전 처리.
        p = p.resolve()
        return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)

    manifest = {
        "catalog_version": CATALOG_VERSION,
        "tree": rel(args.tree),
        "meta": rel(args.meta),
        "output": rel(args.output),
        "only_enriched": args.only_enriched,
        "written_rows": len(records),
        "source_leaf_rows": len(leaves),
        "embedding_token_limit": EMBEDDING_TOKEN_LIMIT,
        "chars_per_token_measured": CHARS_PER_TOKEN,
        "doc_meta_text": "tbl_name | category_paths | dimension names (임베딩 대상)",
        "doc_item_index": "item names + dimension values (BM25 대상)",
        **summary_all,
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
