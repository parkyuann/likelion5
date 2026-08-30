"""Deterministic extraction of explicit period spans from one sentence.

The extractor is surface-based only: it does not absolutise relative periods or
infer a period from a comparison.  It returns the longest compound period
expression available around a value.
"""

from __future__ import annotations

import re
from typing import Any


_RANGE_SEPARATOR = r"[~～-]"
_PAREN_RANGE = (
    rf"(?:\s*\(\s*\d{{1,2}}\s*{_RANGE_SEPARATOR}\s*"
    rf"\d{{1,2}}\s*(?:일|월)\s*\))?"
)
_WEEK_ORDINAL = r"(?:첫(?:째)?|둘째|셋째|넷째|다섯째|[1-5]번째|마지막)"
_RELATIVE = r"(?:올해|지난해|작년|지난달|이달)"
_WEEKDAY_RELATIVE = r"(?:이번\s*주|지난\s*주|이번주|지난주)"

# Specific compound expressions precede their shorter components.  The
# alternatives intentionally describe grammar, not article vocabulary.
_PERIOD_RE = re.compile(
    r"(?:"
    rf"(?:올(?:해)?\s*들어\s*\d{{1,2}}\s*월\s*까지)"
    rf"|(?:\d{{1,2}}\s*{_RANGE_SEPARATOR}\s*\d{{1,2}}\s*월)"
    rf"|(?:{_RELATIVE}\s*{_WEEK_ORDINAL}\s*주{_PAREN_RANGE})"
    rf"|(?:{_RELATIVE}\s*\d{{1,3}}\s*주차{_PAREN_RANGE})"
    rf"|(?:{_RELATIVE}\s*\d{{1,2}}\s*분기{_PAREN_RANGE})"
    rf"|(?:\d{{4}}\s*년\s*\d{{1,2}}\s*월\s*\d{{1,2}}\s*일)"
    rf"|(?:\d{{4}}\s*년\s*\d{{1,2}}\s*분기{_PAREN_RANGE})"
    rf"|(?:\d{{4}}\s*년\s*\d{{1,2}}\s*월)"
    rf"|(?:\d{{4}}\s*년)"
    rf"|(?:\d{{1,2}}\s*분기)"
    rf"|(?:\d{{1,3}}\s*주차{_PAREN_RANGE})"
    rf"|(?:\d{{1,2}}\s*월)"
    rf"|(?:지난달|이달)"
    rf"|(?:{_WEEKDAY_RELATIVE})"
    rf"|(?:올해|지난해|작년)"
    rf"|(?:\d{{1,2}}\s*일)"
    r")"
)

_RELATIVE_COMPARISON_TAIL_RE = re.compile(
    r"^\s*(?:같은\s*기간(?:\s*(?:보다|대비))?|(?:보다|대비))"
)
_LOWER_BOUND_TAIL_RE = re.compile(r"^\s*(?:이래|이후|부터)(?![가-힣])")


def _is_comparison_only(match: re.Match[str], sentence: str) -> bool:
    text = match.group()
    if not re.search(
        r"(?:올해|지난해|작년|지난달|이달|이번\s*주|지난\s*주|이번주|지난주)",
        text,
    ):
        return False
    return bool(_RELATIVE_COMPARISON_TAIL_RE.match(sentence[match.end():]))


def _is_historical_lower_bound(match: re.Match[str], sentence: str) -> bool:
    return bool(_LOWER_BOUND_TAIL_RE.match(sentence[match.end():]))


def _remove_overlapping_candidates(
    matches: list[re.Match[str]],
) -> list[re.Match[str]]:
    kept: list[re.Match[str]] = []
    for match in sorted(matches, key=lambda item: (item.start(), -len(item.group()))):
        if any(
            match.start() < existing.end() and existing.start() < match.end()
            for existing in kept
        ):
            continue
        kept.append(match)
    return sorted(kept, key=lambda item: item.start())


def _span(match: re.Match[str]) -> dict[str, Any]:
    raw = match.group()
    left = len(raw) - len(raw.lstrip())
    right = len(raw.rstrip())
    return {
        "raw": raw.strip(),
        "start": match.start() + left,
        "end": match.start() + right,
    }


def extract_sentence_period_span(
    sentence_text: str,
    value_char_start: int,
) -> dict[str, Any]:
    """Return the nearest explicit period with sentence-relative offsets."""
    sentence = str(sentence_text or "")
    if not isinstance(value_char_start, int):
        return {"raw": "", "start": None, "end": None}

    matches = [
        match
        for match in _PERIOD_RE.finditer(sentence)
        if not _is_comparison_only(match, sentence)
        and not _is_historical_lower_bound(match, sentence)
    ]
    matches = _remove_overlapping_candidates(matches)
    if not matches:
        return {"raw": "", "start": None, "end": None}

    before = [match for match in matches if match.start() < value_char_start]
    chosen = (
        min(before, key=lambda match: value_char_start - match.end())
        if before
        else None
    )
    if chosen is None:
        after = [match for match in matches if match.start() >= value_char_start]
        if after:
            chosen = min(after, key=lambda match: match.start() - value_char_start)
    return _span(chosen) if chosen is not None else {"raw": "", "start": None, "end": None}


def extract_sentence_period(sentence_text: str, value_char_start: int) -> str:
    """Backward-compatible string API for existing callers."""
    return str(extract_sentence_period_span(sentence_text, value_char_start)["raw"])


