"""구조화된 claim 조건을 KOSIS v4 profile의 항목·차원·기간 코드로 보수적으로 정렬한다."""

from __future__ import annotations

import re
from typing import Any

try:
    from .retrieval_schema import AXIS_KINDS, CandidateAxis
except ImportError:  # pragma: no cover - direct module execution
    from retrieval_schema import AXIS_KINDS, CandidateAxis


PERIOD_CODES = {
    "년": "Y", "연": "Y", "year": "Y", "annual": "Y", "y": "Y",
    "월": "M", "month": "M", "monthly": "M", "m": "M",
    "분기": "Q", "quarter": "Q", "quarterly": "Q", "q": "Q",
}
TOTAL_VALUE_NAMES = {"계", "총계", "전체", "합계", "전국"}
ITEM_TERMINAL_PARTICLES = ("은", "는", "이", "가", "을", "를", "의")


def build_candidate_axis_inventory(profile: dict[str, Any]) -> list[CandidateAxis]:
    """후보 profile을 ITEM+dimension axis inventory로 손실 없이 정규화한다.

    이 함수는 claim 의미를 어느 axis에도 배치하지 않는다. R4 resolver가 이후 모든
    axis를 경쟁 후보로 비교할 수 있도록 metadata의 역할·순서·코드를 보존할 뿐이다.
    """
    item_rows = profile.get("items") if isinstance(profile.get("items"), list) else []
    item_values = [
        {
            "value_id": str(row.get("itm_id") or ""),
            "value_name": str(row.get("itm_nm") or ""),
            "unit_name": str(row.get("unit_nm") or ""),
        }
        for row in item_rows if isinstance(row, dict)
    ]
    axes = [CandidateAxis(
        axis_kind="ITEM",
        axis_id="ITEM",
        axis_name="항목",
        values=item_values,
        metadata_complete=bool(item_values) and all(
            row["value_id"] and row["value_name"] for row in item_values
        ),
    )]
    dimensions = (
        profile.get("dimensions")
        if isinstance(profile.get("dimensions"), list) else []
    )
    for position, dimension in enumerate(dimensions, start=1):
        if not isinstance(dimension, dict):
            continue
        raw_values = (
            dimension.get("values")
            if isinstance(dimension.get("values"), list) else []
        )
        values = [
            {
                "value_id": str(row.get("value_id") or ""),
                "value_name": str(row.get("value_name") or ""),
            }
            for row in raw_values if isinstance(row, dict)
        ]
        axis_id = str(dimension.get("obj_id") or "")
        axis_name = str(dimension.get("obj_nm") or "")
        axes.append(CandidateAxis(
            axis_kind="DIMENSION",
            axis_id=axis_id,
            axis_name=axis_name,
            position=position,
            values=values,
            metadata_complete=bool(axis_id and axis_name and values) and all(
                row["value_id"] and row["value_name"] for row in values
            ),
        ))
    return axes


def validate_candidate_axis_inventory(axes: list[CandidateAxis]) -> list[str]:
    """후보별 resolver 입력이 API query 순서를 보존하는지 검사한다."""
    errors: list[str] = []
    item_axes = [axis for axis in axes if axis.axis_kind == "ITEM"]
    if len(item_axes) != 1:
        errors.append("candidate inventory requires exactly one ITEM axis")
    axis_ids: set[str] = set()
    positions: set[int] = set()
    for axis in axes:
        if axis.axis_kind not in AXIS_KINDS:
            errors.append(f"invalid axis_kind: {axis.axis_kind}")
        if not axis.axis_id or not axis.axis_name:
            errors.append("axis_id and axis_name are required")
        if axis.axis_id in axis_ids:
            errors.append(f"duplicate axis_id: {axis.axis_id}")
        axis_ids.add(axis.axis_id)
        if axis.axis_kind == "ITEM" and axis.position is not None:
            errors.append("ITEM axis must not have a position")
        if axis.axis_kind == "DIMENSION":
            if not isinstance(axis.position, int) or axis.position < 1:
                errors.append("DIMENSION axis requires a positive position")
            elif axis.position in positions:
                errors.append(f"duplicate dimension position: {axis.position}")
            else:
                positions.add(axis.position)
        value_ids: set[str] = set()
        for value in axis.values:
            value_id = str(value.get("value_id") or "")
            value_name = str(value.get("value_name") or "")
            if not value_id or not value_name:
                errors.append(f"axis {axis.axis_id} has incomplete value metadata")
            if value_id in value_ids:
                errors.append(f"axis {axis.axis_id} has duplicate value_id: {value_id}")
            value_ids.add(value_id)
        if not axis.metadata_complete:
            errors.append(f"axis {axis.axis_id or '(missing)'} metadata is incomplete")
    if positions and positions != set(range(1, max(positions) + 1)):
        errors.append("dimension positions must be contiguous from 1")
    return errors


