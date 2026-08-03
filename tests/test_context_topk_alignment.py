from src.context_topk_alignment import candidate_records


PROFILE = {
    "table_key": "101:T1", "org_id": "101", "tbl_id": "T1", "tbl_name": "보험료 지수",
    "doc_meta_text": "보험료 지수", "doc_item_index": "보험료",
    "meta_status": "enriched", "period_types": ["년"],
    "items": [{"itm_id": "I1", "itm_nm": "보험료"}],
    "dimensions": [{"obj_id": "A", "obj_nm": "성별", "values": [{"value_id": "0", "value_name": "계"}]}],
}


def test_only_context_expanded_claims_receive_candidates_and_alignment():
    claims = [
        {"context_eval_id": "ok", "claim_text": "보험료는 2025년 3% 올랐다.", "retrieval_query_text": "보험료 2025년",
         "indicator_raw": "보험료", "period": "2025", "period_type": "년",
         "context_resolution": {"retrieval_policy": "context_expanded"}},
        {"context_eval_id": "blocked", "claim_text": "보험료는 올랐다.",
         "context_resolution": {"retrieval_policy": "claim_only_alignment_blocked"}},
    ]

    records, manifest = candidate_records(claims, [PROFILE], top_k=10)

    assert len(records) == 1
    assert records[0]["context_eval_id"] == "ok"
    assert records[0]["alignment"]["align_status"] == "ALIGNED"
    assert manifest["context_expanded_claims"] == 1
    assert manifest["alignment_blocked_claims"] == 1


def test_missing_structured_indicator_does_not_use_raw_query_for_item_alignment():
    profile = {**PROFILE, "items": [{"itm_id": "I1", "itm_nm": "보험료"}, {"itm_id": "I2", "itm_nm": "보험료 지수"}]}
    claim = {"context_eval_id": "ambiguous", "claim_text": "보험료 지수는 2025년", "retrieval_query_text": "보험료 지수는 2025년",
             "context_resolution": {"retrieval_policy": "context_expanded"}}

    records, _ = candidate_records([claim], [profile], top_k=1)

    assert records[0]["alignment"] == {"align_status": "ITEM_AMBIGUOUS", "reason": "claim_indicator_missing", "matched_dimensions": {}}


def test_mapping_gate_includes_claim_only_safe_and_excludes_out_of_scope():
    claims = [
        {"context_eval_id": "expanded", "claim_text": "보험료 2025년", "retrieval_query_text": "보험료 2025년", "indicator_raw": "보험료", "period": "2025", "period_type": "년", "mapping_eligibility": "CONTEXT_EXPANDED"},
        {"context_eval_id": "safe", "claim_text": "보험료 2025년", "retrieval_query_text": "보험료 2025년", "indicator_raw": "보험료", "period": "2025", "period_type": "년", "mapping_eligibility": "CLAIM_ONLY_SAFE"},
        {"context_eval_id": "out", "claim_text": "보험료 2025년", "mapping_eligibility": "OUT_OF_SCOPE"},
    ]

    records, manifest = candidate_records(claims, [PROFILE], top_k=1)

    assert {row["context_eval_id"] for row in records} == {"expanded", "safe"}
    assert manifest["eligible_claims"] == 2
    assert manifest["context_expanded_claims"] == 1
    assert manifest["claim_only_safe_claims"] == 1
