"""Surface-string facts about Korean indicator labels, shared by L3 and L4.

These are grammatical observations, not domain vocabulary: a measure noun says
how something is counted, a basis marker says what it is counted against, a
period says when.  None of them name a subject, so all three have to be
recognisable before either layer can ask whether a label is usable.

The rules live here rather than inside L4 because L3 needs the same test one
stage earlier — a label still carrying its period (``1분기 성장률``) is just as
subjectless as one without it, and L3 decides inheritance before L4 has run.
Duplicating the patterns would let the two layers disagree silently.
"""

from __future__ import annotations

import re

# A period inside the indicator makes the search string unmatchable: no KOSIS
# table is called ``이달 첫 주 휘발유 평균 판매가``.  The span branch comes first so
# ``2023년`` is not eaten by it.
PERIOD_RE = re.compile(
    r"(?:(?:최근|지난|향후)\s*)?\d+\s*(?:년|개월|분기|주)\s*(?:간|동안)|"
    r"(?:이달|지난달|이번\s*달|올해|지난해|작년|전년\s*동월|전년\s*동기|전년|"
    r"전월|전분기|전주|지난\s*주|이번\s*주|첫\s*주|둘째\s*주|셋째\s*주|"
    r"넷째\s*주|상반기|하반기|동월|동기|현재|당월|최근|"
    r"\d{4}년(?:\s*\d+월)?(?:\s*\d+분기)?|\d+분기|\d+월)"
    r"\s*(?:대비|기준)?\s*"
)

# Measure nouns.  Kept to measure nouns on purpose — this is a grammatical
# test, not a lexicon that grows with the corpus (CLAUDE.md 6.5절 4항).
MEASURE_TAIL_RE = re.compile(
    r"(?:증가율|감소율|상승률|하락률|증감률|변화율|성장률|비율|비중|점유율|지수)$"
)

# ``원화 기준`` / ``전년 대비`` state what a figure is compared against, never
# what was measured.
BASIS_TAIL_RE = re.compile(r"(?:기준|대비)$")


def strip_period(indicator: object) -> tuple[str, list[str]]:
    """Return the indicator without period words, plus what was removed."""
    text = str(indicator or "").strip()
    if not text:
        return "", []
    removed = [match.group().strip() for match in PERIOD_RE.finditer(text)]
    stripped = re.sub(r"\s+", " ", PERIOD_RE.sub(" ", text)).strip()
    # Never return an empty indicator: a period-only label is better kept whole
    # than dropped, because retrieval can still fail loudly on it.
    return (stripped or text), [item for item in removed if item]


def measure_tail(indicator: object) -> str:
    """The measure noun a label ends with, or ``""`` when it names none."""
    match = MEASURE_TAIL_RE.search(str(indicator or "").strip())
    return match.group() if match else ""


def names_a_subject(indicator: object) -> bool:
    """Whether anything is left once period, measure and basis are removed."""
    body, _ = strip_period(indicator)
    body = MEASURE_TAIL_RE.sub("", body).strip()
    if not body:
        return False
    return not BASIS_TAIL_RE.search(body)

