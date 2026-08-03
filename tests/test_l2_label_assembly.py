import json

import pytest

from src.develop.l2_label_assembly import (
    allocate_id,
    assemble_review_row,
    existing_ids,
    occurrence_index_for_offset,
    region_choices,
    scope_choices,
)

SENTENCE = "빵 물가 상승률은 38.5%로 과일 물가 상승률 35.2%를 앞질렀다."


def _row():
    return {
        "sentence_review_id": "380-S002",
        "article_idx": "380",
        "text": SENTENCE,
        "published_at": "2025-10-09",
        "value_candidate_span_ids": (
            "38.5%=s2:value_unit:2-7 | 35.2%=s2:value_unit:13-18"
        ),
        "review_status": "미검토",
        "label_provenance": "UNREVIEWED",
    }


def _context():
    return [{
        "sentence_review_id": "380-S000",
        "article_idx": "380",
        "row_kind": "자동확정",
        "scope_id": "380-SC01",
        "region_id": "380-R01",
        "indicator_label": "빵 물가 상승률",
        "source_subtype": "공식집계",
        "text": "리드 문장.",
    }]


def test_occurrence_index_uses_selection_offset():
    first = SENTENCE.find("물가 상승률")
    second = SENTENCE.find("물가 상승률", first + 1)

    assert occurrence_index_for_offset(SENTENCE, "물가 상승률", first) == 0
    assert occurrence_index_for_offset(SENTENCE, "물가 상승률", second) == 1


def test_allocate_id_skips_taken_numbers():
    assert allocate_id("380", "scope", {"380-SC01", "380-SC02"}) == "380-SC03"
    assert allocate_id("380", "region", set()) == "380-R01"


def test_existing_ids_reads_context_and_saved_rows():
    saved = [{
        "article_idx": "380",
        "indicator_scopes_json": json.dumps(
            [{"scope_id": "380-SC04", "attribution_type": "이 문장에서 도입"}],
            ensure_ascii=False,
        ),
    }]

    found = existing_ids("380", _context(), saved, "scope")

    assert found == {"380-SC01", "380-SC04"}


def test_existing_ids_ignores_inherited_references():
    saved = [{
        "article_idx": "380",
        "indicator_scopes_json": json.dumps(
            [{"scope_id": "380-SC01", "attribution_type": "앞에서 상속"}],
            ensure_ascii=False,
        ),
    }]

    assert existing_ids("380", [], saved, "scope") == set()


def test_assemble_allocates_ids_and_builds_boundary():
    decision = {
        "scopes": [{
            "indicator_label": "빵 물가 상승률",
            "source_span_text": "빵 물가 상승률",
            "source_char_start": 0,
            "attribution_type": "이 문장에서 도입",
            "value_span_ids": ["s2:value_unit:2-7"],
        }],
        "review_status": "검토완료",
    }

    updated = assemble_review_row(_row(), decision, {"380-SC01"}, set())

    scopes = json.loads(updated["indicator_scopes_json"])
    boundaries = json.loads(updated["clause_value_boundaries_json"])
    assert scopes[0]["scope_id"] == "380-SC02"
    assert "source_char_start" not in scopes[0]
    assert boundaries[0]["target_value_span_ids"] == ["s2:value_unit:2-7"]
    assert boundaries[0]["boundary_type"] == "값"
    assert updated["label_provenance"] == "HUMAN_CONFIRMED"


def test_assemble_records_occurrence_for_repeated_span():
    second = SENTENCE.find("물가 상승률", SENTENCE.find("물가 상승률") + 1)
    decision = {
        "scopes": [{
            "indicator_label": "과일 물가 상승률",
            "source_span_text": "물가 상승률",
            "source_char_start": second,
            "attribution_type": "이 문장에서 도입",
            "value_span_ids": [],
        }],
    }

    updated = assemble_review_row(_row(), decision, set(), set())

    assert json.loads(updated["indicator_scopes_json"])[0]["occurrence_index"] == 1


def test_assemble_rejects_value_id_not_offered_on_this_sentence():
    decision = {
        "scopes": [{
            "indicator_label": "빵 물가 상승률",
            "source_span_text": "빵 물가 상승률",
            "source_char_start": 0,
            "attribution_type": "이 문장에서 도입",
            "value_span_ids": ["s9:value_unit:0-3"],
        }],
    }

    with pytest.raises(ValueError, match="값 후보가 아닙니다"):
        assemble_review_row(_row(), decision, set(), set())


def test_assemble_requires_span_for_introduced_scope():
    decision = {"scopes": [{
        "indicator_label": "빵 물가 상승률",
        "attribution_type": "이 문장에서 도입",
    }]}

    with pytest.raises(ValueError, match="근거 표현"):
        assemble_review_row(_row(), decision, set(), set())


