"""전체 v4 registry와 보강 profile·exact-value side index의 연결을 검증한다."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_registry(
    registry_rows: list[dict[str, Any]], profiles: list[dict[str, Any]], value_index_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """후보 공간·metadata 보강·값 index가 서로 다른 table key를 가리키지 않는지 확인한다."""
    registry_by_key = {str(row.get("table_key") or ""): row for row in registry_rows if row.get("table_key")}
    profile_keys = {str(row.get("table_key") or "") for row in profiles if row.get("table_key")}
    index_keys = {str(row.get("table_key") or "") for row in value_index_rows if row.get("table_key")}
    duplicate_keys = len(registry_rows) - len(registry_by_key)
    table_key_mismatches = sorted(
        str(row.get("table_key") or "") for row in registry_rows
        if str(row.get("table_key") or "") != f"{row.get('org_id')}:{row.get('tbl_id')}"
    )
    profile_not_preserved = sorted(
        key for key in profile_keys
        if key not in registry_by_key or not registry_by_key[key].get("profile_present")
    )
    index_without_profile = sorted(
        key for key in index_keys
        if key not in registry_by_key or not registry_by_key[key].get("profile_present")
    )
    status_counts = Counter(str(row.get("metadata_status") or "MISSING") for row in registry_rows)
    path_status_counts = Counter(str(row.get("category_path_status") or "MISSING") for row in registry_rows)
    blocking_failure_count = sum((duplicate_keys, len(table_key_mismatches), len(profile_not_preserved), len(index_without_profile)))
    return {
        "registry_rows": len(registry_rows),
        "registry_unique_table_keys": len(registry_by_key),
        "metadata_status_counts": dict(sorted(status_counts.items())),
        "category_path_status_counts": dict(sorted(path_status_counts.items())),
        "profile_input_tables": len(profile_keys),
        "profile_tables_preserved": len(profile_keys) - len(profile_not_preserved),
        "value_index_table_keys": len(index_keys),
        "value_index_table_keys_with_profile": len(index_keys) - len(index_without_profile),
        "exceptions": {
            "duplicate_registry_key_count": duplicate_keys,
            "table_key_mismatches": table_key_mismatches,
            "profile_not_preserved": profile_not_preserved,
            "value_index_without_profile": index_without_profile,
        },
        "blocking_failure_count": blocking_failure_count,
        "quality_gate": "PASS" if not blocking_failure_count else "FAIL",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate KOSIS v4 full registry and supplement linkage")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--value-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate_registry(read_jsonl(args.registry), read_jsonl(args.profiles), read_jsonl(args.value_index))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
