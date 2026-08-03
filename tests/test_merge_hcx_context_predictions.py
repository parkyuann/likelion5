from src.merge_hcx_context_predictions import merge_rows


def test_merge_adds_prediction_columns_without_adjudicating_fixture():
    rows = [{
        "context_eval_id": "context_1", "review_status": "pending",
        "adjudication_status": "", "selected_referent": "",
    }]
    results = {"context_1": {
        "context_eval_id": "context_1", "status": "ok", "latency_ms": 12.5,
        "prediction": {
            "adjudication_status": "RESOLVED", "selected_referent": "손해보험",
            "evidence_sentence_index": 3, "adjudication_notes": "앞 문장의 명시어",
        },
    }}

    merged = merge_rows(rows, results)

    assert merged[0]["hcx_adjudication_status"] == "RESOLVED"
    assert merged[0]["hcx_selected_referent"] == "손해보험"
    assert merged[0]["review_status"] == "pending"
    assert merged[0]["adjudication_status"] == ""


def test_merge_keeps_validation_error_separate_from_human_review_fields():
    rows = [{"context_eval_id": "context_2", "review_status": "pending", "adjudication_status": ""}]
    results = {"context_2": {"context_eval_id": "context_2", "status": "error", "error": "invalid HCX context decision"}}

    merged = merge_rows(rows, results)

    assert merged[0]["hcx_run_status"] == "error"
    assert merged[0]["hcx_validation_error"] == "invalid HCX context decision"
    assert merged[0]["adjudication_status"] == ""
