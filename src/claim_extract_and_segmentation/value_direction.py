"""Extract the grammatical direction governing a numeric value."""

from __future__ import annotations

import re

DIRECTIONS = ("INCREASE", "DECREASE")

_DECREASE = ("감소", "하락", "줄", "축소", "떨어", "내렸", "마이너스", "역성장")
_INCREASE = ("증가", "상승", "늘", "확대", "올랐", "올라", "플러스", "성장")
_BOUNDARY = re.compile(r"[,\.]|며|고|뒤|후|이후|대비|보다")


def _has_boundary(text: str) -> bool:
    return bool(_BOUNDARY.search(text))


def extract_value_direction(
    sentence_text: str, value_start: int, value_end: int
) -> str | None:
    """Return the unambiguous direction governing the value span.

    Direction words after the value take precedence.  A clause boundary between
    the span and a direction word prevents that word from governing the value.
    If no post-span word applies, the same rule is used for at most 20 preceding
    characters.  Ambiguous or malformed spans abstain.
    """
    if not isinstance(sentence_text, str):
        return None
    if not isinstance(value_start, int) or not isinstance(value_end, int):
        return None
    if value_start < 0 or value_end < value_start or value_end > len(sentence_text):
        return None

    matches: list[tuple[int, int, str]] = []
    for direction, words in (("DECREASE", _DECREASE), ("INCREASE", _INCREASE)):
        for word in words:
            for match in re.finditer(re.escape(word), sentence_text):
                matches.append((match.start(), match.end(), direction))

    after = [
        item for item in matches
        if item[0] >= value_end and not _has_boundary(sentence_text[value_end:item[0]])
    ]
    if after:
        nearest = min(after, key=lambda item: item[0] - value_end)
        tied = [item for item in after if item[0] - value_end == nearest[0] - value_end]
        return nearest[2] if len({item[2] for item in tied}) == 1 else None

    before = [
        item for item in matches
        if item[1] <= value_start
        and value_start - item[1] <= 20
        and not _has_boundary(sentence_text[item[1]:value_start])
    ]
    if not before:
        return None
    nearest = min(before, key=lambda item: value_start - item[1])
    tied = [item for item in before if value_start - item[1] == value_start - nearest[1]]
    return nearest[2] if len({item[2] for item in tied}) == 1 else None