def legacy_alignment_view(alignment: dict[str, Any]) -> dict[str, Any]:
    """새 R4 두 상태 축을 기존 ``align_status`` 소비자용으로 축약한다.

    `AXIS_AMBIGUOUS`, unit/profile 실패는 v2 어휘로 완전히 표현할 수 없으므로
    ``compatibility_lossy=true``를 반드시 남긴다. 새 산출물의 권위 상태로 역사용하지
    않는다.
    """
    result = dict(alignment)
    resolution = str(result.get("resolution_status") or "")
    cell = str(result.get("cell_status") or "NOT_QUERIED")
    lossy = False
    if cell == "NO_CELL":
        legacy = "NO_CELL"
    elif resolution == "QUERY_READY":
        legacy = "ALIGNED"
    elif resolution in {"ITEM_AMBIGUOUS", "DIM_MISSING", "PERIOD_MISMATCH"}:
        legacy = resolution
    elif resolution == "AXIS_AMBIGUOUS":
        legacy, lossy = "ITEM_AMBIGUOUS", True
    elif resolution in {"UNIT_MISMATCH", "PROFILE_INCOMPLETE"}:
        legacy, lossy = "DIM_MISSING", True
    else:
        legacy = result.get("align_status")
    result["align_status"] = legacy
    result["compatibility_lossy"] = lossy
    return result


def is_query_ready(alignment: dict[str, Any]) -> bool:
    """새 계약을 우선하고, resolution_status가 없는 legacy 산출물만 호환한다."""
    if alignment.get("resolution_status") is not None:
        return alignment.get("resolution_status") == "QUERY_READY"
    return alignment.get("align_status") == "ALIGNED"


def normalized(value: object) -> str:
    return "".join(str(value or "").lower().split())


def exact_matches(rows: list[dict[str, Any]], field: str, term: str) -> list[dict[str, Any]]:
    target = normalized(term)
    return [row for row in rows if normalized(row.get(field)) == target]


def item_matches(items: list[dict[str, Any]], term: str) -> tuple[list[dict[str, Any]], str | None]:
    """항목명은 완전 일치가 원칙이며, 문장 종결 조사만 제한적으로 제거한다.

    복합어의 일부 일치(예: ``보험료``와 ``자동차보험료``)나 약어 확장은 서로 다른
    KOSIS 항목을 같은 것으로 볼 위험이 있어 허용하지 않는다. 이 함수가 반환하는
    ``item_match_basis``는 이후 감사 기록에서 자동 정렬의 근거가 된다.
    """
    direct = exact_matches(items, "itm_nm", term)
    if len(direct) == 1:
        return direct, "normalized_exact"
    if len(direct) > 1:
        return direct, None
    target = normalized(term)
    for particle in ITEM_TERMINAL_PARTICLES:
        if not target.endswith(particle) or len(target) <= len(particle) + 1:
            continue
        stripped = target[:-len(particle)]
        matches = [item for item in items if normalized(item.get("itm_nm")) == stripped]
        if len(matches) == 1:
            return matches, f"terminal_particle_stripped:{particle}"
    return [], None


def period_code(period_type: object) -> str | None:
    """KOSIS/claim의 동의 기간 단위를 비교 가능한 코드로 바꾼다."""
    return PERIOD_CODES.get(normalized(period_type))


def default_dimension_value(values: list[dict[str, Any]]) -> dict[str, Any] | None:
    """조건이 기사에 없을 때도 안전한 단일값 또는 총계 계열만 자동 선택한다."""
    if len(values) == 1:
        return values[0]
    total_values = [value for value in values if normalized(value.get("value_name")) in TOTAL_VALUE_NAMES]
    return total_values[0] if len(total_values) == 1 else None


def normalize_period_for_api(period: str, period_type: str) -> str | None:
    """Canonical 연·월·분기를 KOSIS ``startPrdDe`` 형식으로 바꾼다."""
    compact = re.sub(r"[^0-9]", "", str(period or ""))
    code = period_code(period_type)
    if code == "Y" and len(compact) == 4:
        return compact
    if code == "M" and len(compact) == 6:
        return compact
    quarter = re.fullmatch(r"(\d{4})-?Q([1-4])", str(period or "").strip(), re.IGNORECASE)
    if code == "Q" and quarter:
        return "".join(quarter.groups())
    return None


