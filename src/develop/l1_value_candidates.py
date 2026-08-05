"""L1 — extract value, time and dimension candidates from an article.

팀 인계: 현재 주장 파이프라인의 첫 단계다. 기사에 등장하는 모든 수치 후보와
원문 span을 보존하며, 해당 값이 KOSIS 검증 대상인지는 판단하지 않는다.

CLAUDE.md 6.4절 names this a layer and freezes it at recall 0.968, but it had
no module: the code sat inside ``article_claim_pipeline.py`` alongside the
r11~r16i contract that 6.5절 forbids extending.  Every layer above it therefore
imported 4098 lines of that contract — and its HTTP client — to reach two
functions.

Nothing here is changed from what was measured.  This is a move, so the layer
boundary that 6.4절 declares also exists in the code.

The extractor deliberately does not infer a claim or pair values with
dimensions.  It exposes exact substrings with stable IDs; deciding what a value
means belongs to L3.
"""

from __future__ import annotations

import re
from typing import Any

try:
    from ..claim_extractor import VALUE_UNIT_RE, canon_unit, iter_sentence_spans
    from .lexical_rules import (
        _AGE_RANGE_RE,
        _CONSECUTIVE_DURATION_VALUE_RE,
        _INDEX_LEVEL_VALUE_RE,
        _INDUSTRY_STOPWORDS,
        _PERIOD_CANDIDATE_RE,
        _REGION_STOPWORDS,
        _RELATIVE_PERIOD_RANGE_VALUE_RE,
        _SPAN_DIMENSION_PATTERNS,
    )
except ImportError:  # pragma: no cover - direct script execution
    from claim_extractor import VALUE_UNIT_RE, canon_unit, iter_sentence_spans
    from lexical_rules import (  # type: ignore
        _AGE_RANGE_RE,
        _CONSECUTIVE_DURATION_VALUE_RE,
        _INDEX_LEVEL_VALUE_RE,
        _INDUSTRY_STOPWORDS,
        _PERIOD_CANDIDATE_RE,
        _REGION_STOPWORDS,
        _RELATIVE_PERIOD_RANGE_VALUE_RE,
        _SPAN_DIMENSION_PATTERNS,
    )

_TIME_UNITS = {"년", "월", "분기", "개월", "일"}


def sentence_offset_map(article_text: str) -> list[dict[str, Any]]:
    return [
        {"sentence_id": index, "char_start": start, "char_end": end, "text": sentence}
        for index, start, end, sentence in iter_sentence_spans(article_text)
    ]


def _span_record(*, sentence: dict[str, Any], kind: str, ordinal: int, start: int, end: int,
                 dimension_type: str | None = None, value: str | None = None,
                 unit: str | None = None) -> dict[str, Any]:
    """Return an immutable-source candidate. IDs are stable for the same sentence map."""
    record: dict[str, Any] = {
        # Local source offsets keep the ID stable when callers extract the
        # whole article first and later rebuild only the selected sentences.
        "span_id": f"s{sentence['sentence_id']}:{kind}:{start}-{end}",
        "kind": kind,
        "sentence_id": sentence["sentence_id"],
        "char_start": sentence["char_start"] + start,
        "char_end": sentence["char_start"] + end,
        "text": sentence["text"][start:end],
    }
    if dimension_type:
        record["dimension_type"] = dimension_type
    if value is not None:
        record["value"] = value
    if unit is not None:
        record["unit"] = unit
    return record


def _keep_dimension_candidate(dimension_type: str, text: str, sentence_text: str, end: int) -> bool:
    """Reject lexical lookalikes before they become HCX-selectable dimension IDs."""
    if dimension_type == "지역":
        if text in _REGION_STOPWORDS or text.endswith("기구"):
            return False
        if text.endswith(("하면", "되면", "으면")):
            return False
        if text.endswith(("인구", "이동")):
            return False
        # '경기를 경유'의 경기는 지역명이 아니라 경제 상황을 뜻한다.
        return not (text == "경기" and sentence_text[end:].lstrip().startswith("를 경유"))
    if dimension_type == "산업":
        return text not in _INDUSTRY_STOPWORDS
    return True


