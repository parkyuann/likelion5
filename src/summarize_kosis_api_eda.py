"""KOSIS API profile/raw checkpoint에서 catalog 설계 결정을 위한 EDA 요약을 만든다."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def distribution(values: list[int]) -> dict[str, float | int]:
    """표본 크기가 작아도 min/median/max를 같은 형식으로 기록한다."""
    if not values:
        return {"min": 0, "median": 0, "max": 0}
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def summarize(profiles: list[dict[str, Any]], raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """카탈로그 크기·값 전개·API 운영 정책에 필요한 분포만 집계한다."""
    dimensions = [dimension for profile in profiles for dimension in profile.get("dimensions", [])]
    items = [item for profile in profiles for item in profile.get("items", [])]
    cardinalities = [len(dimension.get("values", [])) for dimension in dimensions]
    periods = [
        str(period.get("PRD_SE") or "")
        for profile in profiles for period in profile.get("periods", [])
        if isinstance(period, dict)
    ]
    latency: dict[str, dict[str, float | int]] = {}
    for endpoint in sorted({str(row.get("endpoint") or "") for row in raw_rows}):
        values = [float(row["latency_ms"]) for row in raw_rows if row.get("endpoint") == endpoint and row.get("latency_ms") is not None]
        latency[endpoint] = distribution([int(value) for value in values])
    return {
        "tables": len(profiles),
        "profile_status_counts": dict(sorted(Counter(str(profile.get("profile_status") or "") for profile in profiles).items())),
        "items_total": len(items),
        "items_per_table": distribution([len(profile.get("items", [])) for profile in profiles]),
        "item_unit_missing": sum(not item.get("unit_name") for item in items),
        "item_unit_counts": dict(Counter(str(item.get("unit_name") or "<missing>") for item in items).most_common(20)),
        "dimensions_total": len(dimensions),
        "dimensions_per_table": distribution([len(profile.get("dimensions", [])) for profile in profiles]),
        "dimension_values_per_dimension": distribution(cardinalities),
        "dimension_cardinality_bins": {
            "1_32": sum(value <= 32 for value in cardinalities),
            "33_256": sum(33 <= value <= 256 for value in cardinalities),
            "over_256": sum(value > 256 for value in cardinalities),
        },
        "top_dimension_names": dict(Counter(str(dimension.get("dimension_name") or "") for dimension in dimensions).most_common(20)),
        "period_type_counts": dict(sorted(Counter(periods).items())),
        "endpoint_status_counts": dict(sorted(Counter(f"{row.get('endpoint')}:{row.get('status')}" for row in raw_rows).items())),
        "endpoint_latency_ms": latency,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize KOSIS API EDA profile outputs")
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(read_jsonl(args.profiles), read_jsonl(args.raw))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
