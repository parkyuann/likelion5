from __future__ import annotations

from types import SimpleNamespace
from types import MappingProxyType
from pathlib import Path
import sys
import types

import pytest

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas

from src.develop.failure_recovery_shadow_v1 import build_post_binding_clarification_plan, plan_failure_recovery
from src.news_verification.runtime.r4c1_claim_core_v2 import canonical_bytes
from src.news_verification.runtime.run_pipeline_operational_v2 import fetch_exact_single_cell, _final_evidence_sha256
from src.news_verification.runtime.same_series_evidence_v1 import exact_cell


def _query_receipt(query):
    import hashlib
    selector = dict(sorted((str(key), str(value)) for key, value in query["obj_levels"].items()))
    period = {key: str(query[key]) for key in ("prd_se", "start_prd_de", "end_prd_de")}
    return {
        "state": "QUERY_READY",
        "query_plan_sha256": hashlib.sha256(canonical_bytes(query)).hexdigest(),
        "selector_unique": True,
        "selector_sha256": hashlib.sha256(canonical_bytes(selector)).hexdigest(),
        "period_sha256": hashlib.sha256(canonical_bytes(period)).hexdigest(),
    }


def _top50(*, hold_reason="REGION_UNBOUND", incomplete=False):
    projection = SimpleNamespace(
        table_key="table-1",
        canonical_sha256="c" * 64,
        hold_reasons=("PROFILE_INCOMPLETE",) if incomplete else (hold_reason,),
        assignments=(),
        slot_diagnostics=({
            "role": "region", "status": "MISSING", "table_key": "table-1", "profile_sha256": "p" * 64,
            "option_inventory": [{"label": "전국", "value_id": "00"}],
        },),
    )
    resolution = SimpleNamespace(outcome="HOLD", hold_reason=hold_reason)
    return SimpleNamespace(
        resolution=resolution,
        projections=(projection,),
        candidate_membership=("table-1",),
        pinned_raw_profiles={"table-1": {"table_key": "table-1", "release_id": "r1", "profile_sha256": "p" * 64}},
        pinned_projection_profiles={"table-1": {"table_key": "table-1", "release_id": "r1", "profile_sha256": "p" * 64}},
    )


def test_missing_indicator_is_direct_field_missing_and_cell_guard_blocks_before_ready():
    plan = plan_failure_recovery({"retrieval_fields": {"indicator": ""}}, None)
    assert plan["state"] == "DIRECT_FIELD_MISSING"
    assert plan["action"] == "ASK_USER"

    query = {"org_id": "o", "tbl_id": "t", "itm_id": "i", "prd_se": "Y", "start_prd_de": "2025", "end_prd_de": "2025", "obj_levels": {"objL1": "00"}}
    called = []
    fetcher = lambda value: called.append(value) or [{
        "ORG_ID": "o", "TBL_ID": "t", "ITM_ID": "i", "PRD_SE": "Y", "PRD_DE": "2025", "C1": "00",
    }]
    blocked = fetch_exact_single_cell(query, fetcher)
    assert blocked["status"] == "QUERY_READY_RECEIPT_REQUIRED"
    assert called == []

    ready = fetch_exact_single_cell(query, fetcher, query_ready_receipt=_query_receipt(query), target_call_ledger={"cell_api": 0})
    assert ready["status"] == "CELL_RESOLVED"
    assert len(called) == 1


def test_selector_plan_excludes_incomplete_profile_and_binds_candidate_bundle():
    plan = build_post_binding_clarification_plan(_top50(), target_id="a:1")
    assert plan is not None
    assert plan["contract_version"] == "clarification-plan-v2"
    assert plan["candidate_bundle_sha256"]
    assert plan["option_bundle_sha256"]
    assert plan["clarification_plan_sha256"]
    assert plan["binding_continuation"]["candidate_bundle_sha256"] == plan["candidate_bundle_sha256"]
    assert plan["question"]["options"][0]["label"] == "전국"

    excluded = build_post_binding_clarification_plan(_top50(incomplete=True), target_id="a:1")
    assert excluded is None
    incomplete = plan_failure_recovery({"retrieval_fields": {"indicator": "출생아 수"}}, _top50(incomplete=True))
    assert incomplete["state"] == "METADATA_PROFILE_INCOMPLETE"


def test_corrective_budget_has_one_round_and_exhaustion_is_terminal():
    row = {"retrieval_fields": {"indicator": "출생아 수", "item": ["월별 출생아 수"]}}
    initial = plan_failure_recovery(row, None)
    assert initial["state"] == "RETRIEVAL_INSUFFICIENT"
    assert initial["action"] == "CORRECTIVE_RETRIEVAL"
    assert initial["retry_budget"] == {"used": 0, "limit": 1}

    exhausted = plan_failure_recovery({**row, "_corrective_round_used": True}, None)
    assert exhausted["state"] == "CORRECTIVE_RETRIEVAL_EXHAUSTED"
    assert exhausted["action"] == "STOP"
    assert exhausted["retry_budget"] == {"used": 1, "limit": 1}