def test_assemble_allows_inherited_scope_without_span():
    decision = {
        "scopes": [{
            "scope_id": "380-SC01",
            "indicator_label": "빵 물가 상승률",
            "attribution_type": "앞에서 상속",
        }],
        "dominant_region_decision": "380-R01",
        "review_status": "검토완료",
    }

    updated = assemble_review_row(_row(), decision, {"380-SC01"}, {"380-R01"})

    scopes = json.loads(updated["indicator_scopes_json"])
    assert scopes[0]["attribution_type"] == "앞에서 상속"
    assert "source_span_text" not in scopes[0]
    assert updated["dominant_region_decision"] == "380-R01"


def test_assemble_inheritance_only_row_needs_one_dropdown():
    """The 59 context rows are complete with a dominant-region choice alone."""
    decision = {
        "dominant_region_decision": "지배 없음",
        "review_status": "검토완료",
    }

    updated = assemble_review_row(_row(), decision, set(), set())

    assert updated["dominant_region_decision"] == "지배 없음"
    assert updated["indicator_scopes_json"] == ""
    assert updated["label_provenance"] == "HUMAN_CONFIRMED"


def test_assemble_reallocates_scope_id_borrowed_from_another_row():
    """Switching 상속 → 도입 must not carry another sentence's scope_id."""
    decision = {
        "scopes": [{
            "scope_id": "380-SC01",
            "indicator_label": "완전히 다른 지표",
            "source_span_text": "빵 물가 상승률",
            "source_char_start": 0,
            "attribution_type": "이 문장에서 도입",
        }],
    }

    updated = assemble_review_row(_row(), decision, {"380-SC01"}, set())

    scope_id = json.loads(updated["indicator_scopes_json"])[0]["scope_id"]
    assert scope_id != "380-SC01"
    assert scope_id == "380-SC02"


def test_assemble_keeps_inherited_reference_to_existing_scope():
    decision = {"scopes": [{
        "scope_id": "380-SC01",
        "indicator_label": "빵 물가 상승률",
        "attribution_type": "앞에서 상속",
    }]}

    updated = assemble_review_row(_row(), decision, {"380-SC01"}, set())

    assert json.loads(updated["indicator_scopes_json"])[0]["scope_id"] == "380-SC01"


def test_assemble_gives_each_indicator_in_a_row_its_own_scope_id():
    """One ID must never name two indicators inside the same sentence."""
    span = "빵 물가 상승률"
    decision = {"scopes": [
        {
            "scope_id": "380-SC02",
            "indicator_label": "빵 가격 상승률",
            "source_span_text": span,
            "source_char_start": 0,
            "attribution_type": "이 문장에서 도입",
            "value_span_ids": ["s2:value_unit:2-7"],
        },
        {
            "scope_id": "380-SC02",
            "indicator_label": "과일 가격 상승률",
            "source_span_text": span,
            "source_char_start": 0,
            "attribution_type": "이 문장에서 도입",
            "value_span_ids": ["s2:value_unit:13-18"],
        },
    ]}

    updated = assemble_review_row(_row(), decision, set(), set())

    scopes = json.loads(updated["indicator_scopes_json"])
    bounds = json.loads(updated["clause_value_boundaries_json"])
    assert scopes[0]["scope_id"] != scopes[1]["scope_id"]
    assert {b["scope_id"] for b in bounds} == {
        scopes[0]["scope_id"], scopes[1]["scope_id"]
    }


def test_assemble_accepts_undecided_dominance():
    decision = {
        "dominant_region_decision": "판단 불가",
        "reviewer_note": "문맥으로도 출처를 정하지 못함",
        "review_status": "검토완료",
    }

    updated = assemble_review_row(_row(), decision, set(), set())

    assert updated["dominant_region_decision"] == "판단 불가"


def test_assemble_builds_region_with_allocated_id():
    decision = {"regions": [{
        "source_subtype": "민간조사",
        "source_span_text": "과일 물가 상승률",
        "source_char_start": SENTENCE.find("과일 물가 상승률"),
    }]}

    updated = assemble_review_row(_row(), decision, set(), {"380-R01"})

    regions = json.loads(updated["source_regions_json"])
    assert regions[0]["region_id"] == "380-R02"
    assert regions[0]["source_subtype"] == "민간조사"


def test_region_and_scope_choices_expose_article_options():
    regions = region_choices("380", _context(), [])
    scopes = scope_choices("380", _context(), [])

    assert regions[0]["region_id"] == "380-R01"
    assert regions[0]["origin"] == "자동확정"
    assert scopes[0]["indicator_label"] == "빵 물가 상승률"
