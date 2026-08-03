from src.build_context_gate_claim_inputs import enrich_records, normalized_period


def test_only_eligible_records_receive_rule_structure_and_normalized_period():
    records = [
        {"context_eval_id": "safe", "article_idx": "1", "claim_text": "2025년 보험료는 3% 올랐다.", "mapping_eligibility": "CLAIM_ONLY_SAFE"},
        {"context_eval_id": "blocked", "article_idx": "1", "claim_text": "보험료는 올랐다.", "mapping_eligibility": "CONTEXT_REQUIRED_UNRESOLVED"},
    ]

    rows = enrich_records(records, {"1": "2025-06-01"})

    assert len(rows) == 1
    assert rows[0]["context_eval_id"] == "safe"
    assert rows[0]["period"] == "2025"
    assert rows[0]["period_type"] == "년"
    assert rows[0]["auto_structure_audit"]["is_human_gold"] is False


def test_relative_month_uses_article_publication_date():
    assert normalized_period("지난달", "2025-06-10") == ("2025-05", "월")


def test_context_supplies_only_unambiguous_indicator_and_period_with_audit_evidence():
    records = [{"context_eval_id": "c1", "article_idx": "1", "sentence_index": "3", "claim_text": "이 수치는 3% 올랐다.", "mapping_eligibility": "CLAIM_ONLY_SAFE"}]
    contexts = {"c1": {"article_title": "보험 기사", "context_window_json": '[{"sentence_index": 2, "text": "2025년 자동차보험료가 상승했다."}, {"sentence_index": 3, "text": "이 수치는 3% 올랐다."}]'}}

    rows = enrich_records(records, {"1": "2025-06-01"}, contexts)

    assert rows[0]["indicator_raw"] == "자동차보험료"
    assert rows[0]["period"] == "2025"
    assert rows[0]["auto_structure_audit"]["context_indicator_evidence"][0]["sentence_index"] == 2
