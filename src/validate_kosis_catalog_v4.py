"""Validate consistency between a KOSIS v4 seed, catalog, raw checkpoint, and value index."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


META_ENDPOINTS = ("TBL", "ITM", "PRD", "SOURCE", "NCD")


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


def value_pairs(catalog_rows: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    """Return every nonblank structured dimension value that must have a side-index row."""
    pairs: set[tuple[str, str, str]] = set()
    for record in catalog_rows:
        table_key = str(record.get("table_key") or "")
        for dimension in record.get("dimensions", []):
            if not isinstance(dimension, dict):
                continue
            obj_id = str(dimension.get("obj_id") or "")
            for value in dimension.get("values", []):
                if not isinstance(value, dict):
                    continue
                value_id = str(value.get("value_id") or "")
                value_name = str(value.get("value_name") or "")
                if table_key and obj_id and value_id and value_name:
                    pairs.add((table_key, obj_id, value_id))
    return pairs


def validate_v4(
    seeds: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    value_index_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    seed_keys = [str(row.get("table_key") or "") for row in seeds]
    seed_key_set = {key for key in seed_keys if key}
    catalog_keys = [str(row.get("table_key") or "") for row in catalog_rows]
    catalog_key_set = {key for key in catalog_keys if key}
    catalog_by_key = {str(row.get("table_key") or ""): row for row in catalog_rows if row.get("table_key")}

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw_rows:
        key = (str(row.get("table_key") or ""), str(row.get("endpoint") or ""))
        if all(key):
            latest[key] = row
    endpoint_status_counts: dict[str, dict[str, int]] = {}
    missing_checkpoint_records: list[str] = []
    non_source_errors: list[str] = []
    source_errors: list[str] = []
    for endpoint in META_ENDPOINTS:
        counts: Counter[str] = Counter()
        for table_key in sorted(seed_key_set):
            record = latest.get((table_key, endpoint))
            status = str(record.get("status") if record else "NOT_STARTED")
            counts[status] += 1
            if status == "NOT_STARTED":
                missing_checkpoint_records.append(f"{table_key}:{endpoint}")
            elif status != "OK":
                target = source_errors if endpoint == "SOURCE" else non_source_errors
                target.append(f"{table_key}:{endpoint}:{status}")
        endpoint_status_counts[endpoint] = dict(sorted(counts.items()))

    expected_values = value_pairs(catalog_rows)
    indexed_values = {
        (str(row.get("table_key") or ""), str(row.get("obj_id") or ""), str(row.get("value_id") or ""))
        for row in value_index_rows
        if row.get("table_key") and row.get("obj_id") and row.get("value_id")
    }
    index_row_keys = [
        (str(row.get("table_key") or ""), str(row.get("obj_id") or ""), str(row.get("value_id") or ""))
        for row in value_index_rows
    ]
    duplicate_index_rows = sum(count - 1 for count in Counter(index_row_keys).values() if count > 1)

    priority_keys = {
        str(row.get("table_key") or "") for row in seeds
        if str(row.get("sample_source") or "").startswith("provisional_gold_coverage_gap")
    }
    priority_not_enriched = sorted(
        key for key in priority_keys if catalog_by_key.get(key, {}).get("meta_status") != "enriched"
    )
    duplicate_catalog_keys = sorted(key for key, count in Counter(catalog_keys).items() if key and count > 1)
    missing_catalog_keys = sorted(seed_key_set - catalog_key_set)
    unexpected_catalog_keys = sorted(catalog_key_set - seed_key_set)
    missing_index_values = sorted(expected_values - indexed_values)
    orphan_index_values = sorted(indexed_values - expected_values)
    metadata_not_ready = sorted(
        key for key, record in catalog_by_key.items()
        if record.get("meta_status") == "enriched" and (
            not record.get("tbl_name") or not record.get("items") or not record.get("dimensions") or
            not record.get("periods") or any(not dimension.get("values") for dimension in record.get("dimensions", []) if isinstance(dimension, dict))
        )
    )

    blocking_failure_count = sum((
        len(missing_catalog_keys), len(unexpected_catalog_keys), len(duplicate_catalog_keys),
        len(missing_checkpoint_records), len(non_source_errors), len(priority_not_enriched),
        len(missing_index_values), len(orphan_index_values), duplicate_index_rows,
        len(metadata_not_ready),
    ))
    return {
        "seed_tables": len(seed_keys),
        "catalog_tables": len(catalog_rows),
        "unique_seed_table_keys": len(seed_key_set),
        "unique_catalog_table_keys": len(catalog_key_set),
        "catalog_meta_status_counts": dict(sorted(Counter(str(row.get("meta_status") or "MISSING") for row in catalog_rows).items())),
        "category_path_status_counts": dict(sorted(Counter(str(row.get("category_path_status") or "MISSING") for row in catalog_rows).items())),
        "empty_category_paths": sum(not row.get("category_paths") for row in catalog_rows),
        "latest_endpoint_status_counts": endpoint_status_counts,
        "side_index": {
            "rows": len(value_index_rows),
            "expected_structured_values": len(expected_values),
            "unique_indexed_values": len(indexed_values),
            "duplicate_rows": duplicate_index_rows,
            "missing_values": len(missing_index_values),
            "orphan_values": len(orphan_index_values),
        },
        "priority_coverage": {
            "seed_tables": len(priority_keys),
            "enriched_tables": len(priority_keys) - len(priority_not_enriched),
            "not_enriched_table_keys": priority_not_enriched,
        },
        "exceptions": {
            "source_endpoint_errors": source_errors,
            "non_source_endpoint_errors": non_source_errors,
            "missing_checkpoint_records": missing_checkpoint_records,
            "missing_catalog_table_keys": missing_catalog_keys,
            "unexpected_catalog_table_keys": unexpected_catalog_keys,
            "duplicate_catalog_table_keys": duplicate_catalog_keys,
            "missing_side_index_values": ["|".join(value) for value in missing_index_values],
            "orphan_side_index_values": ["|".join(value) for value in orphan_index_values],
            "enriched_but_not_cell_query_ready": metadata_not_ready,
        },
        "blocking_failure_count": blocking_failure_count,
        "quality_gate": "PASS" if not blocking_failure_count else "FAIL",
        "known_optional_source_gap_count": len(source_errors),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate KOSIS v4 catalog cross-artifact consistency")
    parser.add_argument("--seeds", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--value-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate_v4(read_jsonl(args.seeds), read_jsonl(args.catalog), read_jsonl(args.raw), read_jsonl(args.value_index))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
