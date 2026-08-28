"""Deterministic same-series requery for annual level/change claims.

Retrieval is never rerun after observing a value. The chosen table, item, and
dimensions stay sealed; only the annual period is changed for one baseline
cell query.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from src.develop.run_pipeline_operational_v2 import compare_official_cell, fetch_exact_single_cell


CONTRACT_VERSION = "annual-same-series-requery-shadow-v1"
_CHANGE_SUFFIX = re.compile(r"(?:전년대비)?(?:증가|감소|증감|변화)?(?:량|률|율|폭)$")
_FREQUENCY_WORDS = re.compile(r"(?:연간|연도별|월간|월별|분기별|분기)")


class AnnualRequeryError(RuntimeError):
    pass


def _fields(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("retrieval_fields")
    return value if isinstance(value, Mapping) else {}


def _periods(row: Mapping[str, Any]) -> tuple[str, str]:
    period = _fields(row).get("period")
    period = period if isinstance(period, Mapping) else {}
    measurement = period.get("measurement") if isinstance(period.get("measurement"), Mapping) else {}
    baseline = period.get("baseline") if isinstance(period.get("baseline"), Mapping) else {}
    return str(measurement.get("absolute") or ""), str(baseline.get("absolute") or "")


def indicator_key(row: Mapping[str, Any]) -> str:
    """Collapse frequency/change modifiers without inspecting numeric values."""
    text = str(_fields(row).get("indicator") or row.get("indicator_label") or "")
    text = re.sub(r"[\s_./:(),-]+", "", text)
    text = _FREQUENCY_WORDS.sub("", text)
    return _CHANGE_SUFFIX.sub("", text)


def derive_baseline_plan(current_plan: Mapping[str, Any], baseline_year: str) -> dict[str, Any]:
    plan = deepcopy(dict(current_plan))
    if str(plan.get("prd_se") or "") not in {"Y", "A"}:
        raise AnnualRequeryError("ANNUAL_FREQUENCY_REQUIRED")
    start, end = str(plan.get("start_prd_de") or ""), str(plan.get("end_prd_de") or "")
    if start != end or not re.fullmatch(r"\d{4}", start) or not re.fullmatch(r"\d{4}", baseline_year):
        raise AnnualRequeryError("SINGLE_ANNUAL_CELL_REQUIRED")
    if int(baseline_year) >= int(start):
        raise AnnualRequeryError("BASELINE_MUST_PRECEDE_MEASUREMENT")
    plan["start_prd_de"] = baseline_year
    plan["end_prd_de"] = baseline_year
    changed = {key for key in set(plan) | set(current_plan) if plan.get(key) != current_plan.get(key)}
    if changed != {"start_prd_de", "end_prd_de"}:
        raise AnnualRequeryError("REQUERY_SERIES_MUTATED")
    return plan


def _direction(value: Decimal) -> str:
    return "INCREASE" if value > 0 else "DECREASE" if value < 0 else "UNCHANGED"


def _component_verdict(value_comparison: Mapping[str, Any], direction_match: bool = True) -> str:
    verdict = str(value_comparison.get("verdict") or "UNVERIFIABLE")
    if verdict == "UNVERIFIABLE":
        return verdict
    return "VERIFIED" if verdict == "VERIFIED" and direction_match else "REFUTED"


def verify_annual_requery(
    *, rows: Sequence[Mapping[str, Any]], current_plan: Mapping[str, Any],
    current_cell_result: Mapping[str, Any], current_target_id: str,
    cell_fetcher: Callable[[dict[str, Any]], Any], official_unit: str = "",
) -> dict[str, Any]:
    """Verify a current level and its stated annual change with one extra cell."""
    current_row = next((dict(row) for row in rows if str(row.get("value_span_id") or "") == current_target_id), None)
    if current_row is None:
        raise AnnualRequeryError("CURRENT_TARGET_NOT_FOUND")
    current_year = str(current_plan.get("start_prd_de") or "")
    change_rows = []
    for source in rows:
        row = dict(source)
        measurement, baseline = _periods(row)
        if (
            indicator_key(row) == indicator_key(current_row)
            and str(_fields(row).get("measurement_type") or "") in {"CHANGE_RATE", "CHANGE_POINT", "DIFFERENCE"}
            and measurement == current_year
            and baseline
        ):
            change_rows.append(row)
    if len(change_rows) != 1:
        raise AnnualRequeryError(f"ANNUAL_CHANGE_NOT_UNIQUE:{len(change_rows)}")
    change_row = change_rows[0]
    _, baseline_year = _periods(change_row)
    baseline_plan = derive_baseline_plan(current_plan, baseline_year)
    current_cell = current_cell_result.get("cell") if isinstance(current_cell_result.get("cell"), Mapping) else None
    if current_cell_result.get("status") != "CELL_RESOLVED" or current_cell is None:
        raise AnnualRequeryError("CURRENT_CELL_NOT_RESOLVED")
    baseline_cell_result = fetch_exact_single_cell(baseline_plan, cell_fetcher)
    baseline_cell = baseline_cell_result.get("cell") if isinstance(baseline_cell_result.get("cell"), Mapping) else None
    if baseline_cell_result.get("status") != "CELL_RESOLVED" or baseline_cell is None:
        raise AnnualRequeryError(str(baseline_cell_result.get("status") or "BASELINE_CELL_NOT_RESOLVED"))
    unit = str(official_unit or current_row.get("value_unit") or "")
    current_comparison = compare_official_cell(
        str(current_row.get("value_text") or ""), str(current_row.get("value_unit") or ""), current_cell, unit,
    )
    try:
        current_value = Decimal(str(current_cell.get("DT")))
        baseline_value = Decimal(str(baseline_cell.get("DT")))
    except InvalidOperation as exc:
        raise AnnualRequeryError("OFFICIAL_CELL_NOT_NUMERIC") from exc
    if baseline_value == 0:
        raise AnnualRequeryError("BASELINE_ZERO")
    measurement_type = str(_fields(change_row).get("measurement_type") or "")
    signed_change = (
        (current_value - baseline_value) / abs(baseline_value) * Decimal(100)
        if measurement_type == "CHANGE_RATE"
        else current_value - baseline_value
    )
    change_unit = "%" if measurement_type == "CHANGE_RATE" else str(change_row.get("value_unit") or unit)
    value_comparison = compare_official_cell(
        str(change_row.get("value_text") or ""), str(change_row.get("value_unit") or ""),
        {"DT": str(abs(signed_change))}, change_unit,
    )
    expected_direction = str(_fields(change_row).get("value_direction") or "")
    actual_direction = _direction(signed_change)
    direction_match = not expected_direction or expected_direction == actual_direction
    change_verdict = _component_verdict(value_comparison, direction_match)
    components = {
        "current_level": current_comparison,
        "change": {
            "verdict": change_verdict,
            "reason": str(value_comparison.get("reason") or "") if direction_match else "DIRECTION_MISMATCH",
            "value_comparison": dict(value_comparison),
            "expected_direction": expected_direction,
            "actual_direction": actual_direction,
        },
    }
    verdicts = {str(value.get("verdict") or "UNVERIFIABLE") for value in components.values()}
    verdict = "UNVERIFIABLE" if "UNVERIFIABLE" in verdicts else "VERIFIED" if verdicts == {"VERIFIED"} else "REFUTED"
    result = {
        "contract_version": CONTRACT_VERSION,
        "verdict": verdict,
        "selection_rule": "sealed-series-period-only-requery-v1",
        "table_key": f"{current_plan.get('org_id')}:{current_plan.get('tbl_id')}",
        "plans": {"current": dict(current_plan), "baseline": baseline_plan},
        "cells": {"current": dict(current_cell_result), "baseline": baseline_cell_result},
        "official": {
            "current": str(current_value), "baseline": str(baseline_value),
            "signed_change": str(signed_change), "change_unit": change_unit, "level_unit": unit,
        },
        "claims": {
            "current": {"target_id": current_target_id, "value_text": current_row.get("value_text")},
            "change": {"target_id": change_row.get("value_span_id"), "value_text": change_row.get("value_text")},
        },
        "components": components,
        "call_ledger": {"baseline_cell_api": {"used": 1, "limit": 1}},
        "answer": (
            f"{current_year}년 공식값은 {current_value}{unit}, {baseline_year}년은 {baseline_value}{unit}이며 "
            f"공식 전년 대비 변화는 {signed_change}{change_unit}입니다. "
            f"현재 수준 판정은 {current_comparison.get('verdict')}, 변화 판정은 {change_verdict}입니다."
        ),
    }
    serializable = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    serializable["sha256"] = hashlib.sha256(
        json.dumps(serializable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return serializable


__all__ = [
    "AnnualRequeryError", "CONTRACT_VERSION", "derive_baseline_plan", "indicator_key", "verify_annual_requery",
]
