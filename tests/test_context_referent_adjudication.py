import pytest

from src.context_referent_adjudication import apply_adjudication, validate_adjudication


FIXTURE = {
    "context_eval_id": "context_1", "candidate_terms": ["손해보험", "생명보험"],
    "evidence": [
        {"term": "손해보험", "sentence_index": 2},
        {"term": "생명보험", "sentence_index": 2},
    ],
}


def test_adjudication_requires_candidate_and_matching_evidence_span():
    invalid = {"adjudication_status": "RESOLVED", "selected_referent": "치아보험", "evidence_sentence_index": 2}
    assert "selected_referent must be one of candidate_terms" in validate_adjudication(FIXTURE, invalid)
    invalid = {"adjudication_status": "RESOLVED", "selected_referent": "손해보험", "evidence_sentence_index": 3}
    assert "evidence_sentence_index must support selected_referent" in validate_adjudication(FIXTURE, invalid)


def test_valid_adjudication_expands_query_only_after_evidence_backed_resolution():
    applied = apply_adjudication("해당 보험료는 3% 올랐다.", FIXTURE, {
        "adjudication_status": "RESOLVED", "selected_referent": "손해보험", "evidence_sentence_index": 2,
        "adjudication_source": "HCX-007",
    })
    assert applied["context_resolution"]["status"] == "RESOLVED"
    assert "손해보험" in applied["retrieval_query_text"]


def test_ambiguous_decision_keeps_alignment_blocked():
    applied = apply_adjudication("보험료는 3% 올랐다.", FIXTURE, {"adjudication_status": "AMBIGUOUS"})
    assert applied["context_resolution"]["retrieval_policy"] == "claim_only_alignment_blocked"
    assert applied["retrieval_query_text"] == "보험료는 3% 올랐다."
