import json

from src.apply_user_clarifications import apply_rows


CLAIM = {"context_eval_id": "c1", "claim_text": "보험료", "indicator_raw": None, "period": None}


def response_row(response, status="answered"):
    return {
        "context_eval_id": "c1", "missing_slots_json": json.dumps(["indicator", "period"]),
        "user_response_json": json.dumps(response, ensure_ascii=False), "user_response_status": status,
    }


def test_valid_user_response_resumes_search_and_preserves_audit_source():
    response = {
        "indicator": {"value": "자동차보험료", "basis": "user_provided"},
        "period": {"value": "2025-05", "basis": "article_evidence", "evidence_sentence_index": 3},
    }
    applied, audit = apply_rows([CLAIM], [response_row(response)])

    assert audit == [{"context_eval_id": "c1", "status": "SEARCH_RESUMED", "reason": "validated_user_response"}]
    assert applied[0]["indicator_raw"] == "자동차보험료"
    assert applied[0]["period_type"] == "월"
    assert applied[0]["user_clarification_audit"]["source"] == "USER"


def test_no_evidence_stays_unverifiable_and_is_not_sent_to_search():
    applied, audit = apply_rows([CLAIM], [response_row({"indicator": "NO_EVIDENCE", "period": "NO_EVIDENCE"})])

    assert applied == []
    assert audit[0]["status"] == "UNVERIFIABLE"


def test_pending_user_response_remains_user_required():
    applied, audit = apply_rows([CLAIM], [response_row({}, status="pending")])

    assert applied == []
    assert audit[0]["status"] == "USER_REQUIRED"
