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

# The projection helpers under test do not require dataframe execution, but
# the legacy sentence-span compatibility import loads pandas at module load.
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas

from src.develop.role_aware_dimension_shadow_v1 import infer_profile_units
from src.news_verification.runtime.r4c1_binding_proposer_v1 import (
    TERMINOLOGY_REGISTRY_VERSION,
    propose_semantic_alias_matches,
)
from src.news_verification.runtime.r4c1_claim_core_v2 import build_claim_core_v2
from src.news_verification.runtime.r4c1_projection_v2 import project_candidate_v2


def _profile(item: dict) -> dict:
    return {
        "table_key": "org:test",
        "items": [item],
        "dimensions": [
            {
                "obj_id": "REGION",
                "obj_nm": "행정구역별",
                "values": [{"value_id": "00", "value_name": "전국"}],
            }
        ],
        "periods": [{"PRD_SE": "Y", "STRT_PRD_DE": "1990", "END_PRD_DE": "2025"}],
    }


def _claim(indicator: str, value: str, unit: str) -> dict:
    sentence = f"{indicator}는 2025년 {value}{unit}이다."
    return {
        "article_idx": "article:test",
        "article_sentence_id": 0,
        "sentence_text": sentence,
        "value_text": value,
        "value_unit": unit,
        "indicator_label": indicator,
        "period_raw": "2025년",
        "retrieval_fields": {
            "indicator": indicator,
            "measurement_type": "LEVEL",
            "period_absolute": "2025",
        },
    }


def test_birth_count_canonical_alias_is_versioned_and_projects_to_claim_wording():
    proposals = propose_semantic_alias_matches("출생아 수", "출생건수 (명)")

    assert TERMINOLOGY_REGISTRY_VERSION == 1
    assert any(
        proposal.rule_id == "ko-stat-birth-count-common-name"
        and proposal.rule_version == TERMINOLOGY_REGISTRY_VERSION
        for proposal in proposals
    )

    core = build_claim_core_v2(_claim("출생아 수", "238,317", "명"))
    projection = project_candidate_v2(
        core,
        infer_profile_units(
            _profile({"itm_id": "birth-count", "itm_nm": "출생건수 (명)", "unit_nm": ""})
        ),
        allow_unqualified_nationwide=True,
    )

    assert projection.projection_status == "PROJECTED"
    assert projection.assignments
    item_binding = next(binding for binding in projection.bindings if binding.axis_kind == "ITEM")
    assert item_binding.evidence["match_rule"] == "ko-stat-birth-count-common-name"


def test_tfr_canonical_item_infers_unit_with_receipt_and_projects():
    profile = infer_profile_units(
        _profile({"itm_id": "tfr", "itm_nm": "합계 출산율", "unit_nm": ""})
    )
    item = profile["items"][0]

    assert item["unit_nm"] == "명"
    assert item["unit_inference"] == {
        "rule_id": "canonical-item-tfr-unit",
        "rule_version": 1,
        "source_label": "합계 출산율",
    }

    core = build_claim_core_v2(_claim("합계출산율", "0.799", "명"))
    projection = project_candidate_v2(core, profile, allow_unqualified_nationwide=True)

    assert projection.projection_status == "PROJECTED"
    unit_binding = next(binding for binding in projection.bindings if binding.axis_kind == "UNIT")
    assert unit_binding.axis_id == "LABEL:명"
    assert unit_binding.evidence["profile_label"] == "명"


def test_nonblank_profile_unit_is_not_overwritten_by_canonical_rule():
    profile = infer_profile_units(
        _profile({"itm_id": "tfr", "itm_nm": "합계출산율", "unit_nm": "%"})
    )

    assert profile["items"][0]["unit_nm"] == "%"
    assert "unit_inference" not in profile["items"][0]


def test_annual_change_projection_uses_subject_context_for_unqualified_nationwide():
    sentence = "지난해 출생아 수 증가율은 2025년 6.7%였다."
    period_start = sentence.index("2025")
    core = {
        "atoms": {
            "indicator": {
                "surface": "출생아 수 증가율",
                "status": "EXPLICIT",
                # Simulates the observed routed core: the sentence is
                # available, but the whole indicator span is not.
                "provenance": {
                    "article_idx": "article:test",
                    "sentence_id": 0,
                    "sentence_text": sentence,
                },
            },
            "unit": {"surface": "", "status": "UNKNOWN", "provenance": {}},
            "period": {
                "surface": "2025",
                "status": "EXPLICIT",
                "provenance": {
                    "article_idx": "article:test",
                    "sentence_id": 0,
                    "start": period_start,
                    "end": period_start + 4,
                    "text": "2025",
                    "sentence_text": sentence,
                    "derivation_input": {"period_raw": "2025년", "period_absolute": "2025"},
                },
            },
            "population": {"surface": (), "status": "UNKNOWN", "provenance": {}},
            "region": {"surface": "", "status": "UNKNOWN", "provenance": {}},
        }
    }
    profile = infer_profile_units(
        _profile({"itm_id": "birth-count", "itm_nm": "출생건수 (명)", "unit_nm": ""})
    )

    projection = project_candidate_v2(core, profile, allow_unqualified_nationwide=True)

    assert projection.projection_status == "PROJECTED"
    assert projection.assignments
    assert "SPAN_REUSE" not in projection.hold_reasons
    assert any(
        binding.binding_basis == "DISCLOSED_NATIONWIDE_DEFAULT"
        for binding in projection.bindings
    )
