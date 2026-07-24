"""전체 KOSIS tree registry와 점진적 v4 metadata supplement를 provenance와 함께 병합한다."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        rows.append(value)
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_paths(*path_groups: Any) -> list[list[str]]:
    """tree와 supplement가 가진 경로를 모두 남기되 같은 경로는 한 번만 저장한다."""
    result: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for group in path_groups:
        paths = group if isinstance(group, list) else []
        if paths and all(isinstance(value, str) for value in paths):
            paths = [paths]
        for path in paths:
            if not isinstance(path, list):
                continue
            normalized = tuple(str(value) for value in path if str(value))
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(list(normalized))
    return result


def registry_only_record(leaf: dict[str, Any], category_paths: list[list[str]]) -> dict[str, Any]:
    org_id = str(leaf.get("org_id") or leaf.get("ORG_ID") or "")
    tbl_id = str(leaf.get("tbl_id") or leaf.get("TBL_ID") or "")
    table_key = f"{org_id}:{tbl_id}"
    table_name = str(leaf.get("tbl_nm") or leaf.get("tbl_name") or leaf.get("TBL_NM") or "")
    doc_terms = [table_name, *(part for path in category_paths for part in path)]
    return {
        "table_key": table_key,
        "org_id": org_id,
        "tbl_id": tbl_id,
        "tbl_name": table_name,
        "stat_id": str(leaf.get("stat_id") or leaf.get("STAT_ID") or ""),
        "category_paths": category_paths,
        "category_path_status": "tree_registry" if category_paths else "tree_path_missing",
        "tree_presence": True,
        "profile_present": False,
        "metadata_status": "registry_only",
        "items": [],
        "dimensions": [],
        "units": [],
        "periods": [],
        "period_types": [],
        "source_metadata": [],
        "api_status": {},
        "doc_meta_text": " | ".join(term for term in doc_terms if term),
        "doc_item_index": "",
        "catalog_version": "kosis-v4-registry-supplement",
        "value_parse_status": "metadata_not_collected",
    }


def supplement_record(profile: dict[str, Any], base: dict[str, Any] | None, category_paths: list[list[str]]) -> dict[str, Any]:
    """API profile을 우선 보존하고 tree 식별자·경로 provenance를 덧붙인다."""
    record = dict(profile)
    table_key = str(profile.get("table_key") or (base or {}).get("table_key") or "")
    record["table_key"] = table_key
    record["org_id"] = str(profile.get("org_id") or (base or {}).get("org_id") or "")
    record["tbl_id"] = str(profile.get("tbl_id") or (base or {}).get("tbl_id") or "")
    record["tbl_name"] = str(profile.get("tbl_name") or (base or {}).get("tbl_name") or "")
    record["stat_id"] = str(profile.get("stat_id") or (base or {}).get("stat_id") or "")
    record["category_paths"] = category_paths
    record["profile_category_path_status"] = str(profile.get("category_path_status") or "") or None
    record["category_path_status"] = (
        "tree_and_profile" if base and category_paths else
        "tree_registry" if base else
        str(profile.get("category_path_status") or "supplement_only")
    )
    record["tree_presence"] = base is not None
    record["profile_present"] = True
    record["metadata_status"] = str(profile.get("meta_status") or "partial")
    record["tree_tbl_name"] = str((base or {}).get("tbl_name") or "") or None
    record["tree_stat_id"] = str((base or {}).get("stat_id") or "") or None
    record["catalog_version"] = "kosis-v4-registry-supplement"
    return record


def build_registry(
    tree: dict[str, Any], profiles: list[dict[str, Any]], value_index_rows: list[dict[str, Any]], output: Path,
) -> dict[str, Any]:
    """대용량 tree를 스트리밍해 registry를 만들고 supplement 누락 여부를 manifest로 남긴다."""
    profiles_by_key = {str(row.get("table_key") or ""): row for row in profiles if row.get("table_key")}
    used_profiles: set[str] = set()
    seen_keys: set[str] = set()
    duplicate_tree_rows = 0
    tree_leaf_rows = 0
    output_rows = 0
    metadata_status_counts: Counter[str] = Counter()
    category_path_status_counts: Counter[str] = Counter()
    output_hash = hashlib.sha256()
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8") as handle:
        for category in tree.values():
            if not isinstance(category, dict):
                continue
            for leaf in category.get("leaves", []):
                if not isinstance(leaf, dict):
                    continue
                tree_leaf_rows += 1
                base = registry_only_record(leaf, unique_paths(leaf.get("path")))
                table_key = base["table_key"]
                if not all((base["org_id"], base["tbl_id"])):
                    continue
                if table_key in seen_keys:
                    duplicate_tree_rows += 1
                    continue
                seen_keys.add(table_key)
                profile = profiles_by_key.get(table_key)
                record = supplement_record(profile, base, unique_paths(base["category_paths"], profile.get("category_paths"))) if profile else base
                if profile:
                    used_profiles.add(table_key)
                encoded = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                handle.write(encoded)
                output_hash.update(encoded.encode("utf-8"))
                output_rows += 1
                metadata_status_counts[str(record["metadata_status"])] += 1
                category_path_status_counts[str(record["category_path_status"])] += 1

        for table_key, profile in profiles_by_key.items():
            if table_key in used_profiles:
                continue
            record = supplement_record(profile, None, unique_paths(profile.get("category_paths")))
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            handle.write(encoded)
            output_hash.update(encoded.encode("utf-8"))
            output_rows += 1
            metadata_status_counts[str(record["metadata_status"])] += 1
            category_path_status_counts[str(record["category_path_status"])] += 1

    indexed_table_keys = {str(row.get("table_key") or "") for row in value_index_rows if row.get("table_key")}
    profile_only = sorted(set(profiles_by_key) - used_profiles)
    return {
        "tree_categories": len(tree),
        "tree_leaf_rows": tree_leaf_rows,
        "tree_duplicate_rows_skipped": duplicate_tree_rows,
        "registry_tables": output_rows,
        "registry_unique_table_keys": output_rows,
        "profile_input_tables": len(profiles_by_key),
        "profiles_merged_with_tree": len(used_profiles),
        "profile_only_tables": profile_only,
        "metadata_status_counts": dict(sorted(metadata_status_counts.items())),
        "category_path_status_counts": dict(sorted(category_path_status_counts.items())),
        "value_index_rows": len(value_index_rows),
        "value_index_table_keys": len(indexed_table_keys),
        "value_index_keys_without_profile": sorted(indexed_table_keys - set(profiles_by_key)),
        "registry_sha256": output_hash.hexdigest(),
        "registry_path": str(output),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build full KOSIS v4 registry with metadata supplement provenance")
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--value-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    tree = json.loads(args.tree.read_text(encoding="utf-8"))
    if not isinstance(tree, dict):
        raise ValueError("tree must be a JSON object")
    manifest = build_registry(tree, read_jsonl(args.profiles), read_jsonl(args.value_index), args.output)
    manifest["tree_sha256"] = sha256_file(args.tree)
    manifest["profiles_sha256"] = sha256_file(args.profiles)
    manifest["value_index_sha256"] = sha256_file(args.value_index)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
