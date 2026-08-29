from __future__ import annotations

from pathlib import Path
import sys

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.develop.l2_segmentation import resolve_prediction  # noqa: E402
from src.news_verification.runtime.l3_role_assignment import assign_roles  # noqa: E402
from src.news_verification.runtime.l4_field_normalization import compose_all  # noqa: E402
from src.news_verification.runtime.r4c1_claim_core_v2 import build_claim_core_v2  # noqa: E402
from src.news_verification.runtime.r4c1_projection_v2 import (  # noqa: E402
    project_candidate_v2,
    validate_target_v2,
)


ARTICLE = "2025년 대구광역시의 합계출산율은 0.80명이다."


def _profile() -> dict[str, object]:
    return {
        "table_key": "101:DT_TEST",
        "org_id": "101",
        "tbl_id": "DT_TEST",
        "items": [{"itm_id": "TFR", "itm_nm": "합계출산율", "unit_nm": "명"}],
        "dimensions": [{
            "obj_id": "A",
            "obj_nm": "행정구역별",
            "obj_order": 1,
            "values": [
                {"value_id": "00", "value_name": "전국", "unit_nm": ""},
                {"value_id": "22", "value_name": "대구광역시", "unit_nm": ""},
            ],
        }],
        "periods": [{"PRD_SE": "년", "STRT_PRD_DE": "1990", "END_PRD_DE": "2025"}],
    }


def test_empty_hcx_indicator_is_recovered_from_one_exact_registry_surface():
    resolved = resolve_prediction(
        ARTICLE,
        {"sentences": [{
            "sentence_id": 0,
            "indicator_scopes": [],
            "source_region": {},
            "period_context": {"period_raw": "2025년"},
        }]},
    )

    assert resolved["canonical_status"] == "L2_READY"
    scope = resolved["sentences"][0]["indicator_scopes"]
    assert [(item["indicator_label"], item["source_span_text"]) for item in scope] == [
        ("합계출산율", "합계출산율"),
    ]
    receipt = resolved["indicator_evidence_receipts"][0]
    assert receipt["reason_code"] == "EXACT_REGISTRY_SOURCE_RECOVERY"
    assert receipt["decision"] == "RESOLVED"


def test_one_explicit_region_is_carried_to_selector_and_query_plan():
    resolved = resolve_prediction(
        ARTICLE,
        {"sentences": [{
            "sentence_id": 0,
            "indicator_scopes": [],
            "source_region": {},
            "period_context": {"period_raw": "2025년"},
        }]},
    )
    l2_row = {"article_idx": "article-1", **resolved["sentences"][0]}
    assignments = assign_roles(ARTICLE, [l2_row])
    assert len(assignments) == 1
    assert assignments[0]["region_evidence"]["surface"] == "대구광역시"
    assert assignments[0]["region_evidence"]["evidence_basis"] == "ARTICLE_EXACT_DIMENSION"

    routed = compose_all(
        [{"article_idx": "article-1", **assignments[0]}],
        {"article-1": "2026-08-29"},
    )[0]
    assert routed["retrieval_fields"]["dimension"] == ["대구광역시"]
    core = build_claim_core_v2(routed)
    assert core.atoms["region"].status == "EXPLICIT"
    assert core.atoms["region"].surface == "대구광역시"

    projection = project_candidate_v2(core, _profile(), allow_unqualified_nationwide=True)
    resolution = validate_target_v2([projection])
    assert resolution.outcome == "QUERY_READY"
    assert resolution.query_plan == {
        "org_id": "101",
        "tbl_id": "DT_TEST",
        "itm_id": "TFR",
        "prd_se": "Y",
        "start_prd_de": "2025",
        "end_prd_de": "2025",
        "obj_levels": {"objL1": "22"},
    }


def test_multiple_explicit_regions_do_not_fall_back_to_nationwide():
    article = "2025년 대구광역시와 부산광역시의 합계출산율은 0.80명이다."
    resolved = resolve_prediction(
        article,
        {"sentences": [{
            "sentence_id": 0,
            "indicator_scopes": [],
            "source_region": {},
            "period_context": {"period_raw": "2025년"},
        }]},
    )
    l2_row = {"article_idx": "article-2", **resolved["sentences"][0]}
    assignments = assign_roles(article, [l2_row])
    routed = compose_all(
        [{"article_idx": "article-2", **assignments[0]}],
        {"article-2": "2026-08-29"},
    )[0]
    core = build_claim_core_v2(routed)
    assert core.atoms["region"].status == "AMBIGUOUS"
    projection = project_candidate_v2(core, _profile(), allow_unqualified_nationwide=True)
    resolution = validate_target_v2([projection])
    assert resolution.outcome != "QUERY_READY"
