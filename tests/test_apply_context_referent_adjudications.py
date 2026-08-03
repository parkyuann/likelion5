import json

from src.apply_context_referent_adjudications import apply_rows, read_fixture_rows


def test_apply_rows_emits_only_adjudicated_evidence_backed_context():
    rows = [{
        "context_eval_id": "context_1", "article_idx": "1", "sentence_index": "2", "claim_text": "해당 보험료는 3% 올랐다.",
        "candidate_terms_json": json.dumps(["손해보험"], ensure_ascii=False),
        "evidence_json": json.dumps([{"term": "손해보험", "sentence_index": 1}], ensure_ascii=False),
        "review_status": "adjudicated", "adjudication_status": "RESOLVED", "selected_referent": "손해보험",
        "evidence_sentence_index": "1", "adjudication_source": "HCX-007",
    }, {
        "context_eval_id": "context_2", "review_status": "pending",
    }]
    result = apply_rows(rows)
    assert len(result) == 1
    assert result[0]["context_resolution"]["resolved_terms"] == ["손해보험"]
    assert "손해보험" in result[0]["retrieval_query_text"]


def test_read_fixture_rows_accepts_excel_cp949_tsv(tmp_path):
    path = tmp_path / "reviewed.csv"
    path.write_bytes(
        "context_eval_id\tclaim_text\treview_status\ncontext_1\t해당 보험료\tadjudicated\n".encode("cp949")
    )

    rows = read_fixture_rows(path)

    assert rows == [{"context_eval_id": "context_1", "claim_text": "해당 보험료", "review_status": "adjudicated"}]
