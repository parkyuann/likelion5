import pytest

from src.develop.l2_span_resolver import (
    SpanResolutionError,
    parse_value_candidate_span_ids,
    resolve_span,
)


SENTENCE = "빵 물가 상승률은 38.5%로 과일 물가 상승률 35.2%를 앞질렀다."


def test_resolve_span_returns_derived_offsets():
    resolved = resolve_span(SENTENCE, "빵 물가 상승률")

    assert resolved["source_char_start"] == 0
    assert SENTENCE[
        resolved["source_char_start"]:resolved["source_char_end"]
    ] == "빵 물가 상승률"
    assert resolved["match_count"] == 1
    assert resolved["offset_provenance"] == "DERIVED_FROM_SPAN_TEXT"


def test_resolve_span_rejects_text_absent_from_sentence():
    with pytest.raises(SpanResolutionError, match="not found in sentence"):
        resolve_span(SENTENCE, "실업률")


def test_resolve_span_rejects_ambiguous_text_without_occurrence_index():
    with pytest.raises(SpanResolutionError, match="ambiguous"):
        resolve_span(SENTENCE, "물가 상승률")


def test_resolve_span_accepts_ambiguous_text_with_occurrence_index():
    first = resolve_span(SENTENCE, "물가 상승률", occurrence_index=0)
    second = resolve_span(SENTENCE, "물가 상승률", occurrence_index=1)

    assert first["match_count"] == 2
    assert second["source_char_start"] > first["source_char_start"]
    assert SENTENCE[
        second["source_char_start"]:second["source_char_end"]
    ] == "물가 상승률"


def test_resolve_span_rejects_out_of_range_occurrence_index():
    with pytest.raises(SpanResolutionError, match="out of range"):
        resolve_span(SENTENCE, "물가 상승률", occurrence_index=5)


def test_resolve_span_rejects_empty_span_text():
    with pytest.raises(SpanResolutionError, match="source_span_text is empty"):
        resolve_span(SENTENCE, "   ")


def test_parse_value_candidate_span_ids_reads_rendered_pairs():
    rendered = "38.5%=s2:value_unit:2-7 | 35.2%=s2:value_unit:13-18"

    assert parse_value_candidate_span_ids(rendered) == [
        "s2:value_unit:2-7",
        "s2:value_unit:13-18",
    ]


def test_parse_value_candidate_span_ids_handles_empty():
    assert parse_value_candidate_span_ids("") == []
    assert parse_value_candidate_span_ids(None) == []
