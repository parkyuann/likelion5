"""Runtime-safe validation for a resolved KOSIS query plan.

This module contains only deterministic inventory validation.  It deliberately
does not import an evaluator, report, audit, or shadow implementation.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


def _norm(value: Any) -> str:
    return re.sub(r"[\s\-_./:(),]+", "", str(value or "")).casefold()


def _period_frequency(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if text in {"y", "year", "annual", "annually", "연", "년", "연간"}:
        return "Y"
    if text in {"m", "month", "monthly", "월", "월간"}:
        return "M"
    if text in {"q", "quarter", "quarterly", "분기", "분기별"}:
        return "Q"
    return None


def _profile_period_key(value: Any, frequency: str) -> int | None:
    text = re.sub(r"\s+", "", str(value or ""))
    year = re.search(r"\d{4}", text)
    if not year:
        return None
    year_value = int(year.group(0))
    if frequency == "Y":
        return year_value * 100
    if frequency == "Q":
        match = re.match(r"\d{4}0([1-4])$", text) or re.match(r"\d{4}([1-4])/4$", text)
        return year_value * 100 + int(match.group(1)) if match else None
    match = re.search(r"(?:-|/)?(\d{1,2})$", text[4:])
    return year_value * 100 + int(match.group(1)) if match else None


def validate_query_plan_inventory(
    plan: Mapping[str, Any],
    profile: Mapping[str, Any],
    claim_core: Any | None = None,
) -> list[str]:
    """Return stable contract error codes for one query plan/profile pair."""
    errors: list[str] = []
    required = {"org_id", "tbl_id", "itm_id", "prd_se", "start_prd_de", "end_prd_de", "obj_levels"}
    if set(plan) != required:
        errors.append("QUERY_PLAN_KEYS")
        return errors
    table = str(profile.get("table_key") or "")
    if f"{plan['org_id']}:{plan['tbl_id']}" != table:
        errors.append("TABLE_ID_MISMATCH")
    items = [item for item in profile.get("items") or [] if isinstance(item, Mapping)]
    selected = next((item for item in items if str(item.get("itm_id")) == str(plan["itm_id"])), None)
    if selected is None:
        errors.append("ITEM_ID_OR_UNIT")
    frequency = str(plan["prd_se"])
    if frequency not in {"Y", "M", "Q"} or str(plan["start_prd_de"]) != str(plan["end_prd_de"]):
        errors.append("PERIOD_PARAMETER")
    else:
        period_rows = [
            period for period in profile.get("periods") or []
            if isinstance(period, Mapping) and _period_frequency(period.get("PRD_SE")) == frequency
        ]
        requested = _profile_period_key(plan["start_prd_de"], frequency)
        if requested is None or not any(
            (lo := _profile_period_key(period.get("STRT_PRD_DE"), frequency)) is not None
            and (hi := _profile_period_key(period.get("END_PRD_DE"), frequency)) is not None
            and lo <= requested <= hi
            for period in period_rows
        ):
            errors.append("PERIOD_RANGE")
    dimensions = [dimension for dimension in profile.get("dimensions") or [] if isinstance(dimension, Mapping)]
    expected_levels = {f"objL{index}" for index in range(1, len(dimensions) + 1)}
    if set(plan["obj_levels"]) != expected_levels:
        errors.append("OBJ_LEVEL_KEYS")
    selected_dimension_values: list[Mapping[str, Any]] = []
    for index, dimension in enumerate(dimensions, 1):
        values = [value for value in dimension.get("values") or [] if isinstance(value, Mapping)]
        selected_value = next(
            (value for value in values if str(value.get("value_id")) == str(plan["obj_levels"].get(f"objL{index}"))),
            None,
        )
        if selected_value is None:
            errors.append(f"OBJ_LEVEL_VALUE:{index}")
        else:
            selected_dimension_values.append(selected_value)
    if selected is not None:
        selected_units = [
            str(source.get("unit_nm") or "").strip()
            for source in [selected, *selected_dimension_values]
            if str(source.get("unit_nm") or "").strip()
        ]
        normalized_units = {_norm(unit) for unit in selected_units}
        if not selected_units or len(normalized_units) != 1:
            errors.append("ITEM_ID_OR_UNIT")
        elif claim_core is not None:
            atom = claim_core.atoms.get("unit") if hasattr(claim_core, "atoms") else (claim_core.get("atoms", {}).get("unit") if isinstance(claim_core, Mapping) else None)
            surface = atom.surface if hasattr(atom, "surface") else atom.get("surface") if isinstance(atom, Mapping) else ""
            status = atom.status if hasattr(atom, "status") else atom.get("status") if isinstance(atom, Mapping) else "UNKNOWN"
            if status == "EXPLICIT" and _norm(surface) not in normalized_units:
                krw_units = {"원", "천원", "만원", "백만원", "억원", "조원"}
                if str(surface).strip() not in krw_units or not all(unit in krw_units for unit in selected_units):
                    errors.append("UNIT_MISMATCH")
    return errors


__all__ = ["validate_query_plan_inventory"]
