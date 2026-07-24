"""구조화된 claim 조건을 KOSIS v4 profile의 항목·차원·기간 코드로 보수적으로 정렬한다."""

from __future__ import annotations

import re
from typing import Any


PERIOD_CODES = {"년": "Y", "월": "M", "분기": "Q", "year": "Y", "month": "M", "quarter": "Q"}
TOTAL_VALUE_NAMES = {"계", "총계", "전체", "합계", "전국"}


def normalized(value: object) -> str:
    return "".join(str(value or "").lower().split())


def exact_matches(rows: list[dict[str, Any]], field: str, term: str) -> list[dict[str, Any]]:
    target = normalized(term)
    return [row for row in rows if normalized(row.get(field)) == target]


def default_dimension_value(values: list[dict[str, Any]]) -> dict[str, Any] | None:
    """조건이 기사에 없을 때도 안전한 단일값 또는 총계 계열만 자동 선택한다."""
    if len(values) == 1:
        return values[0]
    total_values = [value for value in values if normalized(value.get("value_name")) in TOTAL_VALUE_NAMES]
    return total_values[0] if len(total_values) == 1 else None


def normalize_period_for_api(period: str, period_type: str) -> str | None:
    """현재 월·연 단위만 확정적으로 API 파라미터 형태로 변환한다."""
    compact = re.sub(r"[^0-9]", "", str(period or ""))
    code = PERIOD_CODES.get(period_type)
    if code == "Y" and len(compact) == 4:
        return compact
    if code == "M" and len(compact) == 6:
        return compact
    return None


def align_profile(
    profile: dict[str, Any], *, item_term: str, dimension_terms: dict[str, str] | None = None,
    period: str | None = None, period_type: str | None = None,
) -> dict[str, Any]:
    """표 하나 안에서 항목·차원값·기간을 모두 코드로 정렬하거나 중단 사유를 돌려준다."""
    items = profile.get("items") if isinstance(profile.get("items"), list) else []
    item_matches = exact_matches(items, "itm_nm", item_term)
    if len(item_matches) != 1:
        return {"align_status": "ITEM_AMBIGUOUS", "matched_item_id": None, "matched_dimensions": {}, "reason": "item_exact_match_required"}
    dimensions = profile.get("dimensions") if isinstance(profile.get("dimensions"), list) else []
    requested = dimension_terms or {}
    matched_dimensions: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        obj_id = str(dimension.get("obj_id") or "")
        obj_name = str(dimension.get("obj_nm") or "")
        values = dimension.get("values") if isinstance(dimension.get("values"), list) else []
        requested_value = next((value for name, value in requested.items() if normalized(name) == normalized(obj_name)), None)
        if requested_value is not None:
            matches = exact_matches(values, "value_name", requested_value)
            if len(matches) != 1:
                return {"align_status": "DIM_MISSING", "matched_item_id": item_matches[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                        "reason": f"dimension_value_exact_match_required:{obj_name}"}
            matched_dimensions[obj_id] = str(matches[0].get("value_id") or "")
            continue
        default = default_dimension_value(values)
        if default is None:
            return {"align_status": "DIM_MISSING", "matched_item_id": item_matches[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                    "reason": f"dimension_condition_missing:{obj_name}"}
        matched_dimensions[obj_id] = str(default.get("value_id") or "")
        defaults[obj_id] = str(default.get("value_name") or "")
    profile_period_types = {str(value) for value in profile.get("period_types", [])}
    if period is not None:
        if not period_type or period_type not in PERIOD_CODES or period_type not in profile_period_types:
            return {"align_status": "PERIOD_MISMATCH", "matched_item_id": item_matches[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                    "reason": "period_type_not_available"}
        api_period = normalize_period_for_api(period, period_type)
        if api_period is None:
            return {"align_status": "PERIOD_MISMATCH", "matched_item_id": item_matches[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                    "reason": "period_format_not_supported"}
    else:
        api_period = None
    return {
        "align_status": "ALIGNED", "matched_item_id": str(item_matches[0].get("itm_id") or ""),
        "matched_dimensions": matched_dimensions, "defaulted_dimensions": defaults,
        "matched_period": api_period, "period_type": period_type,
    }


def build_cell_query(profile: dict[str, Any], alignment: dict[str, Any]) -> dict[str, Any]:
    """ALIGNED 결과만 KOSIS statisticsData 표 선택 방식의 안전한 요청 조건으로 바꾼다."""
    if alignment.get("align_status") != "ALIGNED":
        raise ValueError("cell query requires align_status=ALIGNED")
    period_type = str(alignment.get("period_type") or "")
    period_code = PERIOD_CODES.get(period_type)
    if not period_code or not alignment.get("matched_period"):
        raise ValueError("cell query requires a supported aligned period")
    levels: dict[str, str] = {}
    for position, dimension in enumerate(profile.get("dimensions", []), start=1):
        obj_id = str(dimension.get("obj_id") or "")
        value_id = str(alignment.get("matched_dimensions", {}).get(obj_id) or "")
        if not value_id:
            raise ValueError(f"missing aligned value for dimension {obj_id}")
        levels[f"objL{position}"] = value_id
    if not levels:
        raise ValueError("KOSIS table-selection API requires at least objL1")
    return {
        "org_id": str(profile.get("org_id") or ""), "tbl_id": str(profile.get("tbl_id") or ""),
        "itm_id": str(alignment["matched_item_id"]), "prd_se": period_code,
        "start_prd_de": str(alignment["matched_period"]), "end_prd_de": str(alignment["matched_period"]),
        "obj_levels": levels,
    }


def build_probe_alignment(profile: dict[str, Any]) -> dict[str, Any]:
    """profile의 첫 항목과 최신 월·연 period를 써 셀 API 계약을 확인하는 보수적 probe 조건을 만든다."""
    items = profile.get("items") if isinstance(profile.get("items"), list) else []
    if not items or not str(items[0].get("itm_nm") or ""):
        return {"align_status": "ITEM_AMBIGUOUS", "reason": "probe_item_missing"}
    periods = profile.get("periods") if isinstance(profile.get("periods"), list) else []
    supported = [period for period in periods if isinstance(period, dict) and str(period.get("PRD_SE") or "") in PERIOD_CODES]
    if not supported:
        return {"align_status": "PERIOD_MISMATCH", "reason": "probe_supported_period_missing"}
    chosen = max(supported, key=lambda period: str(period.get("END_PRD_DE") or ""))
    return align_profile(
        profile, item_term=str(items[0]["itm_nm"]), period=str(chosen.get("END_PRD_DE") or ""), period_type=str(chosen.get("PRD_SE") or ""),
    )
