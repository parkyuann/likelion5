from src.apply_mapping_eligibilities import apply_eligibilities


def record(context_eval_id, status):
    return {"context_eval_id": context_eval_id, "context_resolution": {"adjudication_status": status}}


def test_applies_human_no_context_gate_and_context_defaults():
    review = [{
        "context_eval_id": "no_context", "mapping_eligibility": "AMBIGUOUS",
        "mapping_eligibility_notes": "통계표 범위 불명", "eligibility_review_status": "adjudicated",
    }]
    rows = apply_eligibilities([
        record("resolved", "RESOLVED"), record("no_context", "NO_CONTEXT"),
        record("ambiguous", "AMBIGUOUS"), record("skip", "SKIP"),
    ], review)

    assert [row["mapping_eligibility"] for row in rows] == [
        "CONTEXT_EXPANDED", "CONTEXT_REQUIRED_UNRESOLVED", "CONTEXT_REQUIRED_UNRESOLVED", "OUT_OF_SCOPE",
    ]
    assert rows[1]["mapping_eligibility_audit"]["raw_mapping_eligibility"] == "AMBIGUOUS"


def test_no_context_without_review_is_rejected():
    try:
        apply_eligibilities([record("no_context", "NO_CONTEXT")], [])
    except ValueError as error:
        assert "without adjudicated eligibility" in str(error)
    else:
        raise AssertionError("NO_CONTEXT requires a human eligibility decision")