def period_type_from_absolute(period: object) -> str | None:
    """Infer only the unambiguous canonical unit emitted by the R1 contract."""
    value = str(period or "").strip()
    if re.fullmatch(r"\d{4}", value):
        return "Y"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return "M"
    if re.fullmatch(r"\d{4}-Q[1-4]", value, re.IGNORECASE):
        return "Q"
    return None


def align_profile(
    profile: dict[str, Any], *, item_term: str, dimension_terms: dict[str, str] | None = None,
    period: str | None = None, period_type: str | None = None,
) -> dict[str, Any]:
    """표 하나 안에서 항목·차원값·기간을 모두 코드로 정렬하거나 중단 사유를 돌려준다."""
    items = profile.get("items") if isinstance(profile.get("items"), list) else []
    matched_items, item_match_basis = item_matches(items, item_term)
    if len(matched_items) != 1:
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
                return {"align_status": "DIM_MISSING", "matched_item_id": matched_items[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                        "item_match_basis": item_match_basis, "reason": f"dimension_value_exact_match_required:{obj_name}"}
            matched_dimensions[obj_id] = str(matches[0].get("value_id") or "")
            continue
        default = default_dimension_value(values)
        if default is None:
            return {"align_status": "DIM_MISSING", "matched_item_id": matched_items[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                    "item_match_basis": item_match_basis, "reason": f"dimension_condition_missing:{obj_name}"}
        matched_dimensions[obj_id] = str(default.get("value_id") or "")
        defaults[obj_id] = str(default.get("value_name") or "")
    profile_period_codes = {code for value in profile.get("period_types", []) if (code := period_code(value))}
    if period is not None:
        if not period_type or (claim_period_code := period_code(period_type)) not in profile_period_codes:
            return {"align_status": "PERIOD_MISMATCH", "matched_item_id": matched_items[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                    "item_match_basis": item_match_basis, "reason": "period_type_not_available"}
        api_period = normalize_period_for_api(period, period_type)
        if api_period is None:
            return {"align_status": "PERIOD_MISMATCH", "matched_item_id": matched_items[0].get("itm_id"), "matched_dimensions": matched_dimensions,
                    "item_match_basis": item_match_basis, "reason": "period_format_not_supported"}
    else:
        api_period = None
    return {
        "align_status": "ALIGNED", "matched_item_id": str(matched_items[0].get("itm_id") or ""),
        "matched_dimensions": matched_dimensions, "defaulted_dimensions": defaults,
        "matched_period": api_period, "period_type": period_type,
        "item_match_basis": item_match_basis,
    }


def build_cell_query(profile: dict[str, Any], alignment: dict[str, Any]) -> dict[str, Any]:
    """ALIGNED 결과만 KOSIS statisticsData 표 선택 방식의 안전한 요청 조건으로 바꾼다."""
    if not is_query_ready(alignment):
        raise ValueError("cell query requires resolution_status=QUERY_READY")
    period_type = str(alignment.get("period_type") or "")
    prd_se = period_code(period_type)
    if not prd_se or not alignment.get("matched_period"):
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
        "itm_id": str(alignment["matched_item_id"]), "prd_se": prd_se,
        "start_prd_de": str(alignment["matched_period"]), "end_prd_de": str(alignment["matched_period"]),
        "obj_levels": levels,
    }


def build_probe_alignment(profile: dict[str, Any]) -> dict[str, Any]:
    """profile의 첫 항목과 최신 월·연 period를 써 셀 API 계약을 확인하는 보수적 probe 조건을 만든다."""
    items = profile.get("items") if isinstance(profile.get("items"), list) else []
    if not items or not str(items[0].get("itm_nm") or ""):
        return {"align_status": "ITEM_AMBIGUOUS", "reason": "probe_item_missing"}
    periods = profile.get("periods") if isinstance(profile.get("periods"), list) else []
    supported = [period for period in periods if isinstance(period, dict) and period_code(period.get("PRD_SE"))]
    if not supported:
        return {"align_status": "PERIOD_MISMATCH", "reason": "probe_supported_period_missing"}
    chosen = max(supported, key=lambda period: str(period.get("END_PRD_DE") or ""))
    return align_profile(
        profile, item_term=str(items[0]["itm_nm"]), period=str(chosen.get("END_PRD_DE") or ""), period_type=str(chosen.get("PRD_SE") or ""),
    )
