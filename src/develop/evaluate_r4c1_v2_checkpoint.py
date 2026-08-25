"""Read-only checkpoint measurements for the R4-C1 v2 contracts.

This evaluator is deliberately outside the projection runtime.  It may read
frozen comparison artifacts, but it never changes them, calls an API, or writes
an output file.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from src.develop.r4c1_claim_core_v2 import build_claim_core_v2
from src.develop.r4c1_article_context import DEFAULT_ARTICLE_SOURCE, with_article_date_context
from src.develop.r4_gold_query_builder import to_kosis_period
from src.develop.r4c1_projection_v2 import (
    CONTRACT_VERSION as PROJECTION_CONTRACT_VERSION,
    _norm,
    project_candidate_v2,
    validate_target_v2,
)


CONTRACT_VERSION = "r4c1-v2-checkpoint-evaluator-v1"
API_CALLS = 0
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATHS = {
    "routed_frozen": ROOT / "data/develop/r17_routing_gold_20260731/dev12_routed.jsonl",
    "routed_live": ROOT / "data/develop/e2e_pipeline_live_front_baseline_20260818/routed.jsonl",
    "r4_frame": ROOT / "data/develop/r4_cell_alignment_20260805/r4b_cell_gold_frame_v2_20260809.jsonl",
    "r4_gold": ROOT / "data/develop/r4_cell_alignment_20260805/r4b_cell_gold_FROZEN_20260809.jsonl",
    "b1_final": ROOT / "data/develop/e2e_pipeline_live_front_baseline_20260818/retrieval_final_output.jsonl",
    "profiles": ROOT / "data/develop/b1_candidate_profiles_20260818/profiles.jsonl",
}


class CheckpointInputError(ValueError):
    """Raised for duplicate or missing checkpoint identities."""


def _jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CheckpointInputError(f"{path}:{line_number}: object required")
            rows.append(value)
    return rows


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _sha256_runtime() -> str:
    digest = hashlib.sha256()
    for name in ("r4c1_claim_core_v2.py", "r4c1_projection_v2.py"):
        path = Path(__file__).with_name(name)
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def target_id_from_routed(row: Mapping[str, Any]) -> str:
    if row.get("target_id"):
        return str(row["target_id"])
    article = row.get("article_idx")
    sentence = row.get("article_sentence_id")
    span = row.get("value_span_id")
    if article in (None, "") or sentence in (None, "") or not span:
        raise CheckpointInputError("routed row has no reproducible target identity")
    sentence_text = str(sentence)
    if not sentence_text.startswith("s"):
        sentence_text = "s" + sentence_text
    span_text = str(span)
    if span_text.startswith(sentence_text + ":"):
        span_text = span_text[len(sentence_text) + 1:]
    return f"dev:{article}:{sentence_text}:{span_text}"


def _index(rows: Iterable[Mapping[str, Any]], *, identity: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get(identity) or "")
        if not key:
            raise CheckpointInputError(f"missing {identity}")
        if key in result:
            raise CheckpointInputError(f"duplicate {identity}: {key}")
        result[key] = dict(row)
    return result


def _routed_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = target_id_from_routed(row)
        if key in result:
            raise CheckpointInputError(f"duplicate routed target_id: {key}")
        result[key] = dict(row)
    return result


def join_target_rows(routed_rows: Sequence[Mapping[str, Any]], frame_rows: Sequence[Mapping[str, Any]], gold_rows: Sequence[Mapping[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    routed = _routed_index(routed_rows)
    frame = _index(frame_rows, identity="target_id")
    gold = _index(gold_rows, identity="target_id")
    if set(frame) != set(gold) or set(frame) != set(routed).intersection(frame):
        missing_routed = sorted(set(frame) - set(routed))
        missing_gold = sorted(set(frame) - set(gold))
        raise CheckpointInputError(f"target join mismatch: frame={len(frame)} missing_routed={missing_routed[:3]} missing_gold={missing_gold[:3]}")
    return [(routed[key], frame[key], gold[key]) for key in sorted(frame)]


def frame_to_profile(frame: Mapping[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    for axis in frame.get("axes") or []:
        if not isinstance(axis, Mapping):
            continue
        kind = str(axis.get("axis_kind") or "")
        values = axis.get("values") or []
        if kind == "ITEM":
            for value in values:
                items.append({
                    "itm_id": value.get("value_id"),
                    "itm_nm": value.get("value_name"),
                    "unit_nm": value.get("unit_name", value.get("unit_nm", "")),
                })
        elif kind == "DIMENSION":
            dimensions.append({
                "obj_id": axis.get("axis_id"),
                "obj_nm": axis.get("axis_name"),
                "values": [
                    {"value_id": value.get("value_id"), "value_name": value.get("value_name")}
                    for value in values
                    if isinstance(value, Mapping)
                ],
            })
    table_key = str(frame.get("table_key") or "")
    return {
        "table_key": table_key,
        "items": items,
        "dimensions": dimensions,
        "periods": list(frame.get("periods") or []),
    }


def _period_frequency(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if text in {"y", "year", "annual", "annually", "연", "년", "연간"}:
        return "Y"
    if text in {"m", "month", "monthly", "월", "월간"}:
        return "M"
    if text in {"q", "quarter", "quarterly", "분기", "분기별"}:
        return "Q"
    return None


def _legacy_expected_parse_period(value: Any) -> tuple[str, str, str] | None:
    """Frozen expected-side parser used by the historical exact scorer.

    This intentionally does not call the runtime projection parser.  In
    particular, the legacy scorer's display-month grammar did not include the
    later ``YYYY.MM`` extension; the independent canonical diagnostic below
    does.  Keeping this function local prevents a runtime parser change from
    changing the expected side of the exact comparison.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    year = re.fullmatch(r"(\d{4})년?", raw)
    if year:
        return "Y", year.group(1), year.group(1)
    quarter = re.fullmatch(r"(\d{4})\s*([1-4])\s*/\s*4", raw)
    if quarter:
        api = f"{quarter.group(1)}0{quarter.group(2)}"
        return "Q", api, api
    # Keep the old separator grammar, excluding the new dot-month surface.
    month = re.fullmatch(r"(\d{4})\s*[-/]\s*(0?[1-9]|1[0-2])", raw)
    if month:
        api = f"{month.group(1)}{int(month.group(2)):02d}"
        return "M", api, api
    return None


