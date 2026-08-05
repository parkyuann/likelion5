"""Derive L2 review span offsets from human-entered span text.

팀 인계: 근거 텍스트를 결정론적인 문자 span으로 변환한다. 사람 검토자와 모델이
문자 offset을 직접 입력하지 않도록 하는 모듈이다.

Contract v2 required the reviewer to type ``source_char_start`` and
``source_char_end`` for every indicator scope, source region and period
context.  Counting those offsets by hand inside a spreadsheet cell is both
slow and unverifiable: the v2 ingest validator never checked them, so a
miscounted offset produced gold that passed validation while pointing at the
wrong characters.

Contract v3 keeps the reviewer responsible for the judgement (which span is
the evidence) and moves the bookkeeping here.  The reviewer supplies
``source_span_text``; this module locates it in the sentence and fails loudly
when the text is absent or ambiguous instead of guessing.
"""

from __future__ import annotations

from typing import Any


class SpanResolutionError(ValueError):
    """Raised when span text cannot be resolved to exactly one offset pair."""


def _occurrences(sentence_text: str, span_text: str) -> list[int]:
    starts: list[int] = []
    cursor = sentence_text.find(span_text)
    while cursor >= 0:
        starts.append(cursor)
        cursor = sentence_text.find(span_text, cursor + 1)
    return starts


def resolve_span(
    sentence_text: str,
    span_text: str,
    occurrence_index: object = None,
) -> dict[str, Any]:
    """Return the offsets of ``span_text`` inside ``sentence_text``.

    ``occurrence_index`` is only needed when the same text appears more than
    once.  Ambiguity is an error rather than a silent choice of the first hit,
    because picking the wrong occurrence is exactly the failure the hand-typed
    offsets were meant to avoid.
    """
    if not isinstance(sentence_text, str) or not sentence_text:
        raise SpanResolutionError("sentence text is empty")
    if not isinstance(span_text, str) or not span_text.strip():
        raise SpanResolutionError("source_span_text is empty")
    starts = _occurrences(sentence_text, span_text)
    if not starts:
        raise SpanResolutionError(
            f"source_span_text not found in sentence: {span_text!r}"
        )
    if len(starts) == 1:
        index = 0
    elif occurrence_index in (None, ""):
        raise SpanResolutionError(
            f"source_span_text is ambiguous ({len(starts)} occurrences): "
            f"{span_text!r}; supply occurrence_index or a longer span"
        )
    else:
        try:
            index = int(occurrence_index)
        except (TypeError, ValueError) as exc:
            raise SpanResolutionError(
                f"occurrence_index must be an integer: {occurrence_index!r}"
            ) from exc
        if not 0 <= index < len(starts):
            raise SpanResolutionError(
                f"occurrence_index {index} out of range for {span_text!r} "
                f"({len(starts)} occurrences)"
            )
    start = starts[index]
    return {
        "source_span_text": span_text,
        "source_char_start": start,
        "source_char_end": start + len(span_text),
        "occurrence_index": index,
        "match_count": len(starts),
        "offset_provenance": "DERIVED_FROM_SPAN_TEXT",
    }


SPAN_BEARING_FIELDS = (
    "indicator_scopes_json",
    "source_regions_json",
    "period_contexts_json",
)


def resolve_entry_span(
    sentence_text: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Return ``entry`` with derived offsets attached."""
    resolved = resolve_span(
        sentence_text,
        entry.get("source_span_text"),
        entry.get("occurrence_index"),
    )
    merged = dict(entry)
    merged.update(resolved)
    return merged


def parse_value_candidate_span_ids(rendered: object) -> list[str]:
    """Return the span IDs offered to the reviewer for one sentence.

    The sheet renders them as ``값=span_id | 값=span_id`` so the reviewer can
    see which number each ID belongs to.
    """
    if not isinstance(rendered, str) or not rendered.strip():
        return []
    span_ids = []
    for chunk in rendered.split("|"):
        _, separator, span_id = chunk.rpartition("=")
        if separator and span_id.strip():
            span_ids.append(span_id.strip())
    return span_ids
