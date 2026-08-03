from src.build_mapping_eligibility_fixture import build_rows


def test_fixture_contains_only_no_context_with_claim_hints():
    reviewed = [
        {"context_eval_id": "a", "article_idx": "1", "sentence_index": "2", "adjudication_status": "NO_CONTEXT",
         "article_title": "기사", "claim_text": "보험료는 3%", "rule_context_status": "CONTEXT_MISSING", "adjudication_notes": "후보 없음"},
        {"context_eval_id": "b", "article_idx": "1", "sentence_index": "3", "adjudication_status": "SKIP"},
    ]
    claim_rows = {("1", "2"): {"value_list": "3", "unit_list": "%", "time_ref": "2025년", "source_org_raw": "", "change_type": "단순수치"}}

    rows = build_rows(reviewed, claim_rows)

    assert len(rows) == 1
    assert rows[0]["human_context_status"] == "NO_CONTEXT"
    assert rows[0]["value_list"] == "3"
    assert rows[0]["eligibility_review_status"] == "pending"