def build_span_candidates(article_text: str, sentence_ids: object = None) -> list[dict[str, Any]]:
    """Extract source-grounded value/unit, time and dimension candidates with stable IDs.

    This deliberately does not infer a claim or pair values to dimensions. It only
    exposes exact substrings for the constrained HCX binding stage.
    """
    wanted = {item for item in sentence_ids if isinstance(item, int)} if isinstance(sentence_ids, list) else None
    candidates: list[dict[str, Any]] = []
    for sentence in sentence_offset_map(article_text):
        if wanted is not None and sentence["sentence_id"] not in wanted:
            continue
        text = sentence["text"]
        occupied_dimension_spans: list[tuple[int, int]] = []
        for dimension_type, pattern in _SPAN_DIMENSION_PATTERNS.items():
            for ordinal, match in enumerate(pattern.finditer(text)):
                if not _keep_dimension_candidate(dimension_type, match.group(), text, match.end()):
                    continue
                candidates.append(_span_record(sentence=sentence, kind="dimension", ordinal=len(candidates),
                                               start=match.start(), end=match.end(), dimension_type=dimension_type))
                occupied_dimension_spans.append(match.span())
        for match in VALUE_UNIT_RE.finditer(text):
            # Calendar years and age labels are period/dimension candidates, not observations.
            in_age_dimension = any(start <= match.start() and match.end() <= end for start, end in occupied_dimension_spans)
            if _AGE_RANGE_RE.search(text[max(0, match.start() - 4):match.end() + 4]) or in_age_dimension:
                continue
            if canon_unit(match.group("unit")) in _TIME_UNITS:
                continue
            candidates.append(_span_record(sentence=sentence, kind="value_unit", ordinal=len(candidates),
                                           start=match.start(), end=match.end(), value=match.group("value"),
                                           unit=canon_unit(match.group("unit"))))
        # Consecutive-month and explicit quarter-range statements are derived
        # statistical claims rather than calendar labels.  They were omitted
        # by the generic time-unit exclusion, which made article-level recall
        # impossible even when the source states the duration directly.
        derived_value_spans: set[tuple[int, int]] = set()
        for match in _CONSECUTIVE_DURATION_VALUE_RE.finditer(text):
            following_text = text[match.end():]
            if (
                VALUE_UNIT_RE.search(following_text)
                or re.search(
                    r"\d{4}년[^.]{0,48}이후\s*가장\s*긴\s*기간",
                    text,
                )
            ):
                continue
            candidates.append(_span_record(
                sentence=sentence,
                kind="value_unit",
                ordinal=len(candidates),
                start=match.start(),
                end=match.end(),
                value=match.group("value"),
                unit="개월 연속",
            ))
            derived_value_spans.add(match.span())
        for match in _RELATIVE_PERIOD_RANGE_VALUE_RE.finditer(text):
            if match.span() in derived_value_spans:
                continue
            candidates.append(_span_record(
                sentence=sentence,
                kind="value_unit",
                ordinal=len(candidates),
                start=match.start(),
                end=match.end(),
                value=match.group(),
                unit="기간",
            ))
        # Index levels are frequently written without an explicit unit (e.g.
        # '생산지수는 111.7로').  Preserve only this narrow, source-grounded
        # construction as an inferred-index candidate; arbitrary bare numbers
        # remain unavailable to HCX binding.
        for match in _INDEX_LEVEL_VALUE_RE.finditer(text):
            value_start, value_end = match.start("value"), match.end("value")
            candidates.append(_span_record(sentence=sentence, kind="value_unit", ordinal=len(candidates),
                                           start=value_start, end=value_end, value=match.group("value"), unit="지수"))
        seen_periods: set[tuple[int, int]] = set()
        for match in _PERIOD_CANDIDATE_RE.finditer(text):
            if match.span() not in seen_periods:
                candidates.append(_span_record(sentence=sentence, kind="time", ordinal=len(candidates),
                                               start=match.start(), end=match.end()))
                seen_periods.add(match.span())
    return candidates
