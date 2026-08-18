"""L1이 쓰는 기사 원문 분할·수치 표기 기본 규칙.

이 모듈은 기존 claim_extractor에서 L1의 전이 의존성만 분리한 것이다.
주장 판정이나 검색·검증 로직은 포함하지 않는다.
"""

from __future__ import annotations

import re


def iter_sentence_spans(text: str):
    """Yield zero-based sentence order and half-open offsets in cleaned text."""
    boundary_re = re.compile(r"(?<=[.!?])(?<!\d\.)\s+")
    segment_start = 0
    sentence_index = 0
    for boundary in list(boundary_re.finditer(text)) + [None]:
        segment_end = boundary.start() if boundary else len(text)
        raw = text[segment_start:segment_end]
        sentence = raw.strip()
        if sentence:
            char_start = segment_start + (len(raw) - len(raw.lstrip()))
            char_end = char_start + len(sentence)
            yield sentence_index, char_start, char_end, sentence
            sentence_index += 1
        if boundary is None:
            break
        segment_start = boundary.end()


_UNIT_ALT = (
    r"%\s?포인트|%p|%|원|위안|달러|배럴|천\s?명|만\s?명|명|건|가구|톤|t|ha|kg|g|포인트"
    r"|GW|MW|mm|GB|단계|분기|일|주(?!년)|선|비율"
    r"|세(?!대)|개월|년|위|대|개|채|척|마리|그루|병|잔|회|차례|편|곳|층|배"
    r"|시간|분(?!의|기|위)|초(?!반)|도|점"
)
_UNIT_CANON = {"%포인트": "%p", "%p": "%p"}


def canon_unit(unit: str) -> str:
    """Keep the measured L1 unit canonicalisation unchanged."""
    return _UNIT_CANON.get(unit.replace(" ", ""), unit)


VALUE_UNIT_RE = re.compile(
    rf"(?P<value>[-−]?\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?(?:\s?\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?)*)"
    rf"\s*(?P<unit>{_UNIT_ALT})"
)
