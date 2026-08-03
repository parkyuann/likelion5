from src.user_clarification_adjudication import search_allowed, validate_user_response


def test_user_answer_can_resume_search_without_internal_review():
    response = {
        "indicator": {"value": "자동차보험료", "basis": "user_provided"},
        "period": {"value": "2025-05", "basis": "article_evidence", "evidence_sentence_index": 2},
    }
    assert validate_user_response(["indicator", "period"], response) == []
    assert search_allowed(["indicator", "period"], response) is True


def test_no_evidence_keeps_search_suspended():
    response = {"indicator": "NO_EVIDENCE"}
    assert validate_user_response(["indicator"], response) == []
    assert search_allowed(["indicator"], response) is False
