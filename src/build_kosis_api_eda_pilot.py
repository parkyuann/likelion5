"""KOSIS 분류 트리에서 API EDA용 층화 표본을 결정론적으로 만든다."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def stable_rank(row: dict[str, Any]) -> str:
    """실행 환경과 입력 순서가 달라도 같은 표본을 얻기 위한 SHA-256 순위다."""
    key = f"{row.get('org_id', '')}:{row.get('tbl_id', '')}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()


def build_pilot(tree: dict[str, Any], per_category: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """각 대분류에서 서로 다른 표를 같은 수만큼 뽑아 도메인 편향을 낮춘다."""
    rows: list[dict[str, str]] = []
    selected_keys: set[str] = set()
    category_counts: dict[str, int] = {}
    for category_id, category in tree.items():
        leaves = category.get("leaves", []) if isinstance(category, dict) else []
        ranked = sorted(
            (leaf for leaf in leaves if isinstance(leaf, dict) and leaf.get("org_id") and leaf.get("tbl_id")),
            key=stable_rank,
        )
        count = 0
        for leaf in ranked:
            table_key = f"{leaf['org_id']}:{leaf['tbl_id']}"
            if table_key in selected_keys:
                continue
            rows.append({
                "table_key": table_key,
                "org_id": str(leaf["org_id"]),
                "tbl_id": str(leaf["tbl_id"]),
                "tbl_name": str(leaf.get("tbl_nm") or ""),
                "sample_source": f"tree_stratified:{category_id}:{category.get('top_nm', '')}",
            })
            selected_keys.add(table_key)
            count += 1
            if count >= per_category:
                break
        category_counts[category_id] = count
    manifest = {
        "categories": len(tree),
        "per_category_requested": per_category,
        "selected_tables": len(rows),
        "category_selected_counts": category_counts,
        "selection_method": "sha256(table_key) ascending, unique tables per top category",
    }
    return rows, manifest


def write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic stratified KOSIS API EDA pilot seeds")
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--per-category", type=int, default=3)
    args = parser.parse_args()
    tree = json.loads(args.tree.read_text(encoding="utf-8"))
    rows, manifest = build_pilot(tree, args.per_category)
    write_jsonl(args.output, rows)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
