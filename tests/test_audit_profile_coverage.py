from src.audit_profile_coverage import audit_coverage


def table(key, name, *, profile_present=False):
    return {
        "table_key": key, "org_id": "101", "tbl_id": key.split(":", 1)[1], "tbl_name": name,
        "category_paths": [["경제"]], "profile_present": profile_present,
        "metadata_status": "enriched" if profile_present else "registry_only",
    }


def test_audit_separates_exact_profile_gap_from_partial_title_match():
    claims = [
        {"context_eval_id": "exact", "indicator_raw": "실업률"},
        {"context_eval_id": "partial", "indicator_raw": "보험료"},
        {"context_eval_id": "empty"},
    ]
    registry = [
        table("101:T1", "실업률"),
        table("101:T2", "자동차보험료", profile_present=True),
    ]

    rows, seeds, manifest = audit_coverage(claims, registry)
    by_id = {row["context_eval_id"]: row for row in rows}

    assert by_id["exact"]["coverage_status"] == "UNIQUE_TITLE_MATCH_REVIEW_REQUIRED"
    assert by_id["partial"]["coverage_status"] == "PROFILE_ALREADY_AVAILABLE"
    assert by_id["empty"]["coverage_status"] == "NO_USABLE_STRUCTURED_TERM"
    assert seeds == [{
        "context_eval_id": "exact", "table_key": "101:T1", "org_id": "101", "tbl_id": "T1", "tbl_name": "실업률",
        "reason": "unique_exact_indicator_to_table_name", "matched_term": "실업률", "review_required": True,
    }]
    assert manifest["metadata_review_seed_count"] == 1


def test_human_context_term_is_audit_evidence_but_never_an_automatic_seed():
    claims = [{
        "context_eval_id": "human", "context_resolution": {
            "adjudication_source": "HUMAN", "resolved_terms": ["자동차보험료"],
        },
    }]
    registry = [table("101:T1", "자동차보험료")]

    rows, seeds, _ = audit_coverage(claims, registry)

    assert rows[0]["coverage_status"] == "UNIQUE_TITLE_MATCH_REVIEW_REQUIRED"
    assert rows[0]["registry_direct_matches"][0]["term_source"] == "human_context_term"
    assert seeds == []
