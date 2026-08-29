from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.develop.l2_segmentation import (  # noqa: E402
    DOWNSTREAM_L2_ELIGIBLE,
    resolve_prediction,
)
from src.news_verification.runtime.l3_role_assignment import assign_roles  # noqa: E402
from src.news_verification.runtime.l4_field_normalization import compose_fields  # noqa: E402
from src.news_verification.runtime.l5_routing import route_value  # noqa: E402
from src.news_verification.runtime.l1_value_candidates import iter_sentence_spans  # noqa: E402


def _missing_prediction() -> dict:
    return {"sentences": []}


def test_birth_count_missing_sentence_is_repaired_from_exact_registry_and_l1_value():
    article = "지난해 출생아 수는 25만4341명이다."
    result = resolve_prediction(article, _missing_prediction(), sentence_span_iterator=iter_sentence_spans)

    assert result["canonical_status"] == "REPAIRED_SOURCE_EXACT"
    assert result["canonical_reason_code"] == "MISSING_SENTENCE_EXACT_INDICATOR"
    assert result["canonical_status"] in DOWNSTREAM_L2_ELIGIBLE
    assert result["missing_sentence_ids"] == []
    scope = result["sentences"][0]["indicator_scopes"][0]
    assert scope["indicator_label"] == "출생아 수"
    assert scope["source_span_text"] == "출생아 수"
    assert result["sentences"][0]["source_region"]["governing_sentence_id"] is None
    receipt = result["repair_receipts"][0]
    assert receipt["repair_reason_code"] == "MISSING_SENTENCE_EXACT_INDICATOR"
    assert receipt["value_span_text"] == "25만4341명"
    assert receipt["candidate_count"] == 1
    assert receipt["terminology_rule_id"] == "ko-stat-birth-count-spaced-v1"
    assert receipt["canonical_l2_sha256"] == result["canonical_l2_sha256"]


def test_tfr_prose_allows_intervening_words_between_exact_indicator_and_value():
    article = "증가율은 2007년 이후 18년 만에 가장 높았고, 합계출산율도 2년 연속 반등해 0.80명을 기록했다."
    result = resolve_prediction(article, _missing_prediction(), sentence_span_iterator=iter_sentence_spans)

    assert result["canonical_status"] == "REPAIRED_SOURCE_EXACT"
    assert result["sentences"][0]["indicator_scopes"][0]["source_span_text"] == "합계출산율"
    assert result["repair_receipts"][0]["value_span_text"] == "0.80명"
    assert result["repair_receipts"][0]["indicator_char_end"] < result["repair_receipts"][0]["value_char_start"]


def test_no_registry_match_is_not_found_and_does_not_enter_downstream():
    result = resolve_prediction(
        "지난해 수치는 10명이다.", _missing_prediction(), sentence_span_iterator=iter_sentence_spans
    )

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert result["canonical_reason_code"] == "MISSING_SENTENCE_EXACT_INDICATOR_NOT_FOUND"
    assert result["missing_sentence_ids"] == [0]
    assert result["sentences"] == []
    assert result["repair_receipts"][0]["candidate_count"] == 0


def test_compact_whitespace_variant_is_not_an_exact_registered_surface():
    result = resolve_prediction(
        "지난해 출생아수는 25만4341명이다.",
        _missing_prediction(),
        sentence_span_iterator=iter_sentence_spans,
    )

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert result["repair_receipts"][0]["candidate_count"] == 0


def test_repeated_indicator_or_multiple_values_is_ambiguous():
    repeated = resolve_prediction(
        "출생아 수는 10명이고 출생아 수는 11명이다.",
        _missing_prediction(),
        sentence_span_iterator=iter_sentence_spans,
    )
    multiple_values = resolve_prediction(
        "출생아 수는 10명에서 11명으로 늘었다.",
        _missing_prediction(),
        sentence_span_iterator=iter_sentence_spans,
    )

    assert repeated["canonical_status"] == "HOLD_AMBIGUOUS"
    assert repeated["repair_receipts"][0]["candidate_count"] == 2
    assert multiple_values["canonical_status"] == "HOLD_AMBIGUOUS"
    assert multiple_values["repair_receipts"][0]["value_candidate_count"] == 2


def test_missing_non_indicator_sentence_does_not_newly_hold_valid_repaired_sentence():
    result = resolve_prediction(
        "지난해 출생아 수는 25만4341명이다. 참고 수치는 10명이다.",
        _missing_prediction(),
        sentence_span_iterator=iter_sentence_spans,
    )

    assert result["canonical_status"] == "REPAIRED_SOURCE_EXACT"
    assert result["sentences"][0]["indicator_scopes"][0]["source_span_text"] == "출생아 수"
    assert result["missing_sentence_ids"] == [1]


def test_existing_hcx_prediction_is_never_overwritten_by_missing_repair():
    article = "출생아 수는 10명이다. 합계출산율은 0.80명이다."
    prediction = {
        "debug_attempt": 7,
        "sentences": [{
            "sentence_id": 0,
            "indicator_scopes": [{"indicator_label": "출생아 수", "source_span_text": "출생아수"}],
            "source_region": {},
            "period_context": {},
        }],
    }
    raw_before = deepcopy(prediction)
    result = resolve_prediction(article, prediction, sentence_span_iterator=iter_sentence_spans)

    assert result["raw_envelope"]["raw_prediction"] == raw_before
    assert result["raw_envelope"]["raw_prediction"]["debug_attempt"] == 7
    assert result["sentences"][0]["indicator_scopes"][0]["span_status"] == "UNRESOLVED"
    assert result["sentences"][1]["indicator_scopes"][0]["source_span_text"] == "합계출산율"
    assert result["canonical_status"] == "HOLD_NOT_FOUND"


