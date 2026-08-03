import json

from src.claim_context_resolver import augment_article_claim_rows, build_contextual_query, resolve_claim_context


def test_resolves_direct_demonstrative_insurance_from_one_preceding_specific_term():
    sentences = ["손해보험의 계약 건수가 늘었다.", "해당 보험료는 3.1% 상승했다."]
    result = resolve_claim_context(sentences[1], sentence_index=1, article_sentences=sentences)
    assert result["status"] == "RESOLVED"
    assert result["resolved_terms"] == ["손해보험"]
    assert result["evidence"][0]["sentence_index"] == 0
    assert "손해보험" in build_contextual_query(sentences[1], result)


def test_blocks_alignment_when_multiple_insurance_referents_exist():
    sentences = ["손해보험과 생명보험의 계약 건수가 늘었다.", "보험료는 3.1% 상승했다."]
    result = resolve_claim_context(sentences[1], sentence_index=1, article_sentences=sentences)
    assert result["status"] == "REFERENT_AMBIGUOUS"
    assert set(result["candidate_terms"]) == {"손해보험", "생명보험"}
    assert result["retrieval_policy"] == "claim_only_alignment_blocked"
    assert build_contextual_query(sentences[1], result) == sentences[1]


def test_explicit_insurance_is_not_overridden_by_prior_context():
    sentences = ["손해보험 시장은 감소했다.", "치아보험 보험료는 2% 올랐다."]
    result = resolve_claim_context(sentences[1], sentence_index=1, article_sentences=sentences)
    assert result["status"] == "EXPLICIT"
    assert result["resolved_terms"] == ["치아보험"]


def test_generic_insurance_keeps_title_match_as_candidate_without_direct_reference():
    sentences = ["보험료는 3.1% 상승했다."]
    result = resolve_claim_context(sentences[0], sentence_index=0, article_sentences=sentences, article_title="손해보험 실적")
    assert result["status"] == "REFERENT_CANDIDATE"
    assert result["evidence"][0]["source"] == "article_title"


def test_article_row_audit_is_json_serialized_for_csv_round_trip():
    rows = augment_article_claim_rows(
        [{"claim_text": "해당 보험료는 3.1% 상승했다.", "sentence_index": 1}],
        article_title="보험 기사", article_sentences=["손해보험 계약이 늘었다.", "해당 보험료는 3.1% 상승했다."],
    )
    assert json.loads(rows[0]["context_resolution_json"])["status"] == "RESOLVED"
