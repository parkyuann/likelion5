from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.develop.failure_recovery_shadow_v1 import build_post_binding_clarification_plan


def test_common_missing_region_builds_candidate_scoped_opaque_options():
    projections = (
        SimpleNamespace(
            table_key="101:A", canonical_sha256="a" * 64,
            hold_reasons=("REGION_UNBOUND",),
            slot_diagnostics=({
                "role": "region", "status": "MISSING", "table_key": "101:A",
                "profile_sha256": "b" * 64,
                "option_inventory": [{"label": "전국", "axis_id": "hidden-axis", "value_id": "00"}],
            },),
        ),
        SimpleNamespace(
            table_key="101:B", canonical_sha256="c" * 64,
            hold_reasons=("REGION_UNBOUND",),
            slot_diagnostics=({
                "role": "region", "status": "MISSING", "table_key": "101:B",
                "profile_sha256": "d" * 64,
                "option_inventory": [{"label": "서울특별시", "axis_id": "hidden-axis-2", "value_id": "11"}],
            },),
        ),
    )
    top50 = SimpleNamespace(
        resolution=SimpleNamespace(outcome="HOLD", hold_reason="REGION_UNBOUND"),
        projections=projections, candidate_membership=("101:A", "101:B"),
    )
    top50.pinned_raw_profiles = {
        "101:A": {"table_key": "101:A", "release_id": "release-1", "profile_sha256": "b" * 64},
        "101:B": {"table_key": "101:B", "release_id": "release-1", "profile_sha256": "d" * 64},
    }
    top50.pinned_projection_profiles = {}
    plan = build_post_binding_clarification_plan(top50, target_id="article:target-1")
    assert plan is not None
    question = plan["question"]
    assert question["role"] == "region"
    assert question["id"].startswith("cq-")
    assert {item["label"] for item in question["options"]} == {"전국", "서울특별시"}
    assert all(item["id"].startswith("co-") for item in question["options"])
    assert all("axis_id" not in item and "value_id" not in item for item in question["options"])
    assert all(option["applicability"] for option in plan["option_bundle"]["options"])


def test_profile_incomplete_never_fabricates_options():
    top50 = SimpleNamespace(
        resolution=SimpleNamespace(outcome="HOLD", hold_reason="REGION_UNBOUND"),
        projections=(SimpleNamespace(table_key="101:A", canonical_sha256="a" * 64, hold_reasons=("PROFILE_INCOMPLETE",), slot_diagnostics=()),),
        candidate_membership=("101:A",),
    )
    assert build_post_binding_clarification_plan(top50, target_id="article:target-1") is None
