"""Adapt KOSIS catalog v3 JSONL records to canonical KOSISTable objects."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

try:  # Supports both `python src/...py` and `from src...` test imports.
    from .retrieval_schema import KOSISDimension, KOSISPeriod, KOSISTable, validate_table
except ImportError:
    from retrieval_schema import KOSISDimension, KOSISPeriod, KOSISTable, validate_table


def as_string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def source_name_from_record(record: dict[str, Any]) -> str | None:
    """v4 SOURCE 응답의 조사명을 canonical source_name으로 보존한다."""
    direct = str(record.get("source") or "").strip()
    if direct:
        return direct
    for source in record.get("source_metadata", []):
        if isinstance(source, dict) and str(source.get("JOSA_NM") or "").strip():
            return str(source["JOSA_NM"]).strip()
    return None


def api_status_from_record(record: dict[str, Any]) -> dict[str, str]:
    raw_status = record.get("api_status")
    return {str(endpoint): str(status) for endpoint, status in raw_status.items()} if isinstance(raw_status, dict) else {}


def adapt_record(record: dict[str, Any]) -> KOSISTable:
    category_paths = record.get("category_paths")
    category_path = category_paths[0] if isinstance(category_paths, list) and category_paths and isinstance(category_paths[0], list) else []
    dimensions = []
    for dimension in record.get("dimensions", []) if isinstance(record.get("dimensions"), list) else []:
        if not isinstance(dimension, dict):
            continue
        values = dimension.get("values") if isinstance(dimension.get("values"), list) else []
        dimensions.append(KOSISDimension(
            dimension_id=str(dimension.get("obj_id") or ""),
            dimension_name=str(dimension.get("obj_nm") or ""),
            values=values,
        ))
    periods = [KOSISPeriod(period_type=period) for period in as_string_list(record.get("period_types"))]
    has_structured_values = any(dimension.values for dimension in dimensions)
    return KOSISTable(
        table_key=str(record.get("table_key") or ""),
        org_id=str(record.get("org_id") or ""),
        org_name=str(record.get("org_name") or ""),
        tbl_id=str(record.get("tbl_id") or ""),
        tbl_name=str(record.get("tbl_name") or ""),
        stat_id=str(record.get("stat_id") or "") or None,
        category_path=[str(item) for item in category_path],
        periods=periods,
        dimensions=dimensions,
        items=record.get("items") if isinstance(record.get("items"), list) else [],
        units=as_string_list(record.get("units")),
        source_name=source_name_from_record(record),
        version_status=str(record.get("meta_status") or "") or None,
        document_text=str(record.get("doc_meta_text") or "") or None,
        doc_meta_text=str(record.get("doc_meta_text") or "") or None,
        doc_item_index=str(record.get("doc_item_index") or "") or None,
        catalog_version=str(record.get("catalog_version") or "") or None,
        value_parse_status="structured" if has_structured_values else "metadata_only",
        category_path_status=str(record.get("category_path_status") or ("present" if category_path else "unresolved")),
        api_status=api_status_from_record(record),
    )


def load_catalog(path: Path) -> Iterator[KOSISTable]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield adapt_record(json.loads(line))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tables = list(load_catalog(args.input))
    errors = [{"table_key": table.table_key, "errors": validate_table(table)} for table in tables if validate_table(table)]
    payload = {
        "tables": len(tables),
        "validation_errors": errors,
        "catalog_versions": sorted({table.catalog_version for table in tables}),
        "value_parse_status": {
            "structured": sum(table.value_parse_status == "structured" for table in tables),
            "metadata_only": sum(table.value_parse_status == "metadata_only" for table in tables),
        },
        "version_status_counts": dict(sorted(Counter(str(table.version_status or "MISSING") for table in tables).items())),
        "category_path_status_counts": dict(sorted(Counter(str(table.category_path_status or "MISSING") for table in tables).items())),
        "source_name_available": sum(bool(table.source_name) for table in tables),
        "api_status_counts": dict(sorted(Counter(
            f"{endpoint}:{status}" for table in tables for endpoint, status in table.api_status.items()
        ).items())),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
