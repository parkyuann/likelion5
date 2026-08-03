from src.develop.article_claim_pipeline import build_span_candidates, validate_claim_observation_scope, validate_span_binding
from src.develop.table_mapping_features import build_table_mapping_features


def test_mapping_features_keep_selected_spans_and_region_constraints():
    article = "경북은 1.6%, 울산은 1.4% 실질 지역내총생산 증가를 기록했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "is_kosis_candidate": True,
        "claim_type": "실질 지역내총생산 증가율",
        "indicator_norm": "실질 지역내총생산 증가율",
        "measurement_type": "CHANGE_RATE",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "EXPLICIT_PAIRING", "pairing_evidence_sentence_ids": [0]},
    }
    binding = {"observations": [
        {"value_span_id": by_text["1.6%"], "period_span_id": None, "dimension_span_ids": [by_text["경북"]]},
        {"value_span_id": by_text["1.4%"], "period_span_id": None, "dimension_span_ids": [by_text["울산"]]},
    ]}
    binding_validation = validate_span_binding(claim, binding, candidates)
    scope_validation = validate_claim_observation_scope(article, claim, binding_validation)

    features, block = build_table_mapping_features(
        article_idx="1", article_sha256="hash", article_text=article, claim_index=0,
        semantic_claim=claim, binding=binding, binding_validation=binding_validation, scope_validation=scope_validation,
    )

    assert block is None
    assert len(features) == 2
    first = features[0]
    assert first["retrieval_eligibility"] is True
    assert first["indicator_terms"] == ["실질", "지역내총생산"]
    assert first["measurement_type"] == "CHANGE_RATE"
    assert first["population_terms"] == []
    assert first["source_span_ids"]["value"] == [by_text["1.6%"]]
    assert first["value_role"] == "TARGET_MEASURE"
    assert first["indicator_value_relation"] == "SAME_METRIC"
    assert first["relation_contract_status"] == "LEGACY_UNASSERTED"
    assert first["dimension_constraints"] == [{
        "dimension_type": "지역", "raw": "경북", "normalized": "경북", "granularity": "시도",
        "source_span_id": by_text["경북"], "sentence_id": 0,
    }]


def test_mapping_features_do_not_create_retrieval_rows_for_scope_blocked_claims():
    article = "소매판매가 증가했다. 제조업은 9.1% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "is_kosis_candidate": True,
        "claim_type": "소매판매 증가율",
        "indicator_norm": "소매판매 증가율",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [1],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["9.1%"], "period_span_id": None, "dimension_span_ids": [by_text["제조업"]],
    }]}
    binding_validation = validate_span_binding(claim, binding, candidates)
    scope_validation = validate_claim_observation_scope(article, claim, binding_validation)

    features, block = build_table_mapping_features(
        article_idx="2", article_sha256="hash", article_text=article, claim_index=0,
        semantic_claim=claim, binding=binding, binding_validation=binding_validation, scope_validation=scope_validation,
    )

    assert features == []
    assert block["retrieval_eligibility"] is False
    assert "VALUE_OUTSIDE_INDICATOR_SCOPE" in block["block_reasons"]
