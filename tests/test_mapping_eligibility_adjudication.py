from src.mapping_eligibility_adjudication import normalize_mapping_eligibility, validate_eligibility_decision


def test_completed_claim_only_safe_decision_is_valid():
    row = {
        "mapping_eligibility": "CLAIM_ONLY_SAFE", "eligibility_review_status": "adjudicated",
        "mapping_eligibility_notes": "지표와 기간이 문장에 명시됨",
    }
    assert validate_eligibility_decision(row) == []


def test_pending_or_unknown_eligibility_cannot_be_applied():
    assert "eligibility review must be adjudicated" in validate_eligibility_decision({
        "mapping_eligibility": "OUT_OF_SCOPE", "eligibility_review_status": "pending", "mapping_eligibility_notes": "개별 상품",
    })
    assert "invalid mapping_eligibility" in validate_eligibility_decision({
        "mapping_eligibility": "MAYBE", "eligibility_review_status": "adjudicated", "mapping_eligibility_notes": "불명",
    })


def test_ambiguous_input_alias_is_preserved_but_normalizes_to_safe_block():
    row = {"mapping_eligibility": "AMBIGUOUS", "eligibility_review_status": "adjudicated", "mapping_eligibility_notes": "통계표 범위 불명"}
    assert validate_eligibility_decision(row) == []
    assert normalize_mapping_eligibility("AMBIGUOUS") == "CONTEXT_REQUIRED_UNRESOLVED"
