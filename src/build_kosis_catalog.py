"""Normalize the crawled KOSIS tree into one JSON object per table.

The source tree may contain the same table under multiple category paths.  This
step keeps one record per ``org_id:tbl_id`` and preserves every observed path.
It does not call the KOSIS API or invent dimensions/items that are not present
in the crawled catalog.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "kosis_table_tree.json"
DEFAULT_OUTPUT = ROOT / "data" / "kosis_catalog_v1.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "kosis_catalog_v1_manifest.json"


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


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


def build_records(leaves: list[dict]) -> list[dict]:
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
                "tbl_id": tbl_id,
                "tbl_name": normalize_text(leaf.get("tbl_nm")),
                "stat_id": normalize_text(leaf.get("stat_id")) or None,
                "category_paths": [],
                "source": "data/kosis_table_tree.json",
                "catalog_version": "kosis-catalog-v1",
            },
        )
        if not record["tbl_name"]:
            record["tbl_name"] = tbl_id
        if not record["stat_id"] and normalize_text(leaf.get("stat_id")):
            record["stat_id"] = normalize_text(leaf.get("stat_id"))
        raw_path = leaf.get("path") or []
        path = tuple(normalize_text(value) for value in raw_path if normalize_text(value))
        if path:
            paths[key].add(path)

    for key, record in grouped.items():
        record["category_paths"] = [list(path) for path in sorted(paths[key])]
        path_text = " ".join(" ".join(path) for path in record["category_paths"])
        record["document_text"] = normalize_text(
            f"{record['tbl_name']} {record['org_id']} {record['stat_id'] or ''} {path_text}"
        )
    return sorted(grouped.values(), key=lambda row: row["table_key"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    tree = json.loads(args.input.read_text(encoding="utf-8"))
    leaves = flatten_leaves(tree)
    records = build_records(leaves)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "input": str(args.input.relative_to(ROOT)),
        "output": str(args.output.relative_to(ROOT)),
        "catalog_version": "kosis-catalog-v1",
        "source_leaf_rows": len(leaves),
        "unique_tables": len(records),
        "unique_orgs": len({record["org_id"] for record in records}),
        "document_text": "tbl_name + org_id + stat_id + all category paths",
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
