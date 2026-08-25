"""Build KOSIS cell queries from the frozen R4-B4 decisions."""
from __future__ import annotations
import re
from typing import Any
from src.claim_table_aligner import PERIOD_CODES
from src.develop.r4_alignment_contract import validate_query_plan

def to_kosis_period(display: str, prd_se_code: str) -> str:
    value = str(display or "").strip()
    if prd_se_code == "Y" and re.fullmatch(r"\d{4}", value):
        return value
    if prd_se_code == "Q":
        match = re.fullmatch(r"(\d{4})\s*([1-4])\s*/\s*4", value)
        if match:
            return f"{match.group(1)}0{match.group(2)}"
    if prd_se_code == "M":
        match = re.fullmatch(r"(\d{4})[.\s/-]*(0?[1-9]|1[0-2])", value)
        if match:
            return f"{match.group(1)}{int(match.group(2)):02d}"
    raise ValueError(f"unsupported display period: {display}")

def _profile_dimensions(frame_row: dict[str, Any]) -> list[dict[str, Any]]:
    profile = frame_row.get("_profile")
    if not isinstance(profile, dict) or not isinstance(profile.get("dimensions"), list):
        raise ValueError("profile dimensions are required for order verification")
    return [row for row in profile["dimensions"] if isinstance(row, dict)]

def resolve_obj_levels(frame_row: dict, dimension_selections: dict) -> dict[str, str]:
    profile_ids = [str(row.get("obj_id") or "") for row in _profile_dimensions(frame_row)]
    frame_axes = [axis for axis in (frame_row.get("axes") or []) if isinstance(axis, dict) and axis.get("axis_kind") == "DIMENSION"]
    frame_ids = [str(axis.get("axis_id") or "") for axis in frame_axes]
    if profile_ids != frame_ids:
        raise ValueError("profile dimensions order differs from frame axes order")
    levels = {}
    for pos, axis in enumerate(frame_axes, 1):
        axis_id = str(axis.get("axis_id") or ""); selection = dimension_selections.get(axis_id)
        if not isinstance(selection, dict) or not str(selection.get("value_id") or ""):
            raise ValueError(f"missing dimension selection: {axis_id}")
        levels[f"objL{pos}"] = str(selection["value_id"])
    if not levels:
        raise ValueError("KOSIS table-selection API requires at least objL1")
    return levels

def build_operand_queries(frame_row: dict, decision: dict) -> list[dict]:
    if str(decision.get("resolution_status") or "") not in {
        "QUERY_READY", "DERIVED_READY", "DERIVED_RANGE"
    }:
        return []
    plan = decision.get("period_plan") or {}; operands = plan.get("operands") or []
    if not operands:
        raise ValueError("ready decision requires operands")
    code = PERIOD_CODES.get(str(plan.get("prd_se") or "").strip())
    if not code:
        raise ValueError(f"unsupported period type: {plan.get('prd_se')}")
    table_key = str(frame_row.get("table_key") or ""); parts = table_key.split(":")
    if len(parts) != 2 or not all(parts):
        raise ValueError("table_key must be org_id:tbl_id")
    org_id, tbl_id = parts
    if f"{org_id}:{tbl_id}" != table_key:
        raise ValueError("table_key drift")
    declared_key = frame_row.get("declared_table_key") or frame_row.get("profile_table_key")
    if declared_key is not None and str(declared_key) != table_key:
        raise ValueError("table_key drift")
    item = decision.get("item_selection") or {}; levels = resolve_obj_levels(frame_row, decision.get("dimension_selections") or {})
    if decision.get("resolution_status") == "DERIVED_RANGE":
        starts = [o for o in operands if o.get("role") == "range_start"]; ends = [o for o in operands if o.get("role") == "range_end"]
        if len(starts) != 1 or len(ends) != 1:
            raise ValueError("DERIVED_RANGE requires one range_start and one range_end")
        work = [(ends[0], to_kosis_period(starts[0]["period"], code), to_kosis_period(ends[0]["period"], code))]
    else:
        work = [(o, to_kosis_period(o.get("period", ""), code), to_kosis_period(o.get("period", ""), code)) for o in operands]
    result = []
    for operand, start, end in work:
        query = {"org_id": org_id, "tbl_id": tbl_id, "itm_id": str(item.get("itm_id") or ""), "prd_se": code, "start_prd_de": start, "end_prd_de": end, "obj_levels": levels}
        if validate_query_plan(query, table_key=table_key):
            raise ValueError("generated query failed query-plan validation")
        query.update({"role": str(operand.get("role") or ""), "operand_index": operands.index(operand), "source_period_display": str(operand.get("period") or "")})
        result.append(query)
    return result
