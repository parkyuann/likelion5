from __future__ import annotations

import sys
import types
from pathlib import Path


RUNTIME_ROOT = Path(__file__).parents[1].resolve() / "deploy" / "pipeline_runtime"
for import_root in (
    RUNTIME_ROOT,
    RUNTIME_ROOT / "src" / "news_verification" / "runtime",
):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas
if "requests" not in sys.modules:
    requests = types.ModuleType("requests")
    requests.RequestException = RuntimeError
    requests.get = lambda *args, **kwargs: None
    requests.post = lambda *args, **kwargs: None
    requests.Session = lambda: None
    sys.modules["requests"] = requests

import src.news_verification.runtime.run_pipeline_operational_v2 as operational
from src.news_verification.runtime.r4c1_projection_v2 import (
    AxisBinding,
    CandidateAssignment,
    CandidateProjection,
    validate_target_v2,
)


def _claim_span(text: str = "출생아 수") -> dict[str, object]:
    return {
        "article_idx": "article:test",
        "sentence_id": 0,
        "start": 0,
        "end": len(text),
        "text": text,
    }


def _assignment(table_key: str, *, specific_item: bool) -> CandidateAssignment:
    item_evidence = {
        "claim_provenance": _claim_span(),
        "profile_inventory_path": "items[0].itm_nm",
        "profile_label": "출생건수 (명)",
        "profile_id": "T10",
        "match_rule": "ko-stat-birth-count-common-name" if specific_item else "SINGLETON_INVENTORY",
        "consumed_span": _claim_span() if specific_item else None,
    }
    period_evidence = {
        "claim_provenance": _claim_span("2025"),
        "profile_inventory_path": "periods[0].PRD_SE",
        "profile_label": "Y",
        "profile_id": "Y",
        "consumed_span": _claim_span("2025"),
    }
    geo_evidence = {
        "claim_provenance": _claim_span(),
        "profile_inventory_path": "dimensions[0].values[0]",
        "profile_label": "전국",
        "profile_id": "00",
        "consumed_span": None,
        "axis_evidence": {
            "profile_inventory_path": "dimensions[0]",
            "profile_label": "행정구역별",
            "profile_id": "REGION",
        },
        "value_evidence": {
            "profile_inventory_path": "dimensions[0].values[0]",
            "profile_label": "전국",
            "profile_id": "00",
        },
        "inference_disclosure": {
            "rule_id": "unqualified-geographic-axis-nationwide",
            "rule_version": 1,
        },
    }
    return CandidateAssignment(
        table_key,
        (
            AxisBinding("ITEM", "T10", None, "indicator", "EXACT_LABEL" if specific_item else "SINGLETON_INVENTORY", item_evidence),
            AxisBinding("PERIOD", "Y", "2025", "period", "PERIOD_RANGE_COMPATIBLE", period_evidence),
            AxisBinding("DIMENSION", "REGION", "00", "inferred_scope", "DISCLOSED_NATIONWIDE_DEFAULT", geo_evidence),
        ),
    )


def _projection(assignment: CandidateAssignment) -> CandidateProjection:
    return CandidateProjection(
        assignment.table_key,
        (assignment,),
        (),
        "PROJECTED",
        (),
        f"sha-{assignment.table_key}",
    )


def _profile(table_key: str, cardinality: int, projection: CandidateProjection) -> dict:
    return {
        "table_key": table_key,
        "dimensions": [{"obj_id": "REGION", "values": [{"value_id": str(i)} for i in range(cardinality)]}],
        "_projection": projection,
    }


def test_release_bound_dominance_prefers_coarser_nationwide_axis():
    coarse = _assignment("org:coarse", specific_item=True)
    fine = _assignment("org:fine", specific_item=True)
    base = validate_target_v2([_projection(coarse), _projection(fine)])

    resolved = operational._apply_release_bound_evidence_specificity_dominance(
        base,
        [_projection(coarse), _projection(fine)],
        {
            "org:coarse": _profile("org:coarse", 19, _projection(coarse)),
            "org:fine": _profile("org:fine", 385, _projection(fine)),
        },
    )

    assert resolved.outcome == "QUERY_READY"
    assert resolved.chosen_table_key == "org:coarse"
    receipt = resolved.audit["release_bound_evidence_specificity_dominance"]
    assert receipt["rule_version"] == 1
    assert receipt["chosen_table"] == "org:coarse"
    assert {row["table_key"] for row in receipt["candidates"]} == {"org:coarse", "org:fine"}
    assert receipt["candidates"][0]["score"]["coarser_geo_cardinality"] in {-19, -385}


def test_release_bound_dominance_prefers_source_backed_item_over_singleton_item():
    specific = _assignment("org:specific", specific_item=True)
    generic = _assignment("org:generic", specific_item=False)
    base = validate_target_v2([_projection(specific), _projection(generic)])

    resolved = operational._apply_release_bound_evidence_specificity_dominance(
        base,
        [_projection(specific), _projection(generic)],
        {
            "org:specific": _profile("org:specific", 19, _projection(specific)),
            "org:generic": _profile("org:generic", 19, _projection(generic)),
        },
    )

    assert resolved.outcome == "QUERY_READY"
    assert resolved.chosen_table_key == "org:specific"


def test_resolve_top50_dominance_is_opt_in(monkeypatch):
    coarse = _assignment("org:coarse", specific_item=True)
    fine = _assignment("org:fine", specific_item=True)
    profiles = {
        "org:coarse": _profile("org:coarse", 19, _projection(coarse)),
        "org:fine": _profile("org:fine", 385, _projection(fine)),
    }
    monkeypatch.setattr(
        operational,
        "project_candidate_v2",
        lambda _core, profile, **_kwargs: profile["_projection"],
    )
    candidates = [{"table_key": "org:coarse"}, {"table_key": "org:fine"}]

    default = operational.resolve_top50({}, candidates, profiles.get)
    opted_in = operational.resolve_top50({}, candidates, profiles.get, release_bound_mode=True)

    assert default.resolution.hold_reason == "MULTIPLE_COMPATIBLE_SERIES"
    assert opted_in.resolution.outcome == "QUERY_READY"
    assert opted_in.resolution.chosen_table_key == "org:coarse"


def test_release_bound_annual_gate_collapses_only_same_span_yoy_duplicate():
    row = {
        "article_idx": "article:test",
        "article_sentence_id": 0,
        "sentence_text": "지난해 출생아 수가 1년 전보다 6.7% 늘었다.",
        "value_text": "6.7",
        "value_unit": "%",
        "indicator_label": "출생아 수 증가율",
        "article_date": "2026-08-26",
        "retrieval_fields": {
            "indicator": "출생아 수 증가율",
            "measurement_type": "CHANGE_RATE",
            "period_absolute": "2025",
            "period": {
                "measurement": {"raw": "지난해", "absolute": "2025"},
                "baseline": {"raw": "", "absolute": ""},
                "basis": "NONE",
            },
        },
    }

    normalized = operational._release_bound_annual_period_row(row)
    period = normalized["retrieval_fields"]["period"]

    assert period["basis"] == "YOY"
    assert period["baseline"]["absolute"] == "2024"
    assert normalized["release_bound_annual_period_receipt"]["rule_version"] == 1
    assert normalized["release_bound_annual_period_receipt"]["source_span"]["text"] == "1년 전보다"
    core = operational._annual_change_projection_core(normalized)
    assert core.atoms["period"].provenance["release_bound_period_basis_receipt"]["selected_basis"] == "YOY"
