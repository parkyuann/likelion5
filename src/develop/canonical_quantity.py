"""Fail-closed Decimal quantity normalization for deterministic verification.

This module deliberately handles only explicit Korean numeric/unit forms.  It
does not infer exchange rates, denominators, approximation semantics, or
missing units.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping


NORMALIZATION_RULE_ID = "canonical-quantity-ko-v1"

_SCALES = {
    "조": Decimal("1000000000000"),
    "억": Decimal("100000000"),
    "천만": Decimal("10000000"),
    "백만": Decimal("1000000"),
    "만": Decimal("10000"),
    "천": Decimal("1000"),
}
_SCALE_PATTERN = "|".join(sorted(_SCALES, key=len, reverse=True))
_BASE_UNITS = ("달러", "가구", "%p", "원", "명", "건", "개", "%", "배")
_UNIT_PATTERN = "|".join(re.escape(unit) for unit in _BASE_UNITS)
_QUALIFIERS = ("약", "이상", "이하", "초과", "미만")
_DENOMINATORS = (("1인당", "per_capita"), ("인당", "per_capita"), ("가구당", "per_household"))


class QuantityNormalizationError(ValueError):
    """A stable fail-closed normalization error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CanonicalQuantity:
    value_base: Decimal
    dimension: str
    currency: str | None
    normalized_unit: str
    scale_multiplier: Decimal
    source_value_text: str
    source_unit_text: str
    precision_quantum: Decimal
    qualifier: str | None
    denominator: str | None
    normalization_rule_id: str
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class CanonicalComparison:
    match: bool
    status: str
    absolute_difference: Decimal | None
    relative_difference: Decimal | None


def _compact(value: Any) -> str:
    return re.sub(r"[\s,]", "", str(value if value is not None else "")).strip()


def _strip_qualifier(text: str) -> tuple[str, str | None]:
    for qualifier in _QUALIFIERS:
        if text.startswith(qualifier):
            return text[len(qualifier) :], qualifier
    return text, None


def _strip_denominator(text: str) -> tuple[str, str | None]:
    for surface, normalized in _DENOMINATORS:
        if surface in text:
            return text.replace(surface, "", 1), normalized
    return text, None


def _split_unit(text: str) -> tuple[str, str | None]:
    for unit in _BASE_UNITS:
        if text.endswith(unit):
            return text[: -len(unit)], unit
    return text, None


def _unit_contract(unit: str) -> tuple[str, str | None, str]:
    if unit == "원":
        return "money", "KRW", "KRW"
    if unit == "달러":
        return "money", "USD", "USD"
    if unit == "%":
        return "percent", None, "%"
    if unit == "%p":
        return "percent_point", None, "%p"
    if unit == "배":
        return "ratio", None, "ratio"
    if unit in {"명", "가구", "건", "개"}:
        return f"count:{unit}", None, unit
    raise QuantityNormalizationError("UNIT_UNSUPPORTED", f"unsupported unit: {unit!r}")


def _parse_number_expression(text: str) -> tuple[Decimal, Decimal, str | None]:
    """Return base numeric value, precision quantum, and outer scale marker."""
    if not text:
        raise QuantityNormalizationError("VALUE_INVALID", "empty numeric value")
    plain = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text)
    if plain:
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise QuantityNormalizationError("VALUE_INVALID", f"invalid value: {text!r}") from exc
        places = max(0, -value.as_tuple().exponent)
        return value, Decimal(1).scaleb(-places), None

    sign = Decimal(-1) if text.startswith("-") else Decimal(1)
    unsigned = text[1:] if text[:1] in "+-" else text
    token = re.compile(rf"(\d+(?:\.\d+)?)({_SCALE_PATTERN})")
    position = 0
    total = Decimal(0)
    previous = Decimal("1e30")
    quantum: Decimal | None = None
    outer_marker: str | None = None
    count = 0
    for match in token.finditer(unsigned):
        if match.start() != position:
            raise QuantityNormalizationError("VALUE_INVALID", f"unparsed numeric text: {text!r}")
        numeric = Decimal(match.group(1))
        marker = match.group(2)
        factor = _SCALES[marker]
        if factor >= previous:
            raise QuantityNormalizationError("VALUE_INVALID", f"non-descending scale: {text!r}")
        if outer_marker is None:
            outer_marker = marker
        places = max(0, -numeric.as_tuple().exponent)
        component_quantum = factor * Decimal(1).scaleb(-places)
        quantum = component_quantum if quantum is None else min(quantum, component_quantum)
        total += numeric * factor
        previous = factor
        position = match.end()
        count += 1
    # Korean mixed numerals commonly omit the final ones marker: ``3만6745``
    # means 3*10,000 + 6,745.  The tail is safe only after a scale token and
    # only when it is strictly smaller than the preceding scale.
    if count and position < len(unsigned):
        tail_text = unsigned[position:]
        if not re.fullmatch(r"\d+(?:\.\d+)?", tail_text):
            raise QuantityNormalizationError("VALUE_INVALID", f"unparsed numeric text: {text!r}")
        tail = Decimal(tail_text)
        if tail >= previous:
            raise QuantityNormalizationError("VALUE_INVALID", f"tail exceeds preceding scale: {text!r}")
        places = max(0, -tail.as_tuple().exponent)
        tail_quantum = Decimal(1).scaleb(-places)
        quantum = tail_quantum if quantum is None else min(quantum, tail_quantum)
        total += tail
        position = len(unsigned)
    if not count or position != len(unsigned):
        raise QuantityNormalizationError("VALUE_INVALID", f"unparsed numeric text: {text!r}")
    return sign * total, quantum or Decimal(1), outer_marker