def test_query_ready_receipt_mismatch_and_non_unique_state_never_call_cell():
    query = {"org_id": "o", "tbl_id": "t", "itm_id": "i", "prd_se": "Y", "start_prd_de": "2025", "end_prd_de": "2025", "obj_levels": {"objL1": "00"}}
    called = []
    fetcher = lambda value: called.append(value) or []
    invalid = fetch_exact_single_cell(
        query,
        fetcher,
        query_ready_receipt={"state": "QUERY_READY", "query_plan_sha256": "0" * 64},
    )
    assert invalid["status"] == "QUERY_READY_RECEIPT_INVALID"
    assert called == []

    guarded = fetch_exact_single_cell(query, fetcher, query_ready_receipt=_query_receipt(query), target_call_ledger={"cell_api": 1})
    assert guarded["status"] == "CELL_API_ONE_CALL_GUARD"
    assert called == []


def test_cell_authorization_is_single_use_and_atomic_before_fetch():
    query = {"org_id": "o", "tbl_id": "t", "itm_id": "i", "prd_se": "Y", "start_prd_de": "2025", "end_prd_de": "2025", "obj_levels": {"objL1": "00"}}
    called = []
    fetcher = lambda value: called.append(value) or [{"ORG_ID": "o", "TBL_ID": "t", "ITM_ID": "i", "PRD_SE": "Y", "PRD_DE": "2025", "C1": "00"}]
    ledger = {"cell_api": 0}
    first = fetch_exact_single_cell(query, fetcher, query_ready_receipt=_query_receipt(query), target_call_ledger=ledger)
    second = fetch_exact_single_cell(query, fetcher, query_ready_receipt=_query_receipt(query), target_call_ledger=ledger)
    assert first["status"] == "CELL_RESOLVED"
    assert second["status"] == "CELL_API_ONE_CALL_GUARD"
    assert len(called) == 1
    assert ledger["cell_api"] == 1

    missing_org = dict(query)
    missing_org.pop("org_id")
    blocked_identity = fetch_exact_single_cell(
        missing_org,
        fetcher,
        query_ready_receipt=_query_receipt(missing_org),
        target_call_ledger={"cell_api": 0},
    )
    assert blocked_identity["status"] == "QUERY_PLAN_INCOMPLETE"
    assert len(called) == 1

    immutable = MappingProxyType({"cell_api": 0})
    blocked = fetch_exact_single_cell(query, fetcher, query_ready_receipt=_query_receipt(query), target_call_ledger=immutable)
    assert blocked["status"] == "CELL_API_ONE_CALL_GUARD"
    assert len(called) == 1


@pytest.mark.parametrize("missing_field", ["ORG_ID", "C1"])
def test_missing_required_kosis_coordinate_is_fail_closed(missing_field):
    query = {
        "org_id": "o", "tbl_id": "t", "itm_id": "i", "prd_se": "Y",
        "start_prd_de": "2025", "end_prd_de": "2025", "obj_levels": {"objL1": "00"},
    }
    row = {"ORG_ID": "o", "TBL_ID": "t", "ITM_ID": "i", "PRD_SE": "Y", "PRD_DE": "2025", "C1": "00"}
    row.pop(missing_field)
    called = []
    result = fetch_exact_single_cell(
        query,
        lambda value: called.append(value) or [row],
        query_ready_receipt=_query_receipt(query),
        target_call_ledger={"cell_api": 0},
    )
    assert result["status"] == "CELL_QUERY_MISMATCH"
    assert result["mismatch"]["field"] == missing_field
    assert result["mismatch"]["reason"] == "RESPONSE_FIELD_MISSING"
    assert len(called) == 1


@pytest.mark.parametrize("missing_field", ["ORG_ID", "C1"])
def test_same_series_validator_rejects_missing_required_kosis_coordinate(missing_field):
    query = {
        "org_id": "o", "tbl_id": "t", "itm_id": "i", "prd_se": "Y",
        "start_prd_de": "2025", "end_prd_de": "2025", "obj_levels": {"objL1": "00"},
    }
    row = {"ORG_ID": "o", "TBL_ID": "t", "ITM_ID": "i", "PRD_SE": "Y", "PRD_DE": "2025", "C1": "00", "DT": "1"}
    row.pop(missing_field)
    result = exact_cell(query, lambda value: [row])
    assert result["status"] == "CELL_QUERY_MISMATCH"
    assert missing_field in result["mismatch_fields"]


def test_final_evidence_identity_is_sensitive_to_unit_and_packet():
    base = {
        "target_id": "a:1", "release_id": "r1", "candidate_bundle_sha256": "a" * 64,
        "profile_sha256": "b" * 64, "query_plan": {"org_id": "o", "tbl_id": "t"},
        "query_ready_receipt": {"state": "QUERY_READY"}, "cell_response_sha256": "c" * 64,
        "cell_status": "CELL_RESOLVED", "official_unit": "명", "evidence_packet_sha256": "d" * 64,
        "answer_packet_sha256": "e" * 64, "comparison": {"verdict": "VERIFIED"}, "annual_requery": None,
    }
    first = _final_evidence_sha256(**base)
    changed_unit = _final_evidence_sha256(**{**base, "official_unit": "%"})
    changed_packet = _final_evidence_sha256(**{**base, "evidence_packet_sha256": "f" * 64})
    assert first != changed_unit
    assert first != changed_packet
