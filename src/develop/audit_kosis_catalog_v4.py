"""KOSIS v4 catalog/meta JSONL의 품질을 감사하고 재현 가능한 산출물을 만든다.

입력을 읽기만 하며 수정하지 않는다. adapter 착수 판단에 필요한 사실(키 무결성, 필드
결측률, 차원값 분포, 기간 형식, 수집 실패)만 측정한다.

``--schema catalog``는 검색용 축약본(`kosis_catalog_v4.jsonl`)을, ``--schema meta``는
수집 원본(`kosis_table_meta_v4.jsonl`)을 대상으로 한다. 두 파일은 같은 표를 담지만
차원값 표현이 달라(개수만 / value_id 포함) 지탱하는 경로가 다르다.
``--compare-value-counts``로 catalog가 meta의 충실한 파생물인지도 확인할 수 있다.

기존 ``src/validate_kosis_catalog_v4.py``는 seed/catalog/raw/value_index 네 산출물의
교차 정합성을 본다. 이 감사는 파일 자체의 내용적 충분성을 본다. 목적이 다르므로
별도 파일로 둔다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# 감사 대상은 두 파일이며 스키마가 다르다.
#   catalog: 검색용 축약본. dimensions에 value_count만 있고 values는 없다.
#   meta:    수집 원본. dimensions에 values(id/nm/up_id/sn)까지 있어 셀 조회가 가능하다.
# catalog는 meta에서 values를 떼어내 만든 파생물이므로, 두 파일을 같은 잣대로 재고
# 각각이 어느 경로(검색 / 셀 정렬)를 지탱하는지 분리해서 판정한다.
SCHEMA_PROFILES = {
    "catalog": {
        "table_name_field": "tbl_name",
        "required": (
            "table_key", "org_id", "tbl_id", "tbl_name", "catalog_version",
            "doc_meta_text", "doc_item_index", "items", "dimensions", "period_types",
        ),
        "observed": ("units", "latest_period", "category_paths", "source", "stat_id", "org_name"),
    },
    "meta": {
        "table_name_field": "tbl_nm",
        "required": ("table_key", "org_id", "tbl_id", "tbl_nm", "items", "dimensions", "period_types"),
        "observed": ("units", "unit_source", "latest_period", "periods"),
    },
}

# 실제 catalog에서 관측된 KOSIS 기간 표기 네 형태.
PERIOD_PATTERNS = {
    "YYYY": re.compile(r"^\d{4}$"),
    "YYYY.MM": re.compile(r"^\d{4}\.\d{2}$"),
    "YYYY N/D": re.compile(r"^\d{4} \d/\d$"),
    "YYYYMMDD": re.compile(r"^\d{8}$"),
}
ALL_SHAPES = set(PERIOD_PATTERNS)
# period_types 라벨이 latest_period의 어떤 형태와 양립하는지의 대응표.
# '부정기'는 갱신 주기가 정해지지 않은 통계라 어떤 표기와도 양립한다.
PERIOD_TYPE_SHAPES = {
    "월": {"YYYY.MM"},
    "분기": {"YYYY N/D"},
    "반기": {"YYYY N/D"},
    "년": {"YYYY"},
    "일": {"YYYYMMDD"},
    "부정기": ALL_SHAPES,
}
ANNUAL_MULTIPLE = re.compile(r"^\d+년$")
MIN_PLAUSIBLE_YEAR = 1900
# 장래인구추계는 2072년까지 발표되므로 단순히 '미래'라는 이유로 결함 처리하면 안 된다.
# 이 폭을 넘는 값만 파싱 오류 후보로 본다.
DEFAULT_HORIZON_YEARS = 50


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_missing(value: Any) -> bool:
    """None, 빈 문자열, 빈 리스트, 빈 dict를 모두 결측으로 본다.

    ``items: []``는 '항목이 없는 표'가 아니라 '항목을 못 가져온 표'이므로 결측이다.
    """
    return value is None or value == "" or value == [] or value == {}


def iter_records(path: Path, limit: int | None = None) -> Iterator[tuple[int, dict[str, Any] | None, str]]:
    """(행 번호, 레코드, 오류사유)를 흘려보낸다. 파싱 실패도 버리지 않고 보고한다."""
    with path.open(encoding="utf-8") as handle:
        emitted = 0
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            if limit is not None and emitted >= limit:
                return
            emitted += 1
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                yield number, None, f"invalid_json: {str(error)[:200]}"
                continue
            if not isinstance(value, dict):
                yield number, None, "not_object"
                continue
            yield number, value, ""


def classify_period(latest_period: str, period_types: list[str], current_year: int,
                    horizon_years: int = DEFAULT_HORIZON_YEARS) -> list[str]:
    """latest_period와 period_types의 이상 유형을 모두 모아 반환한다."""
    problems: list[str] = []
    if not latest_period:
        problems.append("latest_period_missing")
    else:
        shape = next((name for name, pattern in PERIOD_PATTERNS.items() if pattern.match(latest_period)), "")
        if not shape:
            problems.append("latest_period_unknown_format")
        else:
            year = int(latest_period[:4])
            if year < MIN_PLAUSIBLE_YEAR:
                problems.append("latest_period_year_too_old")
            if year > current_year + horizon_years:
                problems.append("latest_period_year_beyond_horizon")
            if shape == "YYYYMMDD":
                month, day = int(latest_period[4:6]), int(latest_period[6:8])
                if not (1 <= month <= 12 and 1 <= day <= 31):
                    problems.append("latest_period_date_out_of_range")
            if shape == "YYYY.MM" and not 1 <= int(latest_period[5:7]) <= 12:
                problems.append("latest_period_month_out_of_range")
            if shape == "YYYY N/D":
                numerator, denominator = int(latest_period[5]), int(latest_period[7])
                if denominator not in (2, 4) or not 1 <= numerator <= denominator:
                    problems.append("latest_period_fraction_invalid")
            allowed: set[str] = set()
            for label in period_types:
                if label in PERIOD_TYPE_SHAPES:
                    allowed |= PERIOD_TYPE_SHAPES[label]
                elif ANNUAL_MULTIPLE.match(label):
                    allowed |= {"YYYY"}
                else:
                    problems.append("period_type_unknown_label")
            if allowed and shape not in allowed:
                problems.append("latest_period_shape_conflicts_period_types")
    if not period_types:
        problems.append("period_types_missing")
    return sorted(set(problems))


def dimension_value_count(dimension: dict[str, Any]) -> tuple[int, bool]:
    """차원의 값 개수와 '구조화된 values 배열을 갖고 있는가'를 함께 반환한다.

    v2/v4 세대 catalog는 ``{"obj_id","obj_nm","value_count"}`` 형태로 **개수만** 보존하고
    value_id/value_name 쌍은 저장하지 않는다. 개수만으로는 기사의 '서울'을 어느
    value_id로 조회할지 결정할 수 없으므로, 두 경우를 반드시 구분해서 센다.
    """
    values = dimension.get("values")
    if isinstance(values, list):
        return len(values), True
    declared = dimension.get("value_count")
    return (declared if isinstance(declared, int) and declared >= 0 else 0), False


def summarize_distribution(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)

    def at(fraction: float) -> int:
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]

    return {
        "count": len(ordered), "min": ordered[0], "p25": at(0.25), "median": at(0.5),
        "p75": at(0.75), "p90": at(0.90), "p99": at(0.99), "max": ordered[-1],
        "zero_count": sum(1 for value in ordered if value == 0),
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def load_collection_failures(path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """수집 원본(kosis_table_meta_v4.jsonl)에서 실패·부분성공 기록을 뽑는다.

    파일이 없으면 '실패가 없었다'가 아니라 '실패 여부를 모른다'로 보고한다.
    """
    if path is None or not path.exists():
        return [], {"available": False, "reason": "raw meta file not provided or missing"}
    failures: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    fetched: list[str] = []
    for _, record, error in iter_records(path):
        if record is None:
            status_counts["PARSE_FAILURE"] += 1
            continue
        status = str(record.get("status") or "MISSING")
        status_counts[status] += 1
        stamp = str(record.get("fetched_at") or "")
        if stamp:
            fetched.append(stamp)
        if status != "ok":
            failures.append({
                "table_key": str(record.get("table_key") or ""),
                "org_id": str(record.get("org_id") or ""),
                "tbl_id": str(record.get("tbl_id") or ""),
                "status": status,
                "fetched_at": stamp,
                "error": str(record.get("error") or record.get("error_message") or "")[:500],
            })
    return failures, {
        "available": True, "path": str(path), "rows": sum(status_counts.values()),
        "status_counts": dict(sorted(status_counts.items())),
        "fetched_at_min": min(fetched, default=None), "fetched_at_max": max(fetched, default=None),
    }


def audit(catalog_path: Path, raw_meta_path: Path | None, current_year: int, limit: int | None = None,
          horizon_years: int = DEFAULT_HORIZON_YEARS, schema: str = "catalog") -> dict[str, Any]:
    profile = SCHEMA_PROFILES[schema]
    required_fields: tuple[str, ...] = profile["required"]
    observed_fields: tuple[str, ...] = profile["observed"]
    table_name_field: str = profile["table_name_field"]
    total_rows = 0
    parse_failures: list[dict[str, Any]] = []
    key_counts: Counter[str] = Counter()
    key_lines: dict[str, list[int]] = {}
    invalid_keys: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    field_missing: Counter[str] = Counter()
    meta_status_counts: Counter[str] = Counter()
    catalog_version_counts: Counter[str] = Counter()
    period_problem_counts: Counter[str] = Counter()
    period_problem_rows: list[dict[str, Any]] = []
    value_counts: list[int] = []
    dimension_counts: list[int] = []
    item_counts: list[int] = []
    zero_value_dimension_tables = 0
    cell_query_ready = 0
    dimensions_with_values_array = 0
    dimensions_value_count_only = 0
    tables_with_structured_values = 0

    for line_number, record, error in iter_records(catalog_path, limit=limit):
        if record is None:
            parse_failures.append({"line_number": line_number, "reason": error})
            continue
        total_rows += 1

        table_key = str(record.get("table_key") or "")
        key_counts[table_key] += 1
        key_lines.setdefault(table_key, []).append(line_number)

        org_id, tbl_id = str(record.get("org_id") or ""), str(record.get("tbl_id") or "")
        expected_key = f"{org_id}:{tbl_id}"
        if not org_id or not tbl_id or table_key != expected_key:
            invalid_keys.append({
                "line_number": line_number, "table_key": table_key, "org_id": org_id,
                "tbl_id": tbl_id, "expected_table_key": expected_key,
                "reason": "empty_org_or_tbl_id" if not org_id or not tbl_id else "table_key_mismatch",
            })

        missing_here = [field for field in required_fields if is_missing(record.get(field))]
        for field in missing_here:
            field_missing[field] += 1
        for field in observed_fields:
            if is_missing(record.get(field)):
                field_missing[field] += 1
        if missing_here:
            missing_rows.append({"line_number": line_number, "table_key": table_key, "missing_fields": missing_here})

        meta_status_counts[str(record.get("meta_status") or "MISSING")] += 1
        catalog_version_counts[str(record.get("catalog_version") or "MISSING")] += 1

        dimensions = record.get("dimensions") if isinstance(record.get("dimensions"), list) else []
        dimension_counts.append(len(dimensions))
        has_zero_value_dimension = False
        all_structured = bool(dimensions)
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                continue
            count, structured = dimension_value_count(dimension)
            value_counts.append(count)
            if structured:
                dimensions_with_values_array += 1
            else:
                dimensions_value_count_only += 1
                all_structured = False
            if count == 0:
                has_zero_value_dimension = True
        if has_zero_value_dimension:
            zero_value_dimension_tables += 1
        if all_structured:
            tables_with_structured_values += 1

        items = record.get("items") if isinstance(record.get("items"), list) else []
        item_counts.append(len(items))

        period_types = [str(value) for value in (record.get("period_types") or []) if isinstance(record.get("period_types"), list)]
        problems = classify_period(str(record.get("latest_period") or ""), period_types, current_year, horizon_years)
        for problem in problems:
            period_problem_counts[problem] += 1
        if problems:
            period_problem_rows.append({
                "line_number": line_number, "table_key": table_key,
                "latest_period": record.get("latest_period"), "period_types": period_types,
                "problems": problems,
            })

        # 셀 조회에는 value_id가 필요하므로 개수만 있는 차원은 ready로 세지 않는다.
        if (record.get(table_name_field) and items and dimensions and all_structured
                and not has_zero_value_dimension and period_types):
            cell_query_ready += 1

    duplicates = [
        {"table_key": key, "occurrences": count, "line_numbers": key_lines[key][:20]}
        for key, count in key_counts.items() if count > 1
    ]
    collection_failures, collection_summary = load_collection_failures(raw_meta_path)

    def rate(count: int) -> float:
        return round(count / total_rows, 6) if total_rows else 0.0

    return {
        "record_totals": {
            "total_records": total_rows,
            "unique_table_keys": len(key_counts),
            "duplicate_table_keys": len(duplicates),
            "duplicate_extra_rows": sum(item["occurrences"] - 1 for item in duplicates),
            "parse_failures": len(parse_failures),
            "invalid_table_keys": len(invalid_keys),
        },
        "field_completeness": {
            "total_records": total_rows,
            "required_fields": {
                field: {"missing": field_missing[field], "missing_rate": rate(field_missing[field])}
                for field in required_fields
            },
            "observed_fields": {
                field: {"missing": field_missing[field], "missing_rate": rate(field_missing[field])}
                for field in observed_fields
            },
            "rows_with_any_required_missing": len(missing_rows),
            "dimension_value_count_distribution": summarize_distribution(value_counts),
            "dimensions_per_table_distribution": summarize_distribution(dimension_counts),
            "items_per_table_distribution": summarize_distribution(item_counts),
            "tables_with_zero_value_dimension": zero_value_dimension_tables,
            "dimension_value_representation": {
                "structured_values_array": dimensions_with_values_array,
                "value_count_only": dimensions_value_count_only,
                "tables_with_all_dimensions_structured": tables_with_structured_values,
            },
            "meta_status_counts": dict(sorted(meta_status_counts.items())),
            "catalog_version_counts": dict(sorted(catalog_version_counts.items())),
            "period_problem_counts": dict(sorted(period_problem_counts.items())),
            "cell_query_ready": cell_query_ready,
            "cell_query_ready_rate": rate(cell_query_ready),
        },
        "rows": {
            "duplicate_table_keys": duplicates,
            "invalid_table_keys": invalid_keys,
            "missing_required_fields": missing_rows,
            "collection_failures": collection_failures,
            "parse_failures": parse_failures,
            "period_problems": period_problem_rows,
        },
        "collection_record": collection_summary,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compare_value_counts(catalog_path: Path, meta_path: Path) -> dict[str, Any]:
    """catalog의 ``value_count``가 meta의 실제 ``len(values)``와 일치하는지 확인한다.

    catalog가 meta에서 values만 떼어낸 충실한 파생물인지 검증하는 절차다. 불일치가
    있으면 catalog의 값 개수를 셀 조회 계획의 근거로 쓸 수 없다.
    """
    actual: dict[tuple[str, str], int] = {}
    for _, record, _ in iter_records(meta_path):
        if record is None:
            continue
        table_key = str(record.get("table_key") or "")
        for dimension in record.get("dimensions") or []:
            if isinstance(dimension, dict):
                values = dimension.get("values")
                if isinstance(values, list):
                    actual[(table_key, str(dimension.get("obj_id") or ""))] = len(values)

    matched = mismatched = missing_in_meta = 0
    examples: list[dict[str, Any]] = []
    for _, record, _ in iter_records(catalog_path):
        if record is None:
            continue
        table_key = str(record.get("table_key") or "")
        for dimension in record.get("dimensions") or []:
            if not isinstance(dimension, dict):
                continue
            key = (table_key, str(dimension.get("obj_id") or ""))
            declared = dimension.get("value_count")
            if key not in actual:
                missing_in_meta += 1
            elif actual[key] == declared:
                matched += 1
            else:
                mismatched += 1
                if len(examples) < 20:
                    examples.append({"table_key": table_key, "obj_id": key[1],
                                     "catalog_value_count": declared, "meta_values_length": actual[key]})
    return {
        "meta_dimensions_with_values": len(actual),
        "matched": matched, "mismatched": mismatched,
        "catalog_dimensions_absent_from_meta": missing_in_meta,
        "consistent": mismatched == 0 and missing_in_meta == 0,
        "mismatch_examples": examples,
    }


def write_outputs(result: dict[str, Any], output_dir: Path, catalog_path: Path,
                  raw_meta_path: Path | None, freeze: dict[str, Any],
                  schema: str = "catalog", value_count_check: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = result["rows"]
    write_jsonl(output_dir / "duplicate_table_keys.jsonl", rows["duplicate_table_keys"])
    write_jsonl(output_dir / "invalid_table_keys.jsonl", rows["invalid_table_keys"])
    write_jsonl(output_dir / "missing_required_fields.jsonl", rows["missing_required_fields"])
    write_jsonl(output_dir / "collection_failures.jsonl", rows["collection_failures"])
    write_jsonl(output_dir / "period_problems.jsonl", rows["period_problems"])
    write_json(output_dir / "field_completeness.json", result["field_completeness"])
    manifest = {
        "generated_at": utc_now(),
        "audit_script": "src/develop/audit_kosis_catalog_v4.py",
        "schema": schema,
        "input": freeze,
        "raw_meta_input": str(raw_meta_path) if raw_meta_path else None,
        "collection_record": result["collection_record"],
        "record_totals": result["record_totals"],
        "value_count_cross_check": value_count_check,
        "outputs": sorted(path.name for path in output_dir.iterdir() if path.is_file()),
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def freeze_input(catalog_path: Path, skip_hash: bool) -> dict[str, Any]:
    stat = catalog_path.stat()
    return {
        "path": str(catalog_path),
        "bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": None if skip_hash else sha256_of(catalog_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a KOSIS catalog v4 JSONL for adapter readiness")
    parser.add_argument("--catalog", type=Path, required=True, help="감사할 JSONL (catalog 또는 meta)")
    parser.add_argument("--raw-meta", type=Path, help="수집 원본 JSONL (status/fetched_at 포함)")
    parser.add_argument("--schema", choices=sorted(SCHEMA_PROFILES), default="catalog",
                        help="입력 파일의 스키마 세대 (기본 catalog)")
    parser.add_argument("--compare-value-counts", type=Path,
                        help="catalog의 value_count를 이 meta 파일의 실제 값 개수와 대조한다")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--current-year", type=int, default=datetime.now(timezone.utc).year)
    parser.add_argument("--horizon-years", type=int, default=DEFAULT_HORIZON_YEARS,
                        help="장래추계 등 미래 시점 허용 폭 (기본 50년)")
    parser.add_argument("--limit", type=int, help="선행 N개 레코드만 감사 (스모크 테스트용)")
    parser.add_argument("--skip-hash", action="store_true", help="대용량 파일의 SHA-256 계산을 건너뛴다")
    args = parser.parse_args()

    freeze = freeze_input(args.catalog, args.skip_hash)
    result = audit(args.catalog, args.raw_meta, args.current_year, limit=args.limit,
                   horizon_years=args.horizon_years, schema=args.schema)
    check = compare_value_counts(args.catalog, args.compare_value_counts) if args.compare_value_counts else None
    manifest = write_outputs(result, args.output_dir, args.catalog, args.raw_meta, freeze,
                             schema=args.schema, value_count_check=check)
    print(json.dumps({"manifest": manifest, "field_completeness": result["field_completeness"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
