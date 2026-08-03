import json

from src.audit_claim_context_resolution import summarize_context_rows


def test_context_audit_reports_status_counts_and_unresolved_rate():
    rows = [
        {"context_resolution_json": json.dumps({"status": "RESOLVED"}), "claim_text": "a"},
        {"context_resolution_json": json.dumps({"status": "REFERENT_CANDIDATE"}), "claim_text": "b"},
        {"context_resolution_json": json.dumps({"status": "NOT_APPLICABLE"}), "claim_text": "c"},
    ]
    report = summarize_context_rows(rows, sample_limit=1)
    assert report["status_counts"] == {"NOT_APPLICABLE": 1, "REFERENT_CANDIDATE": 1, "RESOLVED": 1}
    assert report["context_required_rows"] == 2
    assert report["unresolved_rows"] == 1
    assert report["unresolved_rate"] == 0.3333
    assert len(report["samples"]["RESOLVED"]) == 1
