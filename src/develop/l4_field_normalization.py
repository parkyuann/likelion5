"""L4 — compose the six retrieval fields from the L2 layout and L3 assignment.

The model never emits these fields.  r11~r16i asked it to, and the six-field
joint accuracy was 0.009 while the individual fields reached 0.12~0.61: each
field was derived on its own, so each failed on its own.

Here every field is a deterministic function of what earlier layers already
decided, which makes the errors correlate with the layer that caused them and
keeps them attributable.  No model call happens in this module.
"""

from __future__ import annotations

import re
from typing import Any

try:
    from ..claim_normalizer import normalize_time_ref
except ImportError:  # pragma: no cover - direct script execution
    from claim_normalizer import normalize_time_ref

try:
    from .indicator_text import PERIOD_RE, strip_period
except ImportError:  # pragma: no cover - direct script execution
    from indicator_text import PERIOD_RE, strip_period


LEVEL = "LEVEL"
CHANGE_RATE = "CHANGE_RATE"
CHANGE_POINT = "CHANGE_POINT"
INDEX_LEVEL = "INDEX_LEVEL"

# Suffixes that make an indicator a rate by itself, independent of the unit.
_CHANGE_SUFFIX_RE = re.compile(
    r"(?:증가율|감소율|상승률|하락률|증감률|변화율|성장률)$"
)
# A ratio indicator ending in these is a level even though the unit is %.
_RATIO_SUFFIX_RE = re.compile(
    r"(?:비율|비중|점유율|연체율|고용률|실업률|출산율|증가세)$"
)
_INDEX_SUFFIX_RE = re.compile(r"(?:지수)$")
_CHANGE_PREDICATE_RE = re.compile(
    r"(?:늘어|늘었|증가|줄어|줄었|감소|상승|하락|올랐|내렸|급증|급감|"
    r"확대|축소|뛰|떨어)"
)
_POINT_UNIT_RE = re.compile(r"(?:%?포인트|%p|%P)")

# Population nouns the retrieval layer can filter a table by.
_POPULATION_RE = re.compile(
    r"(?:개인사업자|자영업자|근로자|노동자|취업자|실업자|가구원|가구|인구|"
    r"주민|학생|환자|사업체|기업|농가|어가|청년|고령자|노인|외국인)"
)
_DIMENSION_RE = re.compile(
    r"(?:대기업|중견기업|중소기업|수도권|비수도권|서울|경기|전국|남성|여성|"
    r"청년층|고령층|제조업|서비스업|건설업|농림어업|공공부문|민간부문)"
)


# A period inside the indicator makes the search string unmatchable: no KOSIS
# table is called ``이달 첫 주 휘발유 평균 판매가``.  The period belongs in its own
# field, where the retrieval stage can align it against the table's time axis.
# The pattern lives in ``indicator_text`` because L3 needs the same period
# awareness one stage earlier; see that module's docstring.
_PERIOD_IN_INDICATOR_RE = PERIOD_RE

# Modifiers that open an indicator without naming anything a KOSIS table is
# titled after.  Stripped only at the head and only as whole tokens, so
# `국내총생산` — one token — survives intact.
#
# Foreign scope (`독일`, `OECD`, `미국`) is deliberately absent.  Removing it
# would turn a claim KOSIS cannot hold into one that looks retrievable, which
# is worse than leaving the query long: `독일 소득세 최고 세율` must keep saying
# 독일.
_DOMESTIC_SCOPE_TOKENS = frozenset({
    "한국", "한국의", "대한민국", "대한민국의", "우리나라", "우리나라의",
})
_TEMPORAL_QUALIFIER_TOKENS = frozenset({
    "초기", "말기", "초반", "중반", "후반", "당시",
})
# Projection markers.  Stripped from the query because no table is titled
# `미래 …`, but reported rather than discarded: CLAUDE.md 2.2절 keeps forecasts
# out of automatic verification, and L5 needs the signal to survive L4.
_PROJECTION_TOKENS = frozenset({"미래", "향후", "장래"})


def strip_leading_modifiers(indicator: object) -> tuple[str, dict[str, list[str]]]:
    """Drop head tokens that carry no table-title signal, keeping a record."""
    text = str(indicator or "").strip()
    tokens = text.split()
    removed: dict[str, list[str]] = {"scope": [], "temporal": [], "projection": []}
    while tokens:
        head = tokens[0]
        if head in _DOMESTIC_SCOPE_TOKENS:
            removed["scope"].append(head)
        elif head in _TEMPORAL_QUALIFIER_TOKENS:
            removed["temporal"].append(head)
        elif head in _PROJECTION_TOKENS:
            removed["projection"].append(head)
        else:
            break
        tokens = tokens[1:]
    # An indicator made only of modifiers is kept whole; retrieval failing
    # loudly on it beats an empty query that fails silently.
    return (" ".join(tokens) or text), removed


def strip_period_from_indicator(indicator: object) -> tuple[str, list[str]]:
    """Return the indicator without period words, plus what was removed."""
    return strip_period(indicator)


