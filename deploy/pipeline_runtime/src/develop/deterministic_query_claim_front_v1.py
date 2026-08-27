"""Deterministic source-only front for the narrow query-only input mode.

This module consumes one source sentence and the generic L1 inventory.  It
does not know which downstream collection, selector, or value is authoritative;
it only produces the existing L2 layout shape with source spans that can be
rechecked by the unchanged span resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from .l1_value_candidates import build_span_candidates, sentence_offset_map
from .l2_span_resolver import SpanResolutionError, resolve_span


CONTRACT_VERSION = "DETERMINISTIC_QUERY_CLAIM_FRONT_V1"
QUERY_ONLY_INPUT_KIND = "QUERY_ONLY_SYNTHETIC_CLAIM"
UNSUPPORTED_PREFIX = "QUERY_CLAIM_FRONT_UNSUPPORTED:"
_EXPLICIT_YEAR = re.compile(r"(?<!\d)\d{4}년(?!\d)")
_SUBJECT_PARTICLE = re.compile(r"(?P<head>.+?)(?P<particle>은|는|이|가)\s*$")
_TRAILING_PUNCTUATION = re.compile(r"[\s,，:：;；]+$")


class QueryClaimFrontError(ValueError):
    """A finite, pre-external-call query-front rejection."""


@dataclass(frozen=True)
class QueryClaimFront:
    article_idx: str
    query: str
    sentence_id: int
    value_text: str
    value_unit: str
    value_char_start: int
    value_char_end: int
    indicator: str
    indicator_source_span: str
    indicator_source_start: int
    indicator_source_end: int
    period_raw: str
    period_char_start: int | None
    period_char_end: int | None

    def prediction(self) -> dict[str, Any]:
        """Return only the pre-existing L2 layout fields."""

        indicator_scope: dict[str, Any] = {
            "indicator_label": self.indicator,
            "source_span_text": self.indicator_source_span,
            "source_char_start": self.indicator_source_start,
            "source_char_end": self.indicator_source_end,
            "span_status": "RESOLVED",
        }
        period: dict[str, Any] = {
            "period_raw": self.period_raw,
            "source_span_text": self.period_raw,
        }
        if self.period_char_start is not None and self.period_char_end is not None:
            period.update({
                "source_char_start": self.period_char_start,
                "source_char_end": self.period_char_end,
                "span_status": "RESOLVED",
            })
        return {
            "article_idx": self.article_idx,
            "sentence_id": self.sentence_id,
            "text": self.query,
            "indicator_scopes": [indicator_scope],
            "source_region": {
                "opens_region": False,
                "governing_sentence_id": None,
                "source_subtype": "",
                "source_span_text": "",
            },
            "period_context": period,
        }

    def provenance(self) -> dict[str, Any]:
        """Return generic offsets for the stage manifest, without downstream facts."""

        provenance_record: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "article_idx": self.article_idx,
            "sentence_id": self.sentence_id,
            "value": {
                "text": self.value_text,
                "unit": self.value_unit,
                "char_start": self.value_char_start,
                "char_end": self.value_char_end,
            },
            "indicator": {
                "label": self.indicator,
                "source_span_text": self.indicator_source_span,
                "char_start": self.indicator_source_start,
                "char_end": self.indicator_source_end,
            },
            "period": {
                "raw": self.period_raw,
                "char_start": self.period_char_start,
                "char_end": self.period_char_end,
            },
        }
        return provenance_record


def _reject(code: str) -> None:
    raise QueryClaimFrontError(f"{UNSUPPORTED_PREFIX}{code}")


def _normal_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _offset(item: Mapping[str, Any], name: str, default: int = -1) -> int:
    value = item.get(name)
    return default if value is None else int(value)


def _remove_l1_context(
    source: str,
    *,
    source_start: int,
    source_end: int,
    candidates: list[Mapping[str, Any]],
) -> str:
    """Remove exact L1 time/dimension surfaces from a noun phrase."""

    pieces: list[str] = []
    cursor = source_start
    for candidate in sorted(
        (
            item for item in candidates
            if item.get("kind") in {"time", "dimension"}
            and source_start <= _offset(item, "char_start")
            and _offset(item, "char_end") <= source_end
        ),
        key=lambda item: (_offset(item, "char_start", 0), _offset(item, "char_end", 0)),
    ):
        start = _offset(candidate, "char_start")
        end = _offset(candidate, "char_end")
        if start < cursor or end <= start:
            continue
        pieces.append(source[cursor - source_start:start - source_start])
        cursor = end
    pieces.append(source[cursor - source_start:source_end - source_start])
    return _normal_space("".join(pieces))


def _validate_span(query: str, start: int, end: int, expected: str, code: str) -> None:
    if start < 0 or end <= start or end > len(query) or query[start:end] != expected:
        _reject(code)


def parse_query_claim_front(query: str, *, article_idx: str = "query-only") -> QueryClaimFront:
    """Parse one supported query-only sentence using generic L1 spans."""

    query = str(query or "").strip()
    if not query:
        _reject("EMPTY_QUERY")
    sentences = sentence_offset_map(query)
    if len(sentences) != 1:
        _reject("MULTI_SENTENCE")
    sentence = sentences[0]
    if str(sentence.get("text") or "") != query:
        _reject("SENTENCE_SPAN_MISMATCH")

    candidates = build_span_candidates(query)
    values = [item for item in candidates if item.get("kind") == "value_unit"]
    if len(values) != 1:
        _reject("VALUE_UNIT_COUNT")
    value = values[0]
    value_start = _offset(value, "char_start")
    value_end = _offset(value, "char_end")
    value_text = str(value.get("text") or "")
    value_unit = str(value.get("unit") or "")
    _validate_span(query, value_start, value_end, value_text, "VALUE_SPAN_MISMATCH")
    if not value_text or not value_unit:
        _reject("VALUE_UNIT_MALFORMED")

    prefix = query[:value_start]
    prefix = _TRAILING_PUNCTUATION.sub("", prefix)
    match = _SUBJECT_PARTICLE.search(prefix)
    if match is None:
        _reject("INDICATOR_PARTICLE_REQUIRED")
    head = _normal_space(match.group("head"))
    if not head:
        _reject("INDICATOR_EMPTY")
    indicator_start = match.start("head")
    indicator_end = match.end("head")
    indicator_source_span = query[indicator_start:indicator_end]
    _validate_span(query, indicator_start, indicator_end, indicator_source_span, "INDICATOR_SPAN_MISMATCH")

    indicator = _remove_l1_context(
        query,
        source_start=indicator_start,
        source_end=indicator_end,
        candidates=candidates,
    )
    if not indicator:
        _reject("INDICATOR_EMPTY_AFTER_L1_CONTEXT")
    if any(char in indicator for char in "\n\r"):
        _reject("INDICATOR_MULTILINE")

    explicit_periods = [
        item for item in candidates
        if item.get("kind") == "time" and _EXPLICIT_YEAR.fullmatch(str(item.get("text") or "").strip())
    ]
    if len(explicit_periods) > 1:
        _reject("MULTIPLE_EXPLICIT_PERIOD")
    period_raw = ""
    period_start: int | None = None
    period_end: int | None = None
    if explicit_periods:
        period = explicit_periods[0]
        period_raw = str(period.get("text") or "").strip()
        period_start = _offset(period, "char_start")
        period_end = _offset(period, "char_end")
        _validate_span(query, period_start, period_end, period_raw, "PERIOD_SPAN_MISMATCH")

    front = QueryClaimFront(
        article_idx=str(article_idx or "query-only"),
        query=query,
        sentence_id=int(sentence["sentence_id"]),
        value_text=value_text,
        value_unit=value_unit,
        value_char_start=value_start,
        value_char_end=value_end,
        indicator=indicator,
        indicator_source_span=indicator_source_span,
        indicator_source_start=indicator_start,
        indicator_source_end=indicator_end,
        period_raw=period_raw,
        period_char_start=period_start,
        period_char_end=period_end,
    )
    prediction = front.prediction()
    try:
        resolved = resolve_span(query, indicator_source_span)
    except SpanResolutionError as exc:
        _reject("INDICATOR_SOURCE_SPAN_UNRESOLVED")
    if resolved.get("source_char_start") != indicator_start or resolved.get("source_char_end") != indicator_end:
        _reject("INDICATOR_OFFSET_MISMATCH")
    if prediction["period_context"].get("period_raw"):
        if prediction["period_context"].get("source_span_text") != period_raw:
            _reject("PERIOD_SUBSTRING_MISMATCH")
    if prediction["text"] != query:
        _reject("OUTPUT_SOURCE_MISMATCH")
    return front


def build_query_claim_front(query: str, *, article_idx: str = "query-only") -> dict[str, Any]:
    """Return the L2 prediction plus generic front provenance."""

    front = parse_query_claim_front(query, article_idx=article_idx)
    return {
        "prediction": front.prediction(),
        "provenance": front.provenance(),
    }


def build_deterministic_query_claim_front(query: str, *, article_idx: str = "query-only") -> dict[str, Any]:
    """Compatibility name for callers that want the full front record."""

    return build_query_claim_front(query, article_idx=article_idx)


def build_prediction(query: str, *, article_idx: str = "query-only") -> dict[str, Any]:
    """Return only the existing prediction row used by the downstream stack."""

    return parse_query_claim_front(query, article_idx=article_idx).prediction()


__all__ = [
    "CONTRACT_VERSION",
    "QUERY_ONLY_INPUT_KIND",
    "QueryClaimFront",
    "QueryClaimFrontError",
    "build_deterministic_query_claim_front",
    "build_prediction",
    "build_query_claim_front",
    "parse_query_claim_front",
]