def normalize_quantity(
    value: Any,
    unit_text: Any = "",
    *,
    provenance: Mapping[str, Any] | None = None,
) -> CanonicalQuantity:
    """Normalize an explicit value/unit pair without guessing missing semantics."""
    value_source = str(value if value is not None else "")
    unit_source = str(unit_text if unit_text is not None else "")
    value_text, qualifier = _strip_qualifier(_compact(value_source))
    value_text, value_denominator = _strip_denominator(value_text)
    unit_text_compact, unit_denominator = _strip_denominator(_compact(unit_source))
    if value_denominator and unit_denominator and value_denominator != unit_denominator:
        raise QuantityNormalizationError("UNIT_DENOMINATOR_CONFLICT", "value and unit denominators disagree")
    denominator = value_denominator or unit_denominator

    numeric_text, embedded_unit = _split_unit(value_text)
    unit_numeric_prefix, field_unit = _split_unit(unit_text_compact)
    if embedded_unit and field_unit and embedded_unit != field_unit:
        raise QuantityNormalizationError("UNIT_CONFLICT", "value and unit fields disagree")
    base_unit = embedded_unit or field_unit
    if base_unit is None:
        raise QuantityNormalizationError("UNIT_UNAVAILABLE", "an explicit supported unit is required")

    field_scale = unit_numeric_prefix or ""
    if field_scale and field_scale not in _SCALES:
        raise QuantityNormalizationError("UNIT_UNSUPPORTED", f"unsupported unit prefix: {field_scale!r}")
    value_base, quantum, value_scale = _parse_number_expression(numeric_text)
    scale_multiplier = _SCALES.get(field_scale, Decimal(1))
    if value_scale:
        if field_scale and field_scale != value_scale:
            raise QuantityNormalizationError("UNIT_SCALE_CONFLICT", "value and unit scale markers disagree")
        # The scale in the value text has already been applied.  A duplicate
        # same-provenance unit marker is intentionally applied only once.
        scale_multiplier = Decimal(1)
    else:
        value_base *= scale_multiplier
        quantum *= scale_multiplier

    dimension, currency, normalized_unit = _unit_contract(base_unit)
    return CanonicalQuantity(
        value_base=value_base,
        dimension=dimension,
        currency=currency,
        normalized_unit=normalized_unit,
        scale_multiplier=scale_multiplier,
        source_value_text=value_source,
        source_unit_text=unit_source,
        precision_quantum=quantum,
        qualifier=qualifier,
        denominator=denominator,
        normalization_rule_id=NORMALIZATION_RULE_ID,
        provenance=dict(provenance or {}),
    )


def compare_canonical(
    claim: CanonicalQuantity,
    official: CanonicalQuantity,
    *,
    relative_tolerance: Decimal = Decimal("0.005"),
    precision_tolerance: bool = False,
) -> CanonicalComparison:
    """Compare compatible canonical quantities; qualifiers remain unresolved."""
    if claim.qualifier or official.qualifier:
        return CanonicalComparison(False, "QUALIFIER_UNSUPPORTED", None, None)
    if (claim.dimension, claim.currency, claim.denominator) != (
        official.dimension,
        official.currency,
        official.denominator,
    ):
        return CanonicalComparison(False, "UNIT_MISMATCH", None, None)
    difference = abs(claim.value_base - official.value_base)
    denominator = abs(official.value_base)
    relative = difference / denominator if denominator else (Decimal(0) if difference == 0 else Decimal("Infinity"))
    if precision_tolerance:
        matched = difference <= claim.precision_quantum / Decimal(2)
    else:
        matched = relative <= relative_tolerance
    return CanonicalComparison(matched, "MATCH" if matched else "VALUE_MISMATCH", difference, relative)


__all__ = [
    "CanonicalComparison",
    "CanonicalQuantity",
    "NORMALIZATION_RULE_ID",
    "QuantityNormalizationError",
    "compare_canonical",
    "normalize_quantity",
]
