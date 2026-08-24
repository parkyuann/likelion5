"""Resolve claims against one supplied table using live KOSIS metadata only.

The table mapping contains only ``target_id`` and ``table_key``.  ITEM,
dimension, period, unit, query-plan, score, rank, cell, and verdict fields are
not accepted there.  Profiles must be produced by r4c1_live_metadata from the
official TBL/ITM/PRD endpoints.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.develop.r4c1_claim_core_v2 import build_claim_core_v2
from src.develop.r4c1_live_metadata import ENDPOINTS
from src.develop.r4c1_projection_v2 import project_candidate_v2, validate_target_v2


CONTRACT_VERSION = "r4c1-oracle-table-only-v1"


class OracleTableInputError(ValueError):
    """Raised for leakage, duplicate identity, or non-live profile input."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def target_id_from_routed(row: Mapping[str, Any]) -> str:
    if row.get("target_id"):
        return str(row["target_id"])
    article = row.get("article_idx")
    sentence = row.get("article_sentence_id")
    span = row.get("value_span_id")
    if article in (None, "") or sentence in (None, "") or not span:
        raise OracleTableInputError("routed row has no reproducible target identity")
    sentence_text = str(sentence)
    if not sentence_text.startswith("s"):
        sentence_text = "s" + sentence_text
    span_text = str(span)
    if span_text.startswith(sentence_text + ":"):
        span_text = span_text[len(sentence_text) + 1 :]
    return f"dev:{article}:{sentence_text}:{span_text}"


def _routed_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        target_id = target_id_from_routed(row)
        if target_id in result:
            raise OracleTableInputError(f"duplicate routed target_id: {target_id}")
        result[target_id] = dict(row)
    return result


def _table_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    allowed = {"target_id", "table_key"}
    for row in rows:
        extra = set(row) - allowed
        missing = allowed - set(row)
        if extra or missing:
            raise OracleTableInputError(
                f"table mapping must contain only target_id/table_key: extra={sorted(extra)} missing={sorted(missing)}"
            )
        target_id = str(row.get("target_id") or "")
        table_key = str(row.get("table_key") or "")
        if not target_id or table_key.count(":") != 1:
            raise OracleTableInputError("table mapping identity is invalid")
        if target_id in result:
            raise OracleTableInputError(f"duplicate table mapping: {target_id}")
        result[target_id] = table_key
    return result


def _validate_live_profile(profile: Mapping[str, Any]) -> None:
    if profile.get("source") != "KOSIS_METADATA_API":
        raise OracleTableInputError("profile is not sourced from KOSIS metadata API")
    if tuple(profile.get("source_endpoints") or ()) != ENDPOINTS:
        raise OracleTableInputError("profile endpoint contract must be TBL/ITM/PRD")
    response_sha = profile.get("response_sha256")
    if not isinstance(response_sha, Mapping) or set(response_sha) != set(ENDPOINTS):
        raise OracleTableInputError("profile response SHA inventory is incomplete")
    if any(not isinstance(response_sha[key], str) or len(response_sha[key]) != 64 for key in ENDPOINTS):
        raise OracleTableInputError("profile response SHA is invalid")
    dimensions = profile.get("dimensions")
    if isinstance(dimensions, list):
        orders = [dimension.get("obj_order") for dimension in dimensions if isinstance(dimension, Mapping)]
        if orders != list(range(1, len(dimensions) + 1)):
            raise OracleTableInputError("live dimension order is not contiguous objL1..objLn")


def _profile_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        profile = dict(row)
        _validate_live_profile(profile)
        table_key = str(profile.get("table_key") or "")
        if table_key.count(":") != 1 or table_key in result:
            raise OracleTableInputError(f"profile table identity invalid or duplicate: {table_key}")
        result[table_key] = profile
    return result


def resolve_oracle_tables(
    routed_rows: Iterable[Mapping[str, Any]],
    table_rows: Iterable[Mapping[str, Any]],
    live_profiles: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build table-specific assignments without reading embedded metadata or cells."""

    routed = _routed_index(routed_rows)
    tables = _table_index(table_rows)
    profiles = _profile_index(live_profiles)
    if set(routed) != set(tables):
        raise OracleTableInputError(
            f"routed/table target mismatch: routed={len(routed)} table={len(tables)}"
        )

    outputs: list[dict[str, Any]] = []
    distribution: Counter[str] = Counter()
    profile_available = 0
    for target_id in sorted(routed):
        table_key = tables[target_id]
        profile = profiles.get(table_key)
        core = build_claim_core_v2(routed[target_id])
        projection = project_candidate_v2(core, profile)
        resolution = validate_target_v2([projection])
        profile_available += int(profile is not None)
        status = resolution.outcome if resolution.outcome == "QUERY_READY" else str(resolution.hold_reason)
        distribution[status] += 1
        output = {
            "contract_version": CONTRACT_VERSION,
            "target_id": target_id,
            "oracle_scope": "table_only",
            "table_key": table_key,
            "metadata_source": "KOSIS_METADATA_API" if profile is not None else "UNAVAILABLE",
            "metadata_profile_sha256": profile.get("profile_sha256") if profile else None,
            "metadata_response_sha256": dict(profile.get("response_sha256") or {}) if profile else {},
            "claim_core_sha256": core.canonical_sha256,
            "projection": asdict(projection),
            "resolution": asdict(resolution),
            "forbidden_runtime_inputs_accessed": [],
            "cell_api_calls": 0,
        }
        output["canonical_sha256"] = _sha(output)
        outputs.append(output)

    report = {
        "contract_version": CONTRACT_VERSION,
        "oracle_scope": "table_only",
        "targets": len(outputs),
        "unique_tables": len(set(tables.values())),
        "profile_available": profile_available,
        "profile_unavailable": len(outputs) - profile_available,
        "outcome_distribution": dict(sorted(distribution.items())),
        "metadata_source": "KOSIS_METADATA_API",
        "cell_api_calls": 0,
        "forbidden_runtime_inputs_accessed": [],
    }
    return outputs, report


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise OracleTableInputError(f"{path}:{number}: object required")
        result.append(row)
    return result


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.exists() or temporary.exists():
        raise FileExistsError(f"refusing to overwrite output: {path}")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routed", required=True, type=Path)
    parser.add_argument("--table-map", required=True, type=Path)
    parser.add_argument("--live-profiles", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    outputs, report = resolve_oracle_tables(
        _read_jsonl(args.routed), _read_jsonl(args.table_map), _read_jsonl(args.live_profiles)
    )
    _write_new(args.output, b"".join(_canonical_bytes(row) + b"\n" for row in outputs))
    _write_new(args.report, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTRACT_VERSION",
    "OracleTableInputError",
    "resolve_oracle_tables",
    "target_id_from_routed",
]
