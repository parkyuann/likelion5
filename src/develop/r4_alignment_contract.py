"""R4 candidate-specific alignment artifact validation and drift guard."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.retrieval_schema import CELL_STATUSES, RESOLUTION_STATUSES


FRAME_FIELDS = ("alignment_target_id", "split", "table_key", "observation_id")
QUERY_FIELDS = (
    "org_id", "tbl_id", "itm_id", "prd_se", "start_prd_de", "end_prd_de",
    "obj_levels",
)
_OBJ_LEVEL_RE = re.compile(r"objL([1-8])$")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def frame_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(str(row.get(field) or "") for field in FRAME_FIELDS)  # type: ignore[return-value]


def validate_frame(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        target_id, split, table_key, observation_id = frame_identity(row)
        prefix = f"row[{index}]"
        if not target_id:
            errors.append(f"{prefix} alignment_target_id is required")
        elif target_id in seen:
            errors.append(f"{prefix} duplicate alignment_target_id: {target_id}")
        seen.add(target_id)
        if split not in {"dev", "test"}:
            errors.append(f"{prefix} split must be dev or test")
        if not table_key or ":" not in table_key:
            errors.append(f"{prefix} table_key must be org_id:tbl_id")
        if not observation_id:
            errors.append(f"{prefix} observation_id is required")
    return errors


def frame_sha256(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [frame_identity(row) for row in rows],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def frame_drift_errors(
    frozen_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]
) -> list[str]:
    errors = [
        *(f"frozen: {error}" for error in validate_frame(frozen_rows)),
        *(f"candidate: {error}" for error in validate_frame(candidate_rows)),
    ]
    frozen = [frame_identity(row) for row in frozen_rows]
    candidate = [frame_identity(row) for row in candidate_rows]
    if len(frozen) != len(candidate):
        errors.append(
            f"frame row count drift: frozen={len(frozen)} candidate={len(candidate)}"
        )
    for index, (expected, observed) in enumerate(zip(frozen, candidate)):
        if expected != observed:
            errors.append(
                f"frame identity/order drift at row[{index}]: "
                f"frozen={expected} candidate={observed}"
            )
    return errors


def validate_query_plan(plan: dict[str, Any], *, table_key: str) -> list[str]:
    errors: list[str] = []
    for field in QUERY_FIELDS:
        if field not in plan or plan[field] in (None, "", {}):
            errors.append(f"query_plan.{field} is required")
    if plan.get("org_id") and plan.get("tbl_id"):
        observed_key = f"{plan['org_id']}:{plan['tbl_id']}"
        if observed_key != table_key:
            errors.append(
                f"query_plan table drift: expected={table_key} observed={observed_key}"
            )
    levels = plan.get("obj_levels")
    if isinstance(levels, dict):
        positions = []
        for key, value in levels.items():
            match = _OBJ_LEVEL_RE.fullmatch(str(key))
            if not match:
                errors.append(f"invalid query obj level: {key}")
                continue
            if not str(value or ""):
                errors.append(f"query obj level has empty value: {key}")
            positions.append(int(match.group(1)))
        if positions and set(positions) != set(range(1, max(positions) + 1)):
            errors.append("query obj levels must be contiguous from objL1")
    return errors


def validate_alignment_payload(row: dict[str, Any]) -> list[str]:
    errors = validate_frame([row])
    resolution = str(row.get("resolution_status") or "")
    cell = str(row.get("cell_status") or "")
    if resolution not in RESOLUTION_STATUSES:
        errors.append(f"invalid resolution_status: {resolution}")
    if cell not in CELL_STATUSES:
        errors.append(f"invalid cell_status: {cell}")
    for field in ("match_evidence", "competing_matches", "defaulted_axes"):
        if not isinstance(row.get(field), list):
            errors.append(f"{field} must be a list")
    plan = row.get("query_plan")
    if resolution == "QUERY_READY":
        if not isinstance(plan, dict) or not plan:
            errors.append("QUERY_READY requires query_plan")
        else:
            errors.extend(validate_query_plan(plan, table_key=str(row.get("table_key") or "")))
    elif resolution in {"DERIVED_READY", "DERIVED_RANGE"}:
        if plan:
            errors.append("derived resolution must not include query_plan")
    elif plan:
        errors.append("blocked resolution must not include query_plan")
    if cell != "NOT_QUERIED" and resolution != "QUERY_READY":
        errors.append("queried cell status requires QUERY_READY")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    frozen = read_jsonl(args.frozen)
    candidate = read_jsonl(args.candidate)
    errors = frame_drift_errors(frozen, candidate)
    result = {
        "contract_version": "r4-candidate-axis-frame-v1",
        "frozen_rows": len(frozen),
        "candidate_rows": len(candidate),
        "frozen_frame_sha256": frame_sha256(frozen),
        "candidate_frame_sha256": frame_sha256(candidate),
        "valid": not errors,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
