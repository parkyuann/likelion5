from __future__ import annotations

import sys
from pathlib import Path

import pytest


RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.news_verification.runtime.audit_budget_v1 import BudgetExhausted, HttpAttemptBudgetLedger
from src.news_verification.runtime.run_pipeline_operational_v2 import _cell_budget_target_id


def _plan(**overrides):
    plan = {
        "org_id": "org",
        "tbl_id": "table",
        "itm_id": "item",
        "prd_se": "Y",
        "start_prd_de": "2025",
        "end_prd_de": "2025",
        "obj_levels": {"objL2": "00", "objL1": "00"},
    }
    plan.update(overrides)
    return plan


def _ledger(tmp_path):
    return HttpAttemptBudgetLedger(
        tmp_path / "budget.sqlite",
        limits={"cell": 20},
        per_target_limits={"cell": 1},
    )


def test_same_claim_and_coordinate_is_blocked_by_per_target_cell_limit(tmp_path):
    ledger = _ledger(tmp_path)
    target_id = _cell_budget_target_id("claim-1", _plan())
    calls = []

    ledger.execute("run", "cell", lambda: calls.append("first"), target_id=target_id)
    with pytest.raises(BudgetExhausted, match="CALL_BUDGET_EXHAUSTED:cell:target="):
        ledger.execute("run", "cell", lambda: calls.append("duplicate"), target_id=target_id)

    assert calls == ["first"]
    assert ledger.used("cell") == 1


def test_current_and_baseline_coordinates_are_distinct(tmp_path):
    ledger = _ledger(tmp_path)
    current = _cell_budget_target_id("claim-1", _plan())
    baseline = _cell_budget_target_id(
        "claim-1", _plan(start_prd_de="2024", end_prd_de="2024")
    )
    calls = []

    ledger.execute("run", "cell", lambda: calls.append("current"), target_id=current)
    ledger.execute("run", "cell", lambda: calls.append("baseline"), target_id=baseline)

    assert current != baseline
    assert calls == ["current", "baseline"]
    assert ledger.used("cell") == 2


def test_different_claims_can_share_the_same_baseline_coordinate(tmp_path):
    ledger = _ledger(tmp_path)
    baseline = _plan(start_prd_de="2024", end_prd_de="2024")
    first = _cell_budget_target_id("claim-level", baseline)
    second = _cell_budget_target_id("claim-change-rate", baseline)
    calls = []

    ledger.execute("run", "cell", lambda: calls.append("level"), target_id=first)
    ledger.execute("run", "cell", lambda: calls.append("change-rate"), target_id=second)

    assert first != second
    assert calls == ["level", "change-rate"]
    assert ledger.used("cell") == 2


def test_obj_level_order_and_annual_a_y_spellings_are_equivalent():
    first = _cell_budget_target_id(
        "claim-1",
        _plan(prd_se="A", obj_levels={"objL1": "00", "objL2": "00"}),
    )
    second = _cell_budget_target_id(
        "claim-1",
        _plan(prd_se="Y", obj_levels={"objL2": "00", "objL1": "00"}),
    )

    assert first == second


@pytest.mark.parametrize(
    ("claim_id", "overrides"),
    [
        ("claim-2", {}),
        ("claim-1", {"org_id": "other-org"}),
        ("claim-1", {"tbl_id": "other-table"}),
        ("claim-1", {"itm_id": "other-item"}),
        ("claim-1", {"start_prd_de": "2024", "end_prd_de": "2024"}),
        ("claim-1", {"obj_levels": {"objL1": "11", "objL2": "00"}}),
    ],
)
def test_claim_or_coordinate_difference_changes_identity(claim_id, overrides):
    assert _cell_budget_target_id("claim-1", _plan()) != _cell_budget_target_id(
        claim_id, _plan(**overrides)
    )


@pytest.mark.parametrize(
    "claim_id,plan",
    [
        ("", _plan()),
        ("claim-1", _plan(tbl_id="")),
        ("claim-1", _plan(obj_levels=None)),
        ("claim-1", _plan(obj_levels={"objL1": ""})),
    ],
)
def test_empty_claim_or_incomplete_coordinate_fails_closed(claim_id, plan):
    with pytest.raises(ValueError, match="CELL_BUDGET_(CLAIM_TARGET_ID_REQUIRED|COORDINATE_INCOMPLETE)"):
        _cell_budget_target_id(claim_id, plan)
