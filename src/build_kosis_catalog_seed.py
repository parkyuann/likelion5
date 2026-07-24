"""v4 catalog API EDA·크롤링을 위한 우선순위/층화 seed를 만든다.

입력 tree와 priority JSONL만 있으면 동작하므로 노트북 산출물에 의존하지 않는다.
priority 표는 coverage 복구를 위해 항상 포함하고, 나머지는 KOSIS 대분류마다
결정론적으로 동일한 수를 선택한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rank_key(row: dict[str, Any]) -> str:
    return hashlib.sha256(f"{row.get('org_id', '')}:{row.get('tbl_id', '')}".encode("utf-8")).hexdigest()


def normalize(row: dict[str, Any], source: str) -> dict[str, Any]:
    org_id = str(row.get("org_id") or row.get("ORG_ID") or "")
    tbl_id = str(row.get("tbl_id") or row.get("TBL_ID") or "")
    if not org_id or not tbl_id:
        raise ValueError("seed row requires org_id and tbl_id")
    path = row.get("category_path") or row.get("path") or []
    normalized_path = [str(value) for value in path] if isinstance(path, list) else []
    return {
        "table_key": f"{org_id}:{tbl_id}", "org_id": org_id, "tbl_id": tbl_id,
        "tbl_name": str(row.get("tbl_name") or row.get("TBL_NM") or ""),
        "stat_id": str(row.get("stat_id") or row.get("STAT_ID") or ""),
        "category_path": normalized_path,
        "category_path_status": str(row.get("category_path_status") or ("present" if normalized_path else "unresolved")),
        "sample_source": source,
    }


def table_lookup(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index discovery leaves so coverage-priority rows keep their KOSIS path."""
    lookup: dict[str, dict[str, Any]] = {}
    for category in tree.values():
        if not isinstance(category, dict):
            continue
        for leaf in category.get("leaves", []):
            if not isinstance(leaf, dict):
                continue
            org_id = str(leaf.get("org_id") or leaf.get("ORG_ID") or "")
            tbl_id = str(leaf.get("tbl_id") or leaf.get("TBL_ID") or "")
            if org_id and tbl_id:
                lookup.setdefault(f"{org_id}:{tbl_id}", leaf)
    return lookup


def build_seed(tree: dict[str, Any], priority_rows: list[dict[str, Any]], per_category: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """coverage priority를 먼저 넣고, 대분류별 대표 표를 중복 없이 보강한다."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    discovery_lookup = table_lookup(tree)
    priority_paths_hydrated = 0
    priority_path_unresolved_table_keys: list[str] = []
    for raw in priority_rows:
        candidate = dict(raw)
        key = f"{candidate.get('org_id') or candidate.get('ORG_ID') or ''}:{candidate.get('tbl_id') or candidate.get('TBL_ID') or ''}"
        discovered = discovery_lookup.get(key)
        if discovered and not (candidate.get("category_path") or candidate.get("path")):
            candidate["category_path"] = discovered.get("path") or []
            candidate.setdefault("stat_id", discovered.get("stat_id") or "")
            candidate["category_path_status"] = "discovery_tree"
            priority_paths_hydrated += 1
        elif not (candidate.get("category_path") or candidate.get("path")):
            candidate["category_path_status"] = "not_found_in_discovery_tree"
            priority_path_unresolved_table_keys.append(key)
        row = normalize(candidate, str(raw.get("sample_source") or "coverage_priority"))
        if row["table_key"] not in seen:
            rows.append(row)
            seen.add(row["table_key"])
    category_counts: dict[str, int] = {}
    for category_id, category in tree.items():
        leaves = category.get("leaves", []) if isinstance(category, dict) else []
        chosen = 0
        for leaf in sorted((leaf for leaf in leaves if isinstance(leaf, dict)), key=rank_key):
            try:
                row = normalize(leaf, f"tree_stratified:{category_id}:{category.get('top_nm', '')}")
            except ValueError:
                continue
            if row["table_key"] in seen:
                continue
            rows.append(row)
            seen.add(row["table_key"])
            chosen += 1
            if chosen >= per_category:
                break
        category_counts[category_id] = chosen
    return rows, {
        "priority_rows": len(priority_rows), "per_category_requested": per_category,
        "selected_rows": len(rows), "category_selected_counts": category_counts,
        "priority_paths_hydrated": priority_paths_hydrated,
        "priority_path_unresolved_table_keys": priority_path_unresolved_table_keys,
        "selection_method": "priority first; sha256(org_id:tbl_id) per top category",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build standalone KOSIS v4 crawl seeds")
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--priority", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--per-category", type=int, default=3)
    args = parser.parse_args()
    tree = json.loads(args.tree.read_text(encoding="utf-8"))
    rows, manifest = build_seed(tree, read_jsonl(args.priority), args.per_category)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
