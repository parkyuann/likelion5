import json

import pytest

from src.hcx_context_referent_runner import hcx_input, normalize_prediction, run_rows


ROW = {
    "context_eval_id": "context_1", "claim_text": "해당 보험료는 3% 올랐다.", "article_title": "보험",
    "candidate_terms_json": json.dumps(["손해보험"], ensure_ascii=False),
    "evidence_json": json.dumps([{"term": "손해보험", "sentence_index": 1}], ensure_ascii=False),
    "context_window_json": json.dumps([{"sentence_index": 1, "text": "손해보험 계약"}], ensure_ascii=False),
}


def test_hcx_prediction_must_obey_candidate_and_evidence_contract():
    fixture, _ = hcx_input(ROW)
    result = normalize_prediction({"adjudication_status": "RESOLVED", "selected_referent": "손해보험", "evidence_sentence_index": 1}, fixture)
    assert result["selected_referent"] == "손해보험"
    with pytest.raises(ValueError, match="invalid HCX context decision"):
        normalize_prediction({"adjudication_status": "RESOLVED", "selected_referent": "치아보험", "evidence_sentence_index": 1}, fixture)


def test_runner_writes_resumable_result_without_applying_to_claims(tmp_path):
    def fake_call(*_args, **_kwargs):
        return ({"adjudication_status": "AMBIGUOUS", "selected_referent": "", "evidence_sentence_index": None}, {}, 10.0)

    output = tmp_path / "result.jsonl"
    result = run_rows([ROW], output=output, api_key="secret", model="HCX-007", max_rows=None, delay_seconds=0, call=fake_call)
    assert result[0]["status"] == "ok"
    assert json.loads(output.read_text(encoding="utf-8"))["prediction"]["adjudication_status"] == "AMBIGUOUS"
    assert run_rows([ROW], output=output, api_key="secret", model="HCX-007", max_rows=None, delay_seconds=0, call=fake_call) == []
