"""L4 — compose the six retrieval fields from the L2 layout and L3 assignment.

팀 인계: 라우팅과 통계표 검색·정렬 단계가 사용하는 정규화 검색 필드를
결정론적으로 생성한다.

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
    from .claim_normalizer import normalize_time_ref
except ImportError:  # pragma: no cover - direct script execution
    from claim_normalizer import normalize_time_ref

try:
    from .indicator_text import PERIOD_RE, strip_period
except ImportError:  # pragma: no cover - direct script execution
    from indicator_text import PERIOD_RE, strip_period

try:
    from .value_direction import extract_value_direction
except ImportError:  # pragma: no cover - direct script execution
    from value_direction import extract_value_direction


LEVEL = "LEVEL"
CHANGE_RATE = "CHANGE_RATE"
CHANGE_POINT = "CHANGE_POINT"
INDEX_LEVEL = "INDEX_LEVEL"

YOY = "YOY"
MOM = "MOM"
QOQ = "QOQ"
PERIOD_TO_PERIOD = "PERIOD_TO_PERIOD"
NO_BASIS = "NONE"

# Suffixes that make an indicator a rate by itself, independent of the unit.
_CHANGE_SUFFIX_RE = re.compile(
    r"(?:증가율|감소율|상승률|하락률|증감률|변화율|변동률|성장률)$"
)
# Absolute differences use the same CHANGE_POINT bucket as percentage-point
# differences.  The schema has no separate absolute-delta enum, so preserve
# the distinction from LEVEL using only explicit indicator semantics.
_CHANGE_POINT_SUFFIX_RE = re.compile(
    r"(?:증감폭|증가폭|감소폭|상승폭|하락폭|변화폭|변동폭|"
    r"전월차|전년동월차|차이|증감액|증가금액|감소금액|"
    r"증감인원|증가인원|감소인원|증가수|감소수|변화)$"
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
_DIRECT_CHANGE_AFTER_VALUE_RE = re.compile(
    r"^\s*(?:(?:약|대략|가량|정도|넘게|더)\s*)?"
    r"(?:늘어|늘었|증가|줄어|줄었|감소|상승|하락|올랐|오른|내렸|내려|"
    r"급증|급감|확대|축소|뛰|떨어)"
)
_LEVEL_COMPLEMENT_AFTER_VALUE_RE = re.compile(
    r"^\s*(?:이었|였|이다|이며|이고|다(?:\s|[.,]|$)|"
    r"으로\s*(?:집계|기록|나타|내렸|올랐))"
)

# These are grammatical comparison markers, not subject-matter vocabulary.
# Multiple distinct markers in one sentence are deliberately ambiguous: e.g.
# ``전년 동월 ... 전월보다`` compares two different things, so choosing one
# would be worse than returning NONE and letting alignment abstain.
_YOY_RE = re.compile(
    r"(?:전년\s*(?:동월|동기|같은\s*기간)?(?:\s*대비|보다)?|"
    r"지난해\s*같은\s*기간(?:\s*대비|보다)?|1년\s*전(?:\s*대비|보다)?)"
)
_MOM_RE = re.compile(
    r"(?:전월(?:\s*\([^)]*\))?(?:\s*대비|보다)?|"
    r"한\s*달\s*(?:전|만에)|지난달보다)"
)
_QOQ_RE = re.compile(r"(?:전분기|직전\s*분기)(?:\s*대비|보다)?")
_PERIOD_TO_PERIOD_RE = re.compile(
    r"(?:\d+\s*년\s*전|\d{4}년(?:\s*\d+분기|\s*\d+월)?)"
    r"[^.]{0,40}?(?:비교|보다)"
)
# ``기준`` in ``22일 기준``, ``24주차 기준`` governs the period, it is not part of
# it: the cell is ``22일``.  L2 hands the postposition through with the span, so
# both the layout period and the deterministic fallback are trimmed here where
# the two paths meet.
_GOVERNING_TAIL_RE = re.compile(r"\s*기준(?:\s*으로)?\s*$")


def _strip_governing_tail(period_raw: str) -> str:
    trimmed = _GOVERNING_TAIL_RE.sub("", period_raw).strip()
    # ``기준`` alone carries no period, so a trim that empties the field would
    # delete evidence rather than normalise it.
    return trimmed or period_raw.strip()


_BASELINE_ONLY_RE = re.compile(
    r"\s*(?:전년\s*(?:동월|동기|같은\s*기간)?|지난해\s*같은\s*기간|"
    r"1년\s*전|전월(?:\s*\([^)]*\))?|한\s*달\s*전|전분기|직전\s*분기)"
    r"(?:\s*대비|보다)?\s*"
)
_EXPLICIT_PERIOD_RE = re.compile(
    r"(?:올해|지난해|작년)\s*\d+\s*분기|"
    r"\d{4}년(?:\s*\d+\s*분기|\s*\d+\s*월)?|"
    r"(?:올해|지난해|작년|지난달|이달)|(?<!\d)\d{1,2}월"
)
_DURATION_ONLY_RE = re.compile(r"\s*(?:최근|지난)?\s*\d+\s*(?:년|개월|분기|주)\s*(?:간|동안)?\s*")

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
    tail = ""
    if isinstance(value_end, int):
        tail = str(sentence_text or "")[value_end:value_end + 20]
    explicit_point_unit = bool(
        _POINT_UNIT_RE.search(unit_text)
        or re.match(r"[pP]", tail)
    )
    if _INDEX_SUFFIX_RE.search(compact):
        return INDEX_LEVEL
    if _LEVEL_COMPLEMENT_AFTER_VALUE_RE.search(tail) and not explicit_point_unit:
        # L3 may reuse one change indicator across all values in a sentence.
        # A value serving as the level complement (``1707.8원이었다``,
        # ``1503.3원으로 내렸고``) must therefore override that shared label.
        return LEVEL
    if unit_text == "%" and explicit_point_unit:
        # The candidate span parser may leave the P/p just outside ``1%``.
        return CHANGE_POINT
    if unit_text == "%" and _CHANGE_PREDICATE_RE.search(tail):
        # ``3.1%(1조9000억원) 증가`` and ``2.6% 늘었다`` are rates even if
        # L3 attached the neighbouring absolute-change indicator.
        return CHANGE_RATE
    if _CHANGE_POINT_SUFFIX_RE.search(compact):
        return CHANGE_POINT
    if _CHANGE_SUFFIX_RE.search(compact):
        return CHANGE_POINT if explicit_point_unit else CHANGE_RATE
    if explicit_point_unit:
        # Checked before the ratio suffix: ``고용률`` is a level, but
        # ``고용률 … 0.1%포인트`` is the change in that level.
        return CHANGE_POINT
    if _RATIO_SUFFIX_RE.search(compact):
        return LEVEL
    if unit_text == "%":
        # A bare percentage is a rate only when the sentence says it moved,
        # and the predicate has to sit after the value to be about it.
        if _CHANGE_PREDICATE_RE.search(tail):
            return CHANGE_RATE
        return LEVEL
    if tail:
        # ``2.8원 내려``, ``16만명 늘어`` are absolute deltas.  Anchor the
        # predicate immediately after the value so a parenthesised baseline
        # such as ``(493만원)보다 2.6% 늘었다`` remains a LEVEL observation.
        if _DIRECT_CHANGE_AFTER_VALUE_RE.search(tail):
            return CHANGE_POINT
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


def comparison_basis(
    sentence_text: object,
    measurement: object,
) -> tuple[str, str, int | None]:
    """Return a deterministic basis, its raw marker and marker position.

    A comparison basis only applies to change observations.  A level value in
    the same sentence may carry the comparison wording as context, but it does
    not require a second cell to verify that level.
    """
    if measurement not in {CHANGE_RATE, CHANGE_POINT}:
        return NO_BASIS, "", None
    text = str(sentence_text or "")
    matches: list[tuple[str, re.Match[str]]] = []
    for basis, pattern in (
        (YOY, _YOY_RE), (MOM, _MOM_RE), (QOQ, _QOQ_RE),
        (PERIOD_TO_PERIOD, _PERIOD_TO_PERIOD_RE),
    ):
        match = pattern.search(text)
        if match:
            matches.append((basis, match))
    distinct = {basis for basis, _ in matches}
    if len(distinct) != 1:
        return NO_BASIS, "", None
    basis, match = min(matches, key=lambda item: item[1].start())
    return basis, match.group().strip(), match.start()


def _canonical_absolute(value: object) -> str:
    """Canonicalise only periods that actually resolved to a date bucket."""
    text = str(value or "").strip()
    if not text:
        return ""
    quarter = re.search(r"(\d{4})\s*(?:년|[-.]?)\s*[Qq]?\s*([1-4])\s*분기", text)
    if not quarter:
        quarter = re.search(r"(\d{4})-Q([1-4])", text, re.IGNORECASE)
    if quarter:
        return f"{quarter.group(1)}-Q{quarter.group(2)}"
    month = re.search(r"(\d{4})\s*(?:년|[.\-/ ])\s*(\d{1,2})\s*월?", text)
    if month and 1 <= int(month.group(2)) <= 12:
        return f"{month.group(1)}-{int(month.group(2)):02d}"
    year = re.fullmatch(r"\s*(\d{4})(?:년)?\s*", text)
    return year.group(1) if year else ""


def period_granularity(period_raw: object) -> str:
    """Return the resolution the cell is actually stated at.

    Checked week-first because ``이달 첫 주`` contains ``이달``: reading it as a
    month is exactly the collapse this function exists to prevent.
    """
    text = str(period_raw or "").strip()
    if not text:
        return ""
    if re.search(r"주\s*차|(?<![가-힣])주(?![가-힣])|주간", text):
        return "week"
    if re.search(r"\d{1,2}\s*일", text):
        return "day"
    if re.search(r"분기", text):
        return "quarter"
    if re.search(r"동월|전월|지난달|이달|\d{1,2}\s*월", text):
        return "month"
    if re.search(r"\d{4}\s*년|지난해|작년|올해|전년", text):
        return "year"
    return ""


# The canonical bucket format only reaches month resolution.  Emitting a month
# for a week or a day would make ``5월 첫째 주`` and ``5월 셋째 주`` equal, so a
# finer cell resolves to nothing rather than to a wrong-but-comparable value.
_UNREPRESENTABLE_GRANULARITY = {"week", "day"}


def resolved_absolute_period(period_raw: object, published_at: object) -> str:
    """Resolve a raw period, returning empty instead of echoing unresolved text."""
    if not period_raw or not published_at:
        return ""
    if period_granularity(period_raw) in _UNREPRESENTABLE_GRANULARITY:
        return ""
    return _canonical_absolute(absolute_period(period_raw, published_at))


def _shift_period(absolute: str, amount: int) -> str:
    month = re.fullmatch(r"(\d{4})-(\d{2})", absolute)
    if month:
        serial = int(month.group(1)) * 12 + int(month.group(2)) - 1 + amount
        return f"{serial // 12:04d}-{serial % 12 + 1:02d}"
    quarter = re.fullmatch(r"(\d{4})-Q([1-4])", absolute)
    if quarter:
        serial = int(quarter.group(1)) * 4 + int(quarter.group(2)) - 1 + amount
        return f"{serial // 4:04d}-Q{serial % 4 + 1}"
    year = re.fullmatch(r"(\d{4})", absolute)
    if year:
        return str(int(year.group(1)) + amount)
    return ""


def _granularity(*texts: object) -> str:
    joined = " ".join(str(text or "") for text in texts)
    if re.search(r"분기", joined):
        return "quarter"
    if re.search(r"동월|전월|지난달|이달|\d{1,2}월", joined):
        return "month"
    return "year"


def _publication_bucket(published_at: object, granularity: str) -> str:
    match = re.match(r"(\d{4})-(\d{1,2})", str(published_at or ""))
    if not match:
        return ""
    year, month = int(match.group(1)), int(match.group(2))
    if granularity == "month":
        return f"{year:04d}-{month:02d}"
    if granularity == "quarter":
        return f"{year:04d}-Q{(month - 1) // 3 + 1}"
    return f"{year:04d}"


def _preceding_measurement_raw(sentence: str, comparison_start: int | None) -> str:
    if comparison_start is None:
        return ""
    prefix = sentence[:comparison_start]
    matches = list(_EXPLICIT_PERIOD_RE.finditer(prefix))
    return matches[-1].group().strip() if matches else ""


def period_pair(
    period_raw: object,
    sentence_text: object,
    measurement: object,
    published_at: object,
    fallback_measurement_raw: object = None,
) -> dict[str, Any]:
    """Build measurement/baseline periods without inventing an unknown basis."""
    raw = str(period_raw or "").strip()
    sentence = str(sentence_text or "")
    basis, baseline_marker, comparison_start = comparison_basis(sentence, measurement)
    baseline_only = bool(raw and _BASELINE_ONLY_RE.fullmatch(raw))
    if baseline_only:
        measurement_raw = (
            _preceding_measurement_raw(sentence, comparison_start)
            or str(fallback_measurement_raw or "").strip()
        )
    else:
        measurement_raw = raw
    if basis == PERIOD_TO_PERIOD and _DURATION_ONLY_RE.fullmatch(measurement_raw):
        # ``5년 간`` is a distance between cells, not the measurement cell.
        # The legacy period_raw alias still preserves it as source evidence.
        measurement_raw = ""
    baseline_raw = raw if baseline_only and basis != NO_BASIS else baseline_marker

    granularity = _granularity(measurement_raw, baseline_raw, sentence)
    measurement_absolute = resolved_absolute_period(measurement_raw, published_at)
    baseline_absolute = ""

    if basis != NO_BASIS:
        # When L3 supplied only ``전월(8월)``, the concrete 8월 is the baseline
        # and the measurement is one bucket later.  Otherwise a missing local
        # measurement falls back to the publication bucket, visibly recorded
        # by an empty raw field rather than a fabricated source phrase.
        if baseline_only:
            baseline_absolute = resolved_absolute_period(raw, published_at)
        if not measurement_absolute and baseline_absolute:
            forward = {YOY: 12 if granularity == "month" else 4 if granularity == "quarter" else 1,
                       MOM: 1, QOQ: 1}.get(basis)
            if forward is not None:
                measurement_absolute = _shift_period(baseline_absolute, forward)
        if not measurement_absolute and basis != PERIOD_TO_PERIOD:
            measurement_absolute = _publication_bucket(published_at, granularity)
        if not baseline_absolute:
            backward = {
                YOY: -12 if granularity == "month" else -4 if granularity == "quarter" else -1,
                MOM: -1,
                QOQ: -1,
            }.get(basis)
            if backward is not None:
                baseline_absolute = _shift_period(measurement_absolute, backward)
        # PERIOD_TO_PERIOD needs two explicit anchors.  A duration plus the
        # publication date is not enough evidence for the exact KOSIS cells.
        if basis == PERIOD_TO_PERIOD:
            baseline_absolute = ""

    return {
        "measurement": {"raw": measurement_raw, "absolute": measurement_absolute},
        "baseline": {"raw": baseline_raw, "absolute": baseline_absolute},
        "basis": basis,
    }


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
    period_raw = _strip_governing_tail(str(assignment.get("period_raw") or ""))
    if not period_raw and stripped_periods:
        # The layout left the period empty but the indicator carried one, so
        # the information is moved rather than discarded.
        period_raw = _strip_governing_tail(stripped_periods[0])
    kind = measurement_type(
        indicator, unit, sentence, assignment.get("value_char_end")
    )
    value_start = assignment.get("value_char_start")
    value_end = assignment.get("value_char_end")
    value_direction = (
        extract_value_direction(sentence, value_start, value_end)
        if value_start is not None and value_end is not None else None
    )
    pair = period_pair(
        period_raw,
        sentence,
        kind,
        published_at,
        assignment.get("region_period_raw"),
    )
    baseline_level = assignment.get("indicator_pairing") == "PARENTHESIZED_BASELINE"
    if baseline_level:
        _, _, comparison_start = comparison_basis(sentence, CHANGE_RATE)
        current_raw = _preceding_measurement_raw(sentence, comparison_start)
        comparison_pair = period_pair(
            current_raw or period_raw,
            sentence,
            CHANGE_RATE,
            published_at,
            assignment.get("region_period_raw"),
        )
        if comparison_pair["baseline"]["raw"]:
            # This value is the comparison cell itself, so its measurement
            # period is the comparison baseline—not the current observation.
            pair = {
                "measurement": comparison_pair["baseline"],
                "baseline": {"raw": "", "absolute": ""},
                "basis": NO_BASIS,
            }
    fields = {
        "indicator": indicator,
        "measurement_type": kind,
        "period": pair,
        # Transitional aliases for existing audit/evaluation artifacts.  New
        # query-plan code consumes the nested pair above; these preserve the
        # pre-R1 raw evidence instead of deleting it during the contract move.
        "period_raw": period_raw,
        "period_absolute": absolute_period(period_raw, published_at),
        "population": population_terms(indicator, sentence),
        "item": item_terms(indicator),
        "dimension": dimension_terms(indicator, sentence),
        "value_direction": value_direction,
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
            "period_pair_method": (
                "PARENTHESIZED_BASELINE_CELL_V1"
                if baseline_level else
                "DETERMINISTIC_COMPARISON_EXPRESSION_V1"
            ),
            # The cell's own resolution and where its absolute came from.  Both
            # are recorded rather than folded into the absolute string so a
            # later comparator can refuse to equate two different resolutions.
            "period_granularity": period_granularity(
                pair["measurement"]["raw"] or period_raw
            ),
            "period_resolution_basis": (
                "UNRESOLVED" if not pair["measurement"]["absolute"] else
                "EXPLICIT_SPAN"
                if re.search(r"\d{4}\s*년", pair["measurement"]["raw"] or "")
                else "PUBLISHED_AT"
            ),
            "period_measurement_fallback": (
                "SOURCE_REGION_OPENING_PERIOD"
                if pair["measurement"]["raw"]
                and pair["measurement"]["raw"]
                == str(assignment.get("region_period_raw") or "").strip()
                and pair["measurement"]["raw"] != period_raw
                else "NONE"
            ),
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