def _gold_plan(gold: Mapping[str, Any]) -> dict[str, Any] | None:
    table = str(gold.get("table_key") or "")
    if table.count(":") != 1:
        return None
    org_id, tbl_id = table.split(":", 1)
    item = gold.get("item_selection") or {}
    dimensions = gold.get("dimension_selections") or {}
    period_plan = gold.get("period_plan") or {}
    operands = period_plan.get("operands") or []
    if not org_id or not tbl_id or not item.get("itm_id") or not operands:
        return None
    period = operands[0].get("period") if isinstance(operands[0], Mapping) else None
    parsed = _legacy_expected_parse_period(period)
    frequency = _period_frequency(period_plan.get("prd_se")) or (parsed[0] if parsed else None)
    api_period = parsed[1] if parsed else None
    if frequency is None or api_period is None:
        return None
    return {
        "org_id": org_id,
        "tbl_id": tbl_id,
        "itm_id": str(item["itm_id"]),
        "prd_se": frequency,
        "start_prd_de": api_period,
        "end_prd_de": api_period,
        "obj_levels": {f"objL{index}": str(selection.get("value_id")) for index, selection in enumerate(dimensions.values(), 1) if isinstance(selection, Mapping) and selection.get("value_id")},
    }


def _independent_canonical_gold_plan(gold: Mapping[str, Any], frame: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the canonical gold plan without importing runtime period code.

    This is diagnostic-only.  It mirrors the frozen R4 gold query contract and
    is deliberately never used as the expected side of ``exact_query_plan``.
    """
    table = str(gold.get("table_key") or "")
    if table.count(":") != 1:
        return None
    org_id, tbl_id = table.split(":", 1)
    item = gold.get("item_selection") or {}
    period_plan = gold.get("period_plan") or {}
    operands = period_plan.get("operands") or []
    if not org_id or not tbl_id or not item.get("itm_id") or not operands:
        return None
    period = operands[0].get("period") if isinstance(operands[0], Mapping) else None
    frequency = _period_frequency(period_plan.get("prd_se"))
    if frequency is None:
        return None
    try:
        api_period = to_kosis_period(str(period or ""), frequency)
    except (TypeError, ValueError):
        return None
    axes = [axis for axis in frame.get("axes") or [] if isinstance(axis, Mapping) and axis.get("axis_kind") == "DIMENSION"]
    selections = gold.get("dimension_selections") or {}
    obj_levels: dict[str, str] = {}
    for index, axis in enumerate(axes, 1):
        axis_id = str(axis.get("axis_id") or "")
        selection = selections.get(axis_id)
        if not isinstance(selection, Mapping) or not selection.get("value_id"):
            return None
        obj_levels[f"objL{index}"] = str(selection["value_id"])
    if not obj_levels:
        return None
    return {
        "org_id": org_id,
        "tbl_id": tbl_id,
        "itm_id": str(item["itm_id"]),
        "prd_se": frequency,
        "start_prd_de": api_period,
        "end_prd_de": api_period,
        "obj_levels": obj_levels,
    }


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


def validate_query_plan_inventory(plan: Mapping[str, Any], profile: Mapping[str, Any], claim_core: Any | None = None) -> list[str]:
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
        period_rows = [period for period in profile.get("periods") or [] if isinstance(period, Mapping) and _period_frequency(period.get("PRD_SE")) == frequency]
        requested = _profile_period_key(plan["start_prd_de"], frequency)
        if requested is None or not any((lo := _profile_period_key(period.get("STRT_PRD_DE"), frequency)) is not None and (hi := _profile_period_key(period.get("END_PRD_DE"), frequency)) is not None and lo <= requested <= hi for period in period_rows):
            errors.append("PERIOD_RANGE")
    dimensions = [dimension for dimension in profile.get("dimensions") or [] if isinstance(dimension, Mapping)]
    expected_levels = {f"objL{index}" for index in range(1, len(dimensions) + 1)}
    if set(plan["obj_levels"]) != expected_levels:
        errors.append("OBJ_LEVEL_KEYS")
    selected_dimension_values: list[Mapping[str, Any]] = []
    for index, dimension in enumerate(dimensions, 1):
        values = [value for value in dimension.get("values") or [] if isinstance(value, Mapping)]
        selected_value = next(
            (
                value
                for value in values
                if str(value.get("value_id"))
                == str(plan["obj_levels"].get(f"objL{index}"))
            ),
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
                if str(surface).strip() not in krw_units or not all(
                    unit in krw_units for unit in selected_units
                ):
                    errors.append("UNIT_MISMATCH")
    return errors


def _routed_field_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    """Record raw input paths independently; never coalesce frozen/live rows."""
    fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
    return {
        "period_raw": {"path": "period_raw", "value": row.get("period_raw")},
        "period_absolute": {"path": "retrieval_fields.period_absolute", "value": fields.get("period_absolute")},
        "indicator": {"path": "retrieval_fields.indicator", "value": fields.get("indicator")},
        "unit": {"path": "value_unit", "value": row.get("value_unit")},
        "population": {"path": "retrieval_fields.population", "value": fields.get("population")},
    }


def _claim_core_consumption(core: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    atoms = getattr(core, "atoms", {})
    for role in ("indicator", "period", "unit", "population"):
        atom = atoms.get(role) if isinstance(atoms, Mapping) else None
        if atom is None:
            continue
        surface = getattr(atom, "surface", None)
        provenance = getattr(atom, "provenance", {})
        provenance = provenance if isinstance(provenance, Mapping) else {}
        derivation = provenance.get("derivation_input")
        result[role] = {
            "structured_surface": surface,
            "span_source_path": provenance.get("span_path"),
            "span_start": provenance.get("start"),
            "span_end": provenance.get("end"),
            "span_text": provenance.get("text"),
            "derivation_input": dict(derivation) if isinstance(derivation, Mapping) else None,
        }
    return result


def _candidate_diagnostic(table_key: str, profile: Mapping[str, Any] | None, projection: Any) -> dict[str, Any]:
    return {
        "table_key": table_key,
        "profile_available": profile is not None,
        "projection_status": projection.projection_status,
        "assignment_count": len(projection.assignments),
        "abstained": [list(value) for value in projection.abstained],
        "hold_reasons": list(projection.hold_reasons),
    }


def evaluate_synthetic() -> dict[str, Any]:
    row = {
        "article_idx": 1,
        "article_sentence_id": "s1",
        "sentence_text": "취득세 징수액은 2024년 1원이다.",
        "retrieval_fields": {"indicator": "취득세 징수액", "period_absolute": "2024"},
        "value_text": "1",
        "value_unit": "원",
    }
    profile_base = {
        "table_key": "ORG:T",
        "periods": [{"PRD_SE": "Y", "STRT_PRD_DE": "2020", "END_PRD_DE": "2025"}],
    }
    correct = {**profile_base, "items": [{"itm_id": "I_ITEM", "itm_nm": "징수액", "unit_nm": "원"}], "dimensions": [{"obj_id": "O_DIM", "obj_nm": "세목", "values": [{"value_id": "V_TAX", "value_name": "취득세"}]}]}
    switched = {**profile_base, "items": [{"itm_id": "I_TAX", "itm_nm": "취득세", "unit_nm": "원"}], "dimensions": [{"obj_id": "O_DIM", "obj_nm": "세목", "values": [{"value_id": "V_ITEM", "value_name": "징수액"}]}]}
    same_span = {**profile_base, "items": [{"itm_id": "I_TAX", "itm_nm": "취득세", "unit_nm": "원"}], "dimensions": [{"obj_id": "O_DIM", "obj_nm": "세목", "values": [{"value_id": "V_TAX", "value_name": "취득세"}]}]}
    core = build_claim_core_v2(row)
    first, second = project_candidate_v2(core, correct), project_candidate_v2(core, switched)
    target = validate_target_v2([first, second])
    same = validate_target_v2([project_candidate_v2(core, same_span)])
    return {
        "role_switch": {"candidate_assignments": [len(first.assignments), len(second.assignments)], "outcome": target.outcome, "reason": target.hold_reason, "denominator": 1, "false_ready": int(target.outcome == "QUERY_READY")},
        "same_span_decoy": {"outcome": same.outcome, "reason": same.hold_reason, "denominator": 1, "false_ready": int(same.outcome == "QUERY_READY")},
    }


def evaluate_r4(routed_path: str | Path, frame_path: str | Path, gold_path: str | Path, routed_live_path: str | Path | None = None) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any] | None]]]:
    frozen_rows = _jsonl(routed_path)
    joined = join_target_rows(frozen_rows, _jsonl(frame_path), _jsonl(gold_path))
    live_by_target = _routed_index(_jsonl(routed_live_path)) if routed_live_path is not None else {}
    unsafe = 0
    exact = 0
    legacy_buildable = 0
    canonical_buildable = 0
    predicted_counter: Counter[str] = Counter()
    inventory_rows: list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any] | None]] = []
    direct_input_contract: dict[str, Any] = {}
    for routed, frame, gold in joined:
        core = build_claim_core_v2(routed)
        profile = frame_to_profile(frame)
        projection = project_candidate_v2(core, profile)
        resolution = validate_target_v2([projection])
        predicted_counter[resolution.outcome if resolution.outcome == "QUERY_READY" else str(resolution.hold_reason)] += 1
        gold_status = str(gold.get("resolution_status") or "")
        unsafe += int(resolution.outcome == "QUERY_READY" and gold_status != "QUERY_READY")
        expected = _gold_plan(gold) if gold_status == "QUERY_READY" else None
        if expected is not None and resolution.query_plan == expected:
            exact += 1
        if gold_status == "QUERY_READY":
            legacy_buildable += int(expected is not None)
            canonical_buildable += int(_independent_canonical_gold_plan(gold, frame) is not None)
        target_id = str(gold.get("target_id") or routed.get("target_id") or target_id_from_routed(routed))
        live = live_by_target.get(target_id)
        direct_input_contract[target_id] = {
            "frozen": _routed_field_observation(routed),
            "live": _routed_field_observation(live) if live is not None else {"available": False},
            "claim_core_consumption": _claim_core_consumption(core),
        }
        if resolution.query_plan is not None:
            inventory_rows.append((routed, profile, core, resolution.query_plan))
    denominator = len(joined)
    gold_qr = sum(str(gold.get("resolution_status") or "") == "QUERY_READY" for _, _, gold in joined)
    metric_contracts = {
        "exact_query_plan": {"numerator": exact, "denominator": gold_qr},
        "gold_query_ready_denominator": gold_qr,
        "baseline_reference": {
            "namespace": "r4c0_oracle_table_item_component_upper_bound",
            "numerator": 5,
            "denominator": 12,
        },
    }
    return ({
        "targets": denominator,
        "predicted_distribution": dict(sorted(predicted_counter.items())),
        "unsafe_ready": {"numerator": unsafe, "denominator": denominator},
        "exact_query_plan": {"numerator": exact, "denominator": gold_qr},
        "gold_query_ready_denominator": gold_qr,
        "baseline_reference": {"numerator": 5, "denominator": 12},
        "metric_contracts": metric_contracts,
        "legacy_expected_buildability": {"numerator": legacy_buildable, "denominator": gold_qr},
        "independent_canonical_gold_buildability": {"numerator": canonical_buildable, "denominator": gold_qr},
        "direct_input_contract": direct_input_contract,
    }, inventory_rows)


def evaluate_b1(routed_path: str | Path, final_path: str | Path, profiles_path: str | Path, article_source_path: str | Path | None = DEFAULT_ARTICLE_SOURCE) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any] | None]]]:
    routed = _routed_index(with_article_date_context(_jsonl(routed_path), article_source_path))
    profile_rows = _jsonl(profiles_path)
    profiles = _index(profile_rows, identity="table_key")
    final_rows = _jsonl(final_path)
    target_rows = [row for row in final_rows if isinstance(row.get("retrieval"), Mapping) and row["retrieval"].get("status") == "CANDIDATES_FOUND"]
    target_counter: Counter[str] = Counter()
    candidate_counter: Counter[str] = Counter()
    inventory_rows: list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any] | None]] = []
    target_reason_union: dict[str, dict[str, Any]] = {}
    target_diagnostics: dict[str, list[dict[str, Any]]] = {}
    available_target_ids: set[str] = set()
    available_item_and_dimension_targets: set[str] = set()
    item_option_count = 0
    all_dimension_option_count = 0
    for final in target_rows:
        target_id = str(final.get("target_id") or "")
        if target_id not in routed:
            raise CheckpointInputError(f"B1 target missing from routed: {target_id}")
        core = build_claim_core_v2(routed[target_id])
        projections = []
        candidate_diagnostics: list[dict[str, Any]] = []
        target_reasons: set[str] = set()
        target_has_available = False
        target_has_item = False
        target_has_all_dimensions = False
        target_has_item_and_all_dimensions = False
        for candidate in final["retrieval"].get("candidates") or []:
            table_key = str(candidate.get("table_key") or "")
            profile = profiles.get(table_key)
            projection = project_candidate_v2(core, profile)
            projections.append(projection)
            candidate_diagnostics.append(_candidate_diagnostic(table_key, profile, projection))
            target_reasons.update(projection.hold_reasons)
            if profile is not None:
                target_has_available = True
                incomplete = "PROFILE_INCOMPLETE" in projection.hold_reasons
                item_ok = not incomplete and not any(kind == "ITEM" for kind, _ in projection.abstained)
                dimension_ok = not incomplete and bool(profile.get("dimensions")) and not any(kind == "DIMENSION" for kind, _ in projection.abstained)
                item_option_count += int(item_ok)
                all_dimension_option_count += int(dimension_ok)
                target_has_item = target_has_item or item_ok
                target_has_all_dimensions = target_has_all_dimensions or dimension_ok
                if item_ok and dimension_ok:
                    target_has_item_and_all_dimensions = True
            candidate_counter[projection.projection_status] += 1
            for reason in projection.hold_reasons:
                candidate_counter[f"HOLD:{reason}"] += 1
        resolution = validate_target_v2(projections)
        target_counter[resolution.outcome if resolution.outcome == "QUERY_READY" else str(resolution.hold_reason)] += 1
        if target_has_available:
            available_target_ids.add(target_id)
        if target_has_item_and_all_dimensions:
            available_item_and_dimension_targets.add(target_id)
        target_reason_union[target_id] = {
            "final_reason": resolution.hold_reason,
            "outcome": resolution.outcome,
            "candidate_reason_union": sorted(target_reasons),
        }
        target_diagnostics[target_id] = sorted(candidate_diagnostics, key=lambda row: (row["table_key"], row["projection_status"], tuple(row["hold_reasons"])))
        if resolution.query_plan is not None:
            inventory_rows.append((routed[target_id], profiles[resolution.chosen_table_key], core, resolution.query_plan))
    available = sum(1 for final in target_rows for candidate in final["retrieval"].get("candidates") or [] if str(candidate.get("table_key") or "") in profiles)
    total_candidates = sum(len(final["retrieval"].get("candidates") or []) for final in target_rows)
    target_count = len(target_rows)
    return ({
        "targets": target_count,
        "candidates": total_candidates,
        "profile_available": available,
        "profile_unavailable": total_candidates - available,
        "expected_drift_report": {"targets": 84, "candidates": 420, "profile_available": 372, "profile_unavailable": 48},
        "target_distribution": dict(sorted(target_counter.items())),
        "candidate_distribution": dict(sorted(candidate_counter.items())),
        "target_reason_union": {
            "targets": target_count,
            "reason_counts": dict(sorted(Counter(reason for row in target_reason_union.values() for reason in row["candidate_reason_union"]).items())),
            "by_target": {key: target_reason_union[key] for key in sorted(target_reason_union)},
        },
        "binding_coverage": {
            "profile_available_candidates": {"numerator": available, "denominator": total_candidates},
            "item_option_candidates": {"numerator": item_option_count, "denominator": available},
            "all_dimension_option_candidates": {"numerator": all_dimension_option_count, "denominator": available},
            "item_and_all_dimension_option_targets": {"numerator": len(available_item_and_dimension_targets), "denominator": target_count},
            "available_targets": {"numerator": len(available_target_ids), "denominator": target_count},
        },
        "target_diagnostics": {key: target_diagnostics[key] for key in sorted(target_diagnostics)},
    }, inventory_rows)


def evaluate_checkpoint(paths: Mapping[str, str | Path] | None = None) -> dict[str, Any]:
    selected = {**DEFAULT_PATHS, **(dict(paths) if paths else {})}
    input_sha = {name: _sha256_file(path) for name, path in selected.items()}
    report: dict[str, Any] = {"contract_version": CONTRACT_VERSION, "projection_contract_version": PROJECTION_CONTRACT_VERSION, "api_calls": API_CALLS, "input_sha256": input_sha, "runtime_code_sha256": _sha256_runtime()}
    all_inventory: list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any] | None]] = []
    try:
        report["synthetic"] = evaluate_synthetic()
        report["r4"], r4_inventory = evaluate_r4(selected["routed_frozen"], selected["r4_frame"], selected["r4_gold"], selected.get("routed_live"))
        report["b1"], b1_inventory = evaluate_b1(
            selected["routed_live"],
            selected["b1_final"],
            selected["profiles"],
            selected.get("article_source", DEFAULT_ARTICLE_SOURCE),
        )
        # These aliases make the two contracts discoverable without changing
        # any of the historical nested metric keys.
        report["metric_contracts"] = report["r4"]["metric_contracts"]
        report["legacy_expected_buildability"] = report["r4"]["legacy_expected_buildability"]
        report["independent_canonical_gold_buildability"] = report["r4"]["independent_canonical_gold_buildability"]
        report["direct_input_contract"] = report["r4"]["direct_input_contract"]
        all_inventory.extend(r4_inventory); all_inventory.extend(b1_inventory)
        valid = 0
        for _, profile, core, plan in all_inventory:
            valid += int(not validate_query_plan_inventory(plan, profile, core))
        report["ready_inventory"] = {"valid": valid, "denominator": len(all_inventory)}
        report["errors"] = []
    except (CheckpointInputError, FileNotFoundError, json.JSONDecodeError) as exc:
        report["errors"] = [f"{type(exc).__name__}: {exc}"]
        report["ready_inventory"] = {"valid": 0, "denominator": 0}
    return report


def main() -> None:
    print(json.dumps(evaluate_checkpoint(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
