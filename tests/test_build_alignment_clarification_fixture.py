import json

from src.build_alignment_clarification_fixture import build_rows


def test_fixture_asks_only_for_missing_slots_with_evidence_requirement():
    claims = [{
        "context_eval_id": "c1", "article_idx": "1", "sentence_index": "2", "claim_text": "보험료는 올랐다.",
        "mapping_eligibility": "CLAIM_ONLY_SAFE", "indicator_raw": None, "dimension_json": {}, "period": None,
    }]
    alignment = [{
        "context_eval_id": "c1", "candidate_rank": 1, "table_key": "101:T", "tbl_name": "보험료",
        "alignment": {"align_status": "ITEM_AMBIGUOUS", "reason": "claim_indicator_missing"},
    }]
    reviewed = [{"context_eval_id": "c1", "article_title": "기사", "context_window_json": "[]"}]

    rows = build_rows(claims, alignment, reviewed)

    assert len(rows) == 1
    assert json.loads(rows[0]["missing_slots_json"]) == ["indicator", "period"]
    assert all(question["response_contract"].endswith("NO_EVIDENCE") for question in json.loads(rows[0]["clarification_questions_json"]))
    assert rows[0]["clarification_request_status"] == "USER_REQUIRED"
    assert rows[0]["user_response_status"] == "pending"