def test_missing_repair_receipt_and_canonical_sha_are_deterministic():
    article = "지난해 출생아 수는 25만4341명이다."
    first = resolve_prediction(article, _missing_prediction(), sentence_span_iterator=iter_sentence_spans)
    second = resolve_prediction(article, _missing_prediction(), sentence_span_iterator=iter_sentence_spans)

    assert first["canonical_l2_sha256"] == second["canonical_l2_sha256"]
    assert first["repair_receipts"] == second["repair_receipts"]
    assert first["raw_envelope"] == second["raw_envelope"]


def test_invented_indicator_label_becomes_clarification_and_preserves_l1_period():
    article = "2025년 출생아 수는 25만4341명이다."
    result = resolve_prediction(
        article,
        {
            "sentences": [{
                "sentence_id": 0,
                "indicator_scopes": [{
                    "indicator_label": "인구 성장률",
                    "source_span_text": article,
                }],
                "source_region": {},
                "period_context": {"period_raw": "2025년"},
            }],
        },
        sentence_span_iterator=iter_sentence_spans,
    )

    assert result["canonical_status"] == "L2_CLARIFICATION_REQUIRED"
    assert result["canonical_reason_code"] == "INDICATOR_EVIDENCE_MISSING"
    assert result["sentences"][0]["indicator_scopes"][0]["indicator_label"] == ""
    assert result["sentences"][0]["field_states"]["indicator"]["state"] == "MISSING"
    assert result["sentences"][0]["period_context"] == {"period_raw": "2025년"}
    assert result["sentences"][0]["field_states"]["indicator"]["value_span_ids"]

    assignments = assign_roles(
        article, result["sentences"], sentence_span_iterator=iter_sentence_spans
    )
    assert len(assignments) == 1
    assert assignments[0]["indicator_label"] is None
    assert assignments[0]["period_raw"] == "2025년"
    normalized = compose_fields(assignments[0], published_at="2026-08-26")
    routed = route_value(normalized)
    assert routed["routing_class"] == "CLARIFICATION_REQUIRED"
    assert routed["authoritative_retrieval_authorized"] is False


def test_exact_indicator_in_a_broad_resolved_span_remains_grounded():
    article = "2025년 출생아 수는 25만4341명이다."
    result = resolve_prediction(
        article,
        {
            "sentences": [{
                "sentence_id": 0,
                "indicator_scopes": [{
                    "indicator_label": "출생아 수",
                    "source_span_text": article,
                }],
                "source_region": {},
                "period_context": {"period_raw": "2025년"},
            }],
        },
        sentence_span_iterator=iter_sentence_spans,
    )

    scope = result["sentences"][0]["indicator_scopes"][0]
    assert result["canonical_status"] == "L2_READY"
    assert scope["indicator_label"] == "출생아 수"
    assert scope["indicator_evidence_status"] == "RESOLVED"


def test_indicator_whitespace_variant_is_not_normalized_for_hcx_evidence():
    article = "2025년 출생아수는 25만4341명이다."
    result = resolve_prediction(
        article,
        {
            "sentences": [{
                "sentence_id": 0,
                "indicator_scopes": [{
                    "indicator_label": "출생아 수",
                    "source_span_text": "출생아수",
                }],
                "source_region": {},
                "period_context": {"period_raw": "2025년"},
            }],
        },
        sentence_span_iterator=iter_sentence_spans,
    )

    assert result["canonical_status"] == "L2_CLARIFICATION_REQUIRED"
    assert result["sentences"][0]["indicator_scopes"][0]["indicator_label"] == ""
    assert result["indicator_evidence_receipts"][0]["exact_label_in_sentence_count"] == 0


def test_empty_open_source_region_is_not_provided_even_with_subtype():
    article = "출생아 수는 10명이다."
    result = resolve_prediction(
        article,
        {
            "sentences": [{
                "sentence_id": 0,
                "indicator_scopes": [{
                    "indicator_label": "출생아 수",
                    "source_span_text": "출생아 수",
                }],
                "source_region": {
                    "opens_region": True,
                    "governing_sentence_id": 0,
                    "source_subtype": "잠정추산",
                    "source_span_text": "",
                },
            }],
        },
        sentence_span_iterator=iter_sentence_spans,
    )

    region = result["sentences"][0]["source_region"]
    assert region["opens_region"] is False
    assert region["governing_sentence_id"] is None
    assert region["source_subtype"] == ""
    assert region["span_status"] == "NOT_PROVIDED"
    assert result["canonical_status"] == "REPAIRED_SOURCE_NOT_PROVIDED"
    assert result["repair_receipts"][0]["repair_action"] == (
        "NORMALIZE_EMPTY_SOURCE_REGION_TO_NOT_PROVIDED"
    )


def test_nonempty_exact_source_region_is_preserved():
    article = "통계청에 따르면 출생아 수는 10명이다."
    result = resolve_prediction(
        article,
        {
            "sentences": [{
                "sentence_id": 0,
                "indicator_scopes": [{
                    "indicator_label": "출생아 수",
                    "source_span_text": "출생아 수",
                }],
                "source_region": {
                    "opens_region": True,
                    "governing_sentence_id": 0,
                    "source_subtype": "공식집계",
                    "source_span_text": "통계청",
                },
            }],
        },
        sentence_span_iterator=iter_sentence_spans,
    )

    region = result["sentences"][0]["source_region"]
    assert region["opens_region"] is True
    assert region["source_span_text"] == "통계청"
    assert region["span_status"] == "RESOLVED"
