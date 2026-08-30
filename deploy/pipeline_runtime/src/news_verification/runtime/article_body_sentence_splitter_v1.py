"""Deterministic article-body sentence inventory used by the trace pipeline.

The legacy extractor intentionally remains the default.  This module is an
opt-in lexer: it preserves the legacy whitespace spans and only adds
zero-width boundaries where the article body has no whitespace between
sentences.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Iterator

try:
    from ..claim_extractor import iter_sentence_spans as _frozen_spans
except ImportError:  # pragma: no cover
    from claim_extractor import iter_sentence_spans as _frozen_spans

SentenceSpanIterator = Callable[[str], Iterator[tuple[int, int, int, str]]]
SPLITTER_MODE = "article_body_v1"

_URI_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_ASCII_SEGMENT = re.compile(r"[A-Za-z0-9_+\-]")
_HANGUL = lambda ch: bool(ch) and "\uac00" <= ch <= "\ud7a3"
_CLOSING = '”’」』》)]}'
_OPENING = "“‘「『《([{"


def splitter_source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _ascii_segment(text: str, index: int, direction: int) -> tuple[int, int]:
    left = right = index
    if direction < 0:
        left = index - 1
        while left >= 0 and _ASCII_SEGMENT.fullmatch(text[left]):
            left -= 1
        return left + 1, index
    right = index
    while right < len(text) and _ASCII_SEGMENT.fullmatch(text[right]):
        right += 1
    return index, right


def _protected_positions(text: str) -> set[int]:
    protected: set[int] = set()
    for pattern in (_URI_RE, _EMAIL_RE):
        for match in pattern.finditer(text):
            protected.update(range(match.start(), match.end()))
    for index, char in enumerate(text):
        if char != "." or index in protected:
            continue
        left = text[index - 1] if index else ""
        right = text[index + 1] if index + 1 < len(text) else ""
        if left.isdigit() and (right.isdigit() or _HANGUL(right)):
            protected.add(index)
            continue
        l0, l1 = _ascii_segment(text, index, -1)
        r0, r1 = _ascii_segment(text, index + 1, 1)
        left_len = l1 - l0
        right_exists = r1 > r0
        if left_len and right_exists:
            protected.add(index)
            continue
        if _HANGUL(right) and left_len >= 2:
            protected.add(index)
            continue
        if _HANGUL(right) and left_len == 1:
            before = l0 - 1
            if before >= 0 and text[before] == ".":
                p0, p1 = _ascii_segment(text, before, -1)
                if p1 > p0:
                    protected.add(index)
    return protected


def _zero_width_boundaries(text: str, protected: set[int]) -> set[int]:
    boundaries: set[int] = set()
    index = 0
    while index < len(text):
        if text[index] not in ".!?" or index in protected:
            index += 1
            continue
        start = index
        while index < len(text) and text[index] in ".!?" and index not in protected:
            index += 1
        end = index
        # A terminal run is evaluated only at its end; this also keeps an
        # ellipsis as one lexical unit.
        cursor = end
        while cursor < len(text) and text[cursor] in _CLOSING:
            cursor += 1
        opening = cursor
        while cursor < len(text) and text[cursor] in _OPENING:
            cursor += 1
        if opening > end and cursor < len(text) and _HANGUL(text[cursor]):
            boundaries.add(opening)
        elif end < len(text) and text[end] in "\"'":
            if end + 1 < len(text) and _HANGUL(text[end + 1]):
                boundaries.add(end)
        elif cursor < len(text) and _HANGUL(text[cursor]):
            boundaries.add(cursor if opening > end else end)
        index = max(index, cursor if cursor > end else end)
    return boundaries


def iter_article_body_sentence_spans(text: str) -> Iterator[tuple[int, int, int, str]]:
    """Yield ``(sentence_id, start, end, sentence)`` with exact source offsets."""
    if not isinstance(text, str):
        raise TypeError("article body must be text")
    frozen = list(_frozen_spans(text))
    if not frozen:
        return
    boundaries = _zero_width_boundaries(text, _protected_positions(text))
    sentence_id = 0
    for _old_id, start, end, _sentence in frozen:
        cuts = sorted(boundary for boundary in boundaries if start < boundary < end)
        segment_start = start
        for cut in [*cuts, end]:
            part_start = segment_start
            part_end = cut
            while part_start < part_end and text[part_start].isspace():
                part_start += 1
            while part_end > part_start and text[part_end - 1].isspace():
                part_end -= 1
            if part_start < part_end:
                yield sentence_id, part_start, part_end, text[part_start:part_end]
                sentence_id += 1
            segment_start = cut