def _compact(text: object) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def measurement_type(
    indicator_label: object,
    unit: object,
    sentence_text: object,
    value_end: object = None,
) -> str:
    """Classify what kind of quantity the value is.

    The indicator wins over the unit: ``고용률`` is a level despite the percent
    sign, and ``상승률`` is a rate regardless of what follows it.  Only when the
    indicator is silent does the sentence's predicate decide.
    """
    compact = _compact(indicator_label)
    unit_text = str(unit or "")
    if _INDEX_SUFFIX_RE.search(compact):
        return INDEX_LEVEL
    if _CHANGE_SUFFIX_RE.search(compact):
        return CHANGE_POINT if _POINT_UNIT_RE.search(unit_text) else CHANGE_RATE
    if _POINT_UNIT_RE.search(unit_text):
        # Checked before the ratio suffix: ``고용률`` is a level, but
        # ``고용률 … 0.1%포인트`` is the change in that level.
        return CHANGE_POINT
    if _RATIO_SUFFIX_RE.search(compact):
        return LEVEL
    if unit_text == "%":
        # A bare percentage is a rate only when the sentence says it moved,
        # and the predicate has to sit after the value to be about it.
        tail = ""
        if isinstance(value_end, int):
            tail = str(sentence_text or "")[value_end:value_end + 20]
        if _CHANGE_PREDICATE_RE.search(tail):
            return CHANGE_RATE
        return LEVEL
    return LEVEL


def absolute_period(period_raw: object, published_at: object) -> str:
    """Resolve ``지난달`` / ``3분기`` against the article's publication date."""
    text = str(period_raw or "").strip()
    if not text:
        return ""
    if not published_at:
        return text
    try:
        resolved = normalize_time_ref(text, published_at)
    except Exception:  # noqa: BLE001 - a bad date must not lose the raw text
        return text
    return str(resolved or text)


def _terms(pattern: re.Pattern[str], *sources: object) -> list[str]:
    found: list[str] = []
    for source in sources:
        for match in pattern.findall(str(source or "")):
            if match not in found:
                found.append(match)
    return found


def population_terms(indicator_label: object, sentence_text: object) -> list[str]:
    """Population words are taken from the indicator first.

    The sentence is only consulted when the indicator names no population, so
    an unrelated noun elsewhere in the sentence cannot leak into the query.
    """
    from_indicator = _terms(_POPULATION_RE, indicator_label)
    return from_indicator or _terms(_POPULATION_RE, sentence_text)


def dimension_terms(indicator_label: object, sentence_text: object) -> list[str]:
    from_indicator = _terms(_DIMENSION_RE, indicator_label)
    return from_indicator or _terms(_DIMENSION_RE, sentence_text)


# Only rate and ratio suffixes are stripped.  Gold keeps the measure noun —
# its item for ``대기업 수출액 증가율`` is ``수출액``, not ``수출`` — and it keeps the
# population inside the item (``단기 근로자``), so neither is removed here.
_METRIC_SUFFIX_RE = re.compile(
    r"\s*(?:증가율|감소율|상승률|하락률|증감률|변화율|성장률|비율|비중|"
    r"점유율|지수)$"
)


def item_terms(indicator_label: object) -> list[str]:
    """The item is the indicator minus its metric suffix and dimension facet."""
    compact = str(indicator_label or "").strip()
    if not compact:
        return []
    stripped = _METRIC_SUFFIX_RE.sub("", compact).strip()
    stripped = _DIMENSION_RE.sub("", stripped).strip()
    if not stripped or stripped == compact:
        return []
    return [stripped]


def compose_fields(
    assignment: dict[str, Any],
    *,
    published_at: object = None,
) -> dict[str, Any]:
    """Return the six retrieval fields for one value."""
    raw_indicator = assignment.get("indicator_label") or ""
    indicator, stripped_periods = strip_period_from_indicator(raw_indicator)
    indicator, stripped_modifiers = strip_leading_modifiers(indicator)
    sentence = assignment.get("sentence_text") or ""
    unit = assignment.get("value_unit")
    period_raw = str(assignment.get("period_raw") or "")
    if not period_raw and stripped_periods:
        # The layout left the period empty but the indicator carried one, so
        # the information is moved rather than discarded.
        period_raw = stripped_periods[0]
    fields = {
        "indicator": indicator,
        "measurement_type": measurement_type(
            indicator, unit, sentence, assignment.get("value_char_end")
        ),
        # Gold records the article's own wording (``지난해``) and adds the
        # comparison basis (``·전년동기비``).  The absolute form is what
        # retrieval queries with, so both are kept and neither is discarded.
        "period": period_raw,
        "period_absolute": absolute_period(period_raw, published_at),
        "population": population_terms(indicator, sentence),
        "item": item_terms(indicator),
        "dimension": dimension_terms(indicator, sentence),
    }
    return {
        **assignment,
        "retrieval_fields": fields,
        "field_provenance": {
            "indicator_raw": raw_indicator,
            "indicator_stripped_periods": stripped_periods,
            "indicator_stripped_modifiers": stripped_modifiers,
            # Survives L4 so a later layer can act on it; L4 itself does not
            # route (CLAUDE.md 6.6절 — one layer per round).
            "indicator_projection_marker": bool(stripped_modifiers["projection"]),
            "indicator": assignment.get("indicator_source", "NONE"),
            "period": assignment.get("period_source", "NONE"),
            "source_subtype": assignment.get("source_subtype", ""),
            # A facet dictionary cannot produce the classification criterion a
            # statistic is actually broken down by (``주당 36시간 미만``,
            # ``상위 10개사``).  It catches business size and little else, so the
            # method is recorded and the metric reported separately.
            "dimension_method": "FACET_DICTIONARY_PARTIAL",
        },
    }


def compose_all(
    assignments: list[dict[str, Any]],
    published_at_by_article: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    published = published_at_by_article or {}
    return [
        compose_fields(
            row,
            published_at=published.get(str(row.get("article_idx"))),
        )
        for row in assignments
    ]
