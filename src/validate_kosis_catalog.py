"""Validate a KOSIS catalog JSONL and write a reproducible quality manifest."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "table_key", "org_id", "tbl_id", "tbl_name", "catalog_version",
    "doc_meta_text", "doc_item_index", "items", "dimensions", "period_types",
)


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, failures = [], []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            failures.append({"line_number": line_number, "reason": "invalid_json", "detail": str(error)})
            continue
        if not isinstance(value, dict):
            failures.append({"line_number": line_number, "reason": "not_object"})
            continue
        rows.append(value)
    return rows, failures


def missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def validate(rows: list[dict[str, Any]], parse_failures: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    failures = list(parse_failures)
    field_missing = Counter()
    status_counts = Counter()
    version_counts = Counter()
    source_counts = Counter()
    keys: Counter[str] = Counter()
    dimensions_total = dimensions_with_values = 0
    item_total = 0

    for index, row in enumerate(rows, start=1):
        table_key = str(row.get("table_key") or "")
        keys[table_key] += 1
        status_counts[str(row.get("meta_status") or "MISSING")] += 1
        version_counts[str(row.get("catalog_version") or "MISSING")] += 1
        source_counts[str(row.get("source") or "MISSING")] += 1
        for field in REQUIRED_FIELDS:
            if missing(row.get(field)):
                field_missing[field] += 1
        expected_key = f"{row.get('org_id')}:{row.get('tbl_id')}"
        if not table_key or table_key != expected_key:
            failures.append({"row_number": index, "table_key": table_key, "reason": "table_key_mismatch", "expected": expected_key})
        if not isinstance(row.get("items"), list):
            failures.append({"row_number": index, "table_key": table_key, "reason": "items_not_list"})
        else:
            item_total += len(row["items"])
        if not isinstance(row.get("dimensions"), list):
            failures.append({"row_number": index, "table_key": table_key, "reason": "dimensions_not_list"})
        else:
            dimensions_total += len(row["dimensions"])
            dimensions_with_values += sum(isinstance(dimension, dict) and isinstance(dimension.get("values"), list)
                                          for dimension in row["dimensions"])

    duplicate_keys = {key: count for key, count in keys.items() if key and count > 1}
    for key, count in duplicate_keys.items():
        failures.append({"table_key": key, "reason": "duplicate_table_key", "count": count})
    total = len(rows)
    manifest = {
        "records": total,
        "coverage_scope": "unknown_requires_source_confirmation",
        "catalog_versions": dict(version_counts),
        "meta_status_counts": dict(status_counts),
        "source_counts": dict(source_counts),
        "unique_table_keys": len([key for key in keys if key]),
        "duplicate_table_keys": len(duplicate_keys),
        "field_missing_counts": {field: field_missing[field] for field in REQUIRED_FIELDS},
        "field_missing_rates": {field: round(field_missing[field] / total, 6) if total else 0.0 for field in REQUIRED_FIELDS},
        "items_total": item_total,
        "dimensions_total": dimensions_total,
        "dimensions_with_structured_values": dimensions_with_values,
        "dimension_value_structure_rate": round(dimensions_with_values / dimensions_total, 6) if dimensions_total else 0.0,
        "failure_count": len(failures),
    }
    return manifest, failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--failures", type=Path, required=True)
    args = parser.parse_args()
    rows, parse_failures = read_jsonl(args.input)
    manifest, failures = validate(rows, parse_failures)
    manifest.update({"input": str(args.input), "failures_output": str(args.failures)})
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    args.failures.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in failures) + ("\n" if failures else ""), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
