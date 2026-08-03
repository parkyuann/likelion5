import pytest

from src.develop.validate_l2_review_ingest import (
    validate_l2_review_ingest,
)


def _context():
    return [{
        "sentence_review_id": "1-S000",
        "article_idx": "1",
        "row_kind": "자동확정",
        "scope_id": "1-SC01",
        "region_id": "1-R01",
        "disagree_flag": "",
        "reviewer_note": "",
    }]


def test_l2_ingest_accepts_same_article_references():
    human = [{
        "sentence_review_id": "1-S001",
        "article_idx": "1",
        "indicator_scopes_json": (
            '[{"scope_id":"1-SC01","attribution_type":"앞에서 상속"}]'
        ),
        "source_regions_json": "",
        "dominant_region_decision": "1-R01",
        "label_provenance": "HUMAN_CONFIRMED",
    }]

    result = validate_l2_review_ingest(human, _context())

    assert result["status"] == "VALID"
    assert result["scope_definition_count"] == 1
    assert result["region_definition_count"] == 1


def test_l2_ingest_rejects_duplicate_definition_within_article():
    human = [{
        "sentence_review_id": "1-S001",
        "article_idx": "1",
        "indicator_scopes_json": (
            '[{"scope_id":"1-SC01","attribution_type":"이 문장에서 도입"}]'
        ),
        "source_regions_json": "",
        "dominant_region_decision": "",
    }]

    with pytest.raises(ValueError, match="duplicate scope_id"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_rejects_unresolved_dominant_region_reference():
    human = [{
        "sentence_review_id": "1-S001",
        "article_idx": "1",
        "indicator_scopes_json": "",
        "source_regions_json": "",
        "dominant_region_decision": "1-R99",
    }]

    with pytest.raises(ValueError, match="unresolved dominant region"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_requires_note_for_auto_context_disagreement():
    context = _context()
    context[0]["disagree_flag"] = "이의"

    with pytest.raises(ValueError, match="requires reviewer_note"):
        validate_l2_review_ingest([], context)


SENTENCE = "빵 물가 상승률은 38.5%로 과일 물가 상승률 35.2%를 앞질렀다."


def _span_row(**overrides):
    row = {
        "sentence_review_id": "1-S001",
        "article_idx": "1",
        "text": SENTENCE,
        "value_candidate_span_ids": (
            "38.5%=s2:value_unit:2-7 | 35.2%=s2:value_unit:13-18"
        ),
        "indicator_scopes_json": "",
        "source_regions_json": "",
        "period_contexts_json": "",
        "clause_value_boundaries_json": "",
        "dominant_region_decision": "",
    }
    row.update(overrides)
    return row


def test_l2_ingest_derives_offsets_from_span_text():
    human = [_span_row(indicator_scopes_json=(
        '[{"scope_id":"1-SC02","indicator_label":"빵 물가 상승률",'
        '"source_span_text":"빵 물가 상승률",'
        '"attribution_type":"이 문장에서 도입"}]'
    ))]

    result = validate_l2_review_ingest(human, _context())

    assert result["status"] == "VALID"
    assert result["resolved_span_count"] == 1
    assert result["contract_version"] == "l2_sentence_regions_v3"


def test_l2_ingest_rejects_span_text_absent_from_sentence():
    human = [_span_row(source_regions_json=(
        '[{"region_id":"1-R02","source_subtype":"공식집계",'
        '"source_span_text":"한국은행"}]'
    ))]

    with pytest.raises(ValueError, match="not found in sentence"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_rejects_ambiguous_span_without_occurrence_index():
    human = [_span_row(indicator_scopes_json=(
        '[{"scope_id":"1-SC02","indicator_label":"물가 상승률",'
        '"source_span_text":"물가 상승률",'
        '"attribution_type":"이 문장에서 도입"}]'
    ))]

    with pytest.raises(ValueError, match="ambiguous"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_rejects_hand_entered_offsets():
    human = [_span_row(indicator_scopes_json=(
        '[{"scope_id":"1-SC02","indicator_label":"빵 물가 상승률",'
        '"source_span_text":"빵 물가 상승률","source_char_start":0,'
        '"attribution_type":"이 문장에서 도입"}]'
    ))]

    with pytest.raises(ValueError, match="must not carry hand-entered"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_rejects_unknown_target_value_span_id():
    human = [_span_row(clause_value_boundaries_json=(
        '[{"scope_id":"1-SC02","boundary_type":"값",'
        '"target_value_span_ids":["s9:value_unit:0-3"]}]'
    ))]

    with pytest.raises(ValueError, match="unknown target_value_span_ids"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_rejects_boundary_pointing_at_absent_scope():
    human = [_span_row(
        indicator_scopes_json=(
            '[{"scope_id":"1-SC02","indicator_label":"빵 물가 상승률",'
            '"source_span_text":"빵 물가 상승률",'
            '"attribution_type":"이 문장에서 도입"}]'
        ),
        clause_value_boundaries_json=(
            '[{"scope_id":"1-SC99","boundary_type":"값",'
            '"target_value_span_ids":["s2:value_unit:2-7"]}]'
        ),
    )]

    with pytest.raises(ValueError, match="scope missing from"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_requires_target_value_span_ids_on_boundary():
    human = [_span_row(clause_value_boundaries_json=(
        '[{"scope_id":"1-SC02","boundary_type":"값"}]'
    ))]

    with pytest.raises(ValueError, match="requires target_value_span_ids"):
        validate_l2_review_ingest(human, _context())


def test_l2_ingest_accepts_boundary_clause_text_and_offered_span_id():
    human = [_span_row(
        indicator_scopes_json=(
            '[{"scope_id":"1-SC02","indicator_label":"빵 물가 상승률",'
            '"source_span_text":"빵 물가 상승률",'
            '"attribution_type":"이 문장에서 도입"}]'
        ),
        clause_value_boundaries_json=(
            '[{"scope_id":"1-SC02","boundary_type":"절",'
            '"clause_text":"빵 물가 상승률은 38.5%로",'
            '"target_value_span_ids":["s2:value_unit:2-7"]}]'
        ),
    )]

    result = validate_l2_review_ingest(human, _context())

    assert result["status"] == "VALID"


def test_l2_ingest_exempts_inherited_scope_from_span_requirement():
    """An inherited scope is introduced elsewhere, so it owns no local span."""
    human = [_span_row(indicator_scopes_json=(
        '[{"scope_id":"1-SC01","indicator_label":"빵 물가 상승률",'
        '"attribution_type":"앞에서 상속"}]'
    ))]

    result = validate_l2_review_ingest(human, _context())

    assert result["status"] == "VALID"
    assert result["resolved_span_count"] == 0


def test_l2_ingest_resolves_inherited_scope_span_when_supplied():
    human = [_span_row(indicator_scopes_json=(
        '[{"scope_id":"1-SC01","indicator_label":"빵 물가 상승률",'
        '"source_span_text":"빵 물가 상승률",'
        '"attribution_type":"앞에서 상속"}]'
    ))]

    result = validate_l2_review_ingest(human, _context())

    assert result["resolved_span_count"] == 1


def test_l2_ingest_still_accepts_unreviewed_baseline():
    human = [_span_row()]

    result = validate_l2_review_ingest(human, _context())

    assert result["status"] == "VALID"
    assert result["resolved_span_count"] == 0
