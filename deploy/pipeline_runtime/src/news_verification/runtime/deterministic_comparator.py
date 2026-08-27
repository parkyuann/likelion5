"""Deterministic numeric operations for the provisional E2E-M1 contract."""

from __future__ import annotations

import re
from typing import Any

_SCALES = {"조": 10**12, "억": 10**8, "만": 10**4, "천": 10**3, "백만": 10**6}
_DIMENSIONS = {
    "money:KRW": ("원",),
    "money:USD": ("달러",),
    "count": ("명", "가구", "개"),
    "percent": ("%", "%p"),
    "ratio": ("배",),
}
_NUMBER = re.compile(r"^([+-]?)([0-9]+(?:\.[0-9]+)?)(.*)$")
_COMPONENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)?(조|억|만|천)")


def apply_direction(value: float, direction: str | None) -> tuple[float, str]:
    """Apply a sentence direction to a parsed change value."""
    if value < 0:
        return value, "literal"
    if direction is None:
        return value, "none"
    if direction == "DECREASE":
        return -value, "direction_word"
    if direction == "INCREASE":
        return value, "direction_word"
    return value, "none"


def parse_korean_number(text: str) -> tuple[float, str] | None:
    """Parse only the small, explicit Korean-number subset in the M1 design.

    Unknown words and formats return ``None``.  In particular, this function
    never turns an approximate expression into a guessed number.
    """
    if not isinstance(text, str):
        return None
    value = re.sub(r"[\s,]", "", text).strip()
    match = _NUMBER.match(value)
    if not match:
        return None
    sign, first, rest = match.groups()

    unit = ""
    for suffix in ("%p", "%", "달러", "원", "배", "명"):
        if rest.endswith(suffix):
            unit = suffix
            rest = rest[: -len(suffix)]
            break

    if not rest:
        number = float(first)
    else:
        total = 0.0
        position = 0
        previous_factor = 10**13
        component_count = 0
        while position < len(rest):
            component = _COMPONENT.match(rest, position)
            if component is None:
                # A trailing number is permitted only after a magnitude,
                # e.g. 25조4000억원.
                tail = re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", rest[position:])
                if tail and component_count:
                    total += float(tail.group())
                    position = len(rest)
                    continue
                return None
            numeric, marker = component.groups()
            if numeric is None:
                if component_count:
                    return None
                numeric = first
            factor = _SCALES[marker]
            if factor >= previous_factor:
                return None
            total += float(numeric) * factor
            previous_factor = factor
            component_count += 1
            position = component.end()
        number = total

    return (-number if sign == "-" else number), unit


def to_base_unit(value: float, unit_text: str) -> tuple[float, str] | None:
    """Return a value in its base unit and its dimension, or None if unknown."""
    unit = re.sub(r"\s+", "", str(unit_text or ""))
    token = re.search(r"(?:조|억|만|천|백만)?(?:달러|원|배|명|가구|개|%p|%)$", unit)
    if token:
        unit = token.group(0)
    for dimension, units in _DIMENSIONS.items():
        for base in units:
            if unit == base:
                return float(value), "percent_point" if base == "%p" else dimension
            for marker, scale in sorted(_SCALES.items(), key=lambda item: -len(item[0])):
                if unit == marker + base:
                    return float(value) * scale, dimension
    return None


def tolerance_from_precision(article_value_text: str) -> float:
    """Derive half of the last displayed place from the article text."""
    text = re.sub(r"[\s,]", "", str(article_value_text or ""))
    parsed = parse_korean_number(text)
    if parsed is None:
        raise ValueError("article value precision is not parseable")
    number, unit = parsed
    numeric = re.match(r"^[+-]?([0-9]+(?:\.[0-9]+)?)", text)
    if numeric is None:
        raise ValueError("article value precision is not parseable")
    highest = 1.0
    marker = re.search(r"(조|억|만|천|백만)", text)
    if marker:
        highest = float(_SCALES[marker.group(1)])
        # A lower component makes the displayed base-unit value decimal at
        # the scale of the highest component (e.g. 25조4000억원 = 25.4조).
        lower = text[marker.end():]
        if lower:
            decimals = str(number / highest).rstrip("0").split(".")
            places = len(decimals[1]) if len(decimals) == 2 else 0
            return highest / (2 * (10 ** places))
        return highest / 2
    digits = numeric.group(1).split(".")
    place = 10 ** (-len(digits[1])) if len(digits) == 2 else 1.0
    return place / 2


def compare_values(
    claim_value: float,
    official_value: float,
    *,
    rel_tolerance: float = 0.005,
    article_unit: str | None = None,
    official_unit: str | None = None,
    article_value_text: str | None = None,
    use_unit_conversion: bool = False,
    use_precision_tolerance: bool = False,
) -> dict[str, float | bool | str]:
    claim_base, official_base = float(claim_value), float(official_value)
    unit_status = "UNKNOWN"
    claim_unit = to_base_unit(claim_base, article_unit or "") if article_unit else None
    official_unit_value = to_base_unit(official_base, official_unit or "") if official_unit else None
    if claim_unit and official_unit_value:
        if claim_unit[1] != official_unit_value[1]:
            unit_status = "KNOWN_MISMATCH"
        else:
            unit_status = "KNOWN_COMPATIBLE"
            if use_unit_conversion:
                claim_base, official_base = claim_unit[0], official_unit_value[0]
    abs_diff = abs(claim_base - official_base)
    denominator = abs(official_base)
    rel_diff = abs_diff / denominator if denominator else (0.0 if abs_diff == 0 else float("inf"))
    tolerance = tolerance_from_precision(article_value_text) if use_precision_tolerance else None
    match = (abs_diff <= tolerance) if tolerance is not None else (rel_diff <= rel_tolerance)
    if unit_status == "KNOWN_MISMATCH":
        match = False
    result = {"match": match, "rel_diff": rel_diff, "abs_diff": abs_diff, "unit_status": unit_status}
    if tolerance is not None:
        result["tolerance"] = tolerance
    result["claim_value_base"] = claim_base
    result["official_value_base"] = official_base
    return result


def compute_derived(operator: str, cells: list[Any]) -> float | None:
    """Compute a supported operation, selecting operands by role."""
    if operator not in {"PERCENT_CHANGE", "DIFFERENCE", "RATIO"}:
        return None
    if all(isinstance(cell, dict) for cell in cells):
        by_role = {cell.get("role"): cell.get("value") for cell in cells}
        measurement = by_role.get("measurement")
        baseline = by_role.get("baseline")
        if measurement is None or baseline is None:
            return None
    elif len(cells) >= 2:
        measurement, baseline = cells[0], cells[1]
    else:
        return None
    try:
        measurement, baseline = float(measurement), float(baseline)
    except (TypeError, ValueError):
        return None
    if operator == "PERCENT_CHANGE":
        return None if baseline == 0 else (measurement - baseline) / baseline * 100
    if operator == "DIFFERENCE":
        return measurement - baseline
    return None if baseline == 0 else measurement / baseline


