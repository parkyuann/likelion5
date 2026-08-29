from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.develop_verify_service import (
    _pending_clarification,
    _pending_clarification_plan,
    _project_failure_recovery_clarification,
)
from backend.errors import BackendError


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _recovery(*, reason: str = "INDICATOR_REQUIRED", role: str = "indicator") -> dict:
    record = {
        "contract_version": "failure-recovery-shadow-v1",
        "state_contract_version": "retrieval-clarification-state-v1",
        "state": "DIRECT_FIELD_MISSING",
        "action": "ASK_USER",
        "reason": reason,
        "question": {
            "question_id": "q-indicator-1",
            "role": role,
            "prompt": "어떤 통계 지표를 확인할까요?",
            "input_mode": "FREE_TEXT",
            "options": [],
            "answer": None,
            "model_prefill": False,
            "internal_ids_exposed": False,
        },
        "retry_budget": {"used": 0, "limit": 1},
    }
    digest = _sha(record)
    record["state_sha256"] = digest
    record["sha256"] = digest
    return record


def test_indicator_missing_projects_to_deterministic_retrieval_plan_without_cell():
    first = _project_failure_recovery_clarification(_recovery())
    second = _project_failure_recovery_clarification(_recovery())

    assert first == second
    assert first["contract_version"] == "clarification-plan-v2"
    assert first["question"]["role"] == "indicator"
    assert first["resume_from_stage"] == "retrieval"
    assert "retrieval" in first["invalidated_stages"]
    assert "retrieval" not in first["reusable_artifacts"]
    assert first["question"]["options"] == []
    assert first["question"]["model_prefill"] is False
    assert first["question"]["internal_ids_exposed"] is False
    assert first["question"]["answer"] is None
    assert first["cell_api_calls"] == 0
    assert first["hcx_answer_calls"] == 0
    assert first["clarification_plan_sha256"] == _sha({k: v for k, v in first.items() if k != "clarification_plan_sha256"})


def test_population_unbound_projects_to_retrieval_resume():
    record = _recovery(reason="POPULATION_UNBOUND", role="population")
    record["state"] = "SELECTOR_CLARIFICATION_POSSIBLE"
    unsigned = {k: v for k, v in record.items() if k not in {"state_sha256", "sha256"}}
    record["state_sha256"] = _sha(unsigned)
    record["sha256"] = record["state_sha256"]

    plan = _project_failure_recovery_clarification(record)
    assert plan["question"]["role"] == "population"
    assert plan["resume_from_stage"] == "retrieval"
    assert "retrieval" not in plan["reusable_artifacts"]


@pytest.mark.parametrize(
    ("reason", "role", "resume_from_stage"),
    [
        ("ARTICLE_DATE_REQUIRED", "article_date", "layers"),
        ("PERIOD_UNKNOWN", "period", "layers"),
        ("ITEM_REQUIRED", "item", "retrieval"),
        ("SOURCE_REQUIRED", "source", "retrieval"),
    ],
)
def test_resume_dependency_mapping_is_closed(reason, role, resume_from_stage):
    plan = _project_failure_recovery_clarification(_recovery(reason=reason, role=role))

    assert plan["resume_from_stage"] == resume_from_stage
    assert "retrieval" in plan["invalidated_stages"]
    if resume_from_stage == "retrieval":
        assert "retrieval" not in plan["reusable_artifacts"]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda row: row.update({"state_sha256": "0" * 64}),
        lambda row: row.update({"sha256": "1" * 64}),
        lambda row: row["question"].update({"role": "region"}),
        lambda row: row["question"].update({"options": [{"label": "전국"}]}),
        lambda row: row["question"].update({"model_prefill": True}),
        lambda row: row["question"].update({"internal_ids_exposed": True}),
        lambda row: row["question"].update({"internal_id": "objL1=00"}),
        lambda row: row["question"].update({"answer": "출생아 수"}),
        lambda row: row.update({"contract_version": "failure-recovery-shadow-v0"}),
        lambda row: row.update({"state_contract_version": "retrieval-clarification-state-v0"}),
        lambda row: row.update({"state": "RETRIEVAL_INSUFFICIENT"}),
        lambda row: row.update({"action": "CORRECTIVE_RETRIEVAL"}),
        lambda row: row.update({"reason": "UNKNOWN_REASON"}),
    ],
)
def test_malformed_recovery_is_rejected_with_bounded_code(mutator):
    record = _recovery()
    mutator(record)
    with pytest.raises(BackendError) as error:
        _project_failure_recovery_clarification(record)
    assert error.value.code == "CLARIFICATION_PLAN_INVALID"
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    ("reason", "role"),
    [
        ("INDICATOR_REQUIRED", "indicator"),
        ("ITEM_REQUIRED", "item"),
        ("SOURCE_REQUIRED", "source"),
        ("ARTICLE_DATE_REQUIRED", "article_date"),
        ("POPULATION_UNBOUND", "population"),
        ("REGION_UNBOUND", "region"),
        ("PERIOD_UNKNOWN", "period"),
        ("PERIOD_INVALID", "period"),
    ],
)
def test_reason_role_pairs_are_closed(reason, role):
    record = _recovery(reason=reason, role=role)
    plan = _project_failure_recovery_clarification(record)
    assert plan["question"]["role"] == role


def test_recovery_projection_is_used_by_pending_reader_and_plan_reader(tmp_path: Path):
    ledger = {"failure_recovery_shadow": _recovery()}
    (tmp_path / "04_stage_ledger.jsonl").write_text(
        json.dumps(ledger, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    public = _pending_clarification(tmp_path)
    plan = _pending_clarification_plan(tmp_path)

    assert public is not None
    assert public["type"] == "needs_user_input"
    assert public["question"]["role"] == "indicator"
    assert public["clarification_receipt"]["cell_api_calls"] == 0
    assert plan is not None
    assert plan["contract_version"] == "clarification-plan-v2"
    assert plan["resume_from_stage"] == "retrieval"
    assert "binding_continuation" not in plan


def test_valid_post_binding_plan_remains_untouched(tmp_path: Path):
    plan = {
        "contract_version": "clarification-plan-v2",
        "reason": "REGION_UNBOUND",
        "question": {"id": "post-1", "role": "region", "prompt": "어느 지역인가요?", "options": []},
        "binding_continuation": {"contract_version": "binding-continuation-v1"},
    }
    (tmp_path / "04_stage_ledger.jsonl").write_text(
        json.dumps({"clarification_plan": plan}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert _pending_clarification_plan(tmp_path) == plan
