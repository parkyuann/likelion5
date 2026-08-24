"""Deterministic, candidate-independent R4-C1 claim core (v2).

The v2 core deliberately contains claim language and provenance only.  It does
not know about KOSIS candidates, table identifiers, cells, scores, or values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Mapping


CONTRACT_VERSION = "r4c1-claim-core-v2"
CLAIM_CORE_CONTRACT_VERSION = CONTRACT_VERSION


@dataclass(frozen=True)
class ClaimAtom:
    role: str
    surface: Any
    status: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class ClaimCore:
    atoms: dict[str, ClaimAtom]
    proposal_view: dict[str, Any]
    provenance: dict[str, Any]
    contract_version: str
    canonical_sha256: str


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _surface_norm(value: Any) -> str:
    return re.sub(r"[\s\-_./:(),]+", "", _text(value)).casefold()


def _sentence_text(row: Mapping[str, Any], sentence_id: Any = None) -> str:
    current_sentence_id = row.get("article_sentence_id", row.get("sentence_id"))
    # A routed row may carry evidence from a different source/context
    # sentence. Do not return the target sentence for that foreign ID before
    # consulting the explicit sentence inventory.
    if sentence_id is not None and sentence_id != current_sentence_id:
        sentences = row.get("sentences")
        if isinstance(sentences, Mapping):
            value = sentences.get(sentence_id)
            if value is None:
                value = sentences.get(str(sentence_id))
            if isinstance(value, Mapping):
                value = value.get("text")
            if isinstance(value, str):
                return value
    for key in ("sentence_text", "article_sentence_text", "text"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    sentences = row.get("sentences")
    if isinstance(sentences, Mapping) and sentence_id is not None:
        value = sentences.get(sentence_id)
        if isinstance(value, Mapping):
            value = value.get("text")
        if isinstance(value, str):
            return value
    return ""


def _evidence_map(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _span_from_evidence(evidence: Mapping[str, Any] | None, *, fallback_text: str = "") -> dict[str, Any] | None:
    if not evidence:
        return None
    span = evidence.get("span")
    if isinstance(span, Mapping):
        start, end = span.get("start"), span.get("end")
        text = span.get("text", evidence.get("text", fallback_text))
    else:
        start, end = evidence.get("start"), evidence.get("end")
        text = evidence.get("text", fallback_text)
    if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end:
        return {"start": start, "end": end, "text": _text(text)}
    return None


def _validated_value_span(row: Mapping[str, Any], sentence: str, value_surface: str) -> dict[str, Any] | None:
    """Recover a value span only when the routed identity is self-verifying."""

    span_id = _text(row.get("value_span_id"))
    match = re.search(r":(?P<start>\d+)-(?P<end>\d+)$", span_id)
    if not match or not sentence or not value_surface:
        return None
    start, end = int(match.group("start")), int(match.group("end"))
    if not (0 <= start <= end <= len(sentence)):
        return None
    text = sentence[start:end]
    if text != value_surface:
        return None
    return {"start": start, "end": end, "text": text}


def _anchored_period_normalization(
    raw_surface: str, structured_surface: Any, article_date: Any
) -> dict[str, Any] | None:
    """Validate a small relative-period grammar against an article date."""

    raw = re.sub(r"\s+", " ", _text(raw_surface))
    structured = re.sub(r"\s+", " ", _text(structured_surface))
    date_match = re.fullmatch(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})", _text(article_date))
    if not raw or not structured or not date_match:
        return None
    anchor_year = int(date_match.group("year"))
    rule_id = source_granularity = expected = ""
    if raw in {"작년", "지난해"}:
        rule_id, source_granularity, expected = "anchor-previous-year", "YEAR", str(anchor_year - 1)
    elif raw == "올해":
        rule_id, source_granularity, expected = "anchor-current-year", "YEAR", str(anchor_year)
    else:
        quarter = re.fullmatch(r"올해\s*([1-4])분기", raw)
        month = re.fullmatch(r"(0?[1-9]|1[0-2])월", raw)
        if quarter:
            rule_id = "anchor-current-year-quarter"
            source_granularity = "QUARTER"
            expected = f"{anchor_year} {quarter.group(1)}분기"
        elif month:
            rule_id = "anchor-current-year-month"
            source_granularity = "MONTH"
            expected = f"{anchor_year}.{int(month.group(1)):02d}"
        else:
            return None
    if _surface_norm(expected) != _surface_norm(structured):
        return None
    return {
        "rule_id": rule_id,
        "rule_version": 1,
        "anchor_date": _text(article_date),
        "source_granularity": source_granularity,
        "lossless": True,
        "raw_text": raw,
        "structured_surface": structured,
    }


def _unique_occurrence(sentence: str, surface: str) -> dict[str, Any] | None:
    """Return a real sentence span when the normalized surface occurs once."""
    if not sentence or not surface:
        return None
    # Normalization is whitespace-insensitive but the returned span is always
    # in the original sentence, so downstream evidence remains inspectable.
    ns = re.sub(r"\s+", "", sentence).lower()
    target = re.sub(r"\s+", "", surface).lower()
    if not target:
        return None
    positions: list[tuple[int, int]] = []
    for match in re.finditer(re.escape(target), ns):
        # Build normalized-character -> original-character offsets.
        norm_to_orig = [i for i, ch in enumerate(sentence) if not ch.isspace()]
        start = norm_to_orig[match.start()]
        end = norm_to_orig[match.end() - 1] + 1
        positions.append((start, end))
    if len(positions) != 1:
        return None
    start, end = positions[0]
    return {"start": start, "end": end, "text": sentence[start:end]}


def _literal_occurrences(sentence: str, surface: str) -> list[dict[str, Any]]:
    if not sentence or not surface:
        return []
    return [
        {"start": match.start(), "end": match.end(), "text": match.group(0)}
        for match in re.finditer(re.escape(surface), sentence)
    ]


def _paired_indicator_span(
    row: Mapping[str, Any], sentence: str, surface: str, value_span: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Recompute the upstream SPAN_NEAREST relation without using values."""

    if (
        row.get("indicator_pairing") != "SPAN_NEAREST"
        or value_span is None
        or _text(row.get("indicator_span_text")) != _text(row.get("value_text"))
    ):
        return None
    candidates = [
        span
        for span in _literal_occurrences(sentence, surface)
        if span["end"] <= int(value_span["start"])
        and not re.search(r"[.!?。]", sentence[int(span["end"]):int(value_span["start"])])
    ]
    if not candidates:
        return None
    best_end = max(int(span["end"]) for span in candidates)
    best = [span for span in candidates if int(span["end"]) == best_end]
    return best[0] if len(best) == 1 else None


def _value_linked_year_span(sentence: str, value_span: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Bind the closest explicit year in the same clause before a value."""

    if value_span is None:
        return None
    value_start = int(value_span["start"])
    candidates = [
        {"start": match.start(), "end": match.end(), "text": match.group(0)}
        for match in re.finditer(r"(?<!\d)\d{4}년", sentence[:value_start])
        if not re.search(r"[.!?。]", sentence[match.end():value_start])
        and value_start - match.end() <= 80
    ]
    return candidates[-1] if candidates else None


def _claim_provenance(
    row: Mapping[str, Any], *, sentence_id: Any, span: Mapping[str, Any] | None,
    span_path: str | None = None, context_span: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "article_idx": row.get("article_idx"),
        "article_id": row.get("article_id"),
        "sentence_id": sentence_id,
        "span_path": span_path,
        "start": span.get("start") if span else None,
        "end": span.get("end") if span else None,
        "text": span.get("text") if span else None,
    }
    if context_span is not None:
        result["context_span"] = dict(context_span)
    return result


def _atom(role: str, surface: Any, status: str, provenance: dict[str, Any]) -> ClaimAtom:
    return ClaimAtom(role, surface, status, provenance)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def build_claim_core_v2(routed_value: Mapping[str, Any]) -> ClaimCore:
    row = routed_value
    fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}

    article_sentence_id = row.get("article_sentence_id", row.get("sentence_id"))
    indicator_sentence_id = row.get("indicator_sentence_id", article_sentence_id)
    value_surface = _text(row.get("value_text"))
    value_sentence_id = row.get("value_sentence_id", article_sentence_id)
    value_sentence = _sentence_text(row, value_sentence_id)
    explicit_value_span = _span_from_evidence(
        _evidence_map(row.get("value_evidence")), fallback_text=value_surface
    )
    value_span = explicit_value_span or _validated_value_span(row, value_sentence, value_surface)

    indicator_evidence = _evidence_map(row.get("indicator_evidence"))
    indicator_surface = _text((indicator_evidence or {}).get("surface") or fields.get("indicator") or row.get("indicator_label") or (indicator_evidence or {}).get("text"))
    indicator_span = _span_from_evidence(indicator_evidence, fallback_text=indicator_surface)
    indicator_sentence = _sentence_text(row, indicator_sentence_id)
    indicator_span_path = "indicator_evidence.span" if indicator_evidence else None
    if indicator_span is None:
        indicator_span = _unique_occurrence(indicator_sentence, indicator_surface)
        if indicator_span is not None:
            indicator_span_path = "sentence_text"
    if indicator_span is None:
        paired = _paired_indicator_span(row, indicator_sentence, indicator_surface, value_span)
        if paired is not None:
            indicator_span = paired
            indicator_span_path = "indicator_pairing.SPAN_NEAREST"
    indicator_context_span = None
    if indicator_span is None and indicator_surface:
        base_indicator = re.sub(r"\([^()]*\)", "", indicator_surface).strip()
        if base_indicator and base_indicator != indicator_surface:
            indicator_context_span = _unique_occurrence(indicator_sentence, base_indicator)
    indicator_status = "EXPLICIT" if indicator_evidence or indicator_span else ("INFERRED" if indicator_surface else "UNKNOWN")
    indicator_prov = _claim_provenance(
        row, sentence_id=indicator_sentence_id, span=indicator_span,
        span_path=indicator_span_path,
        context_span=indicator_context_span,
    )
    if indicator_sentence:
        indicator_prov["sentence_text"] = indicator_sentence

    value_span_path = (
        "value_evidence.span"
        if explicit_value_span is not None
        else ("value_span_id" if value_span is not None else row.get("value_span_id"))
    )
    value_prov = _claim_provenance(
        row,
        sentence_id=value_sentence_id,
        span=value_span,
        span_path=value_span_path,
    )
    if value_sentence:
        value_prov["sentence_text"] = value_sentence

    unit_surface = _text(row.get("value_unit") or fields.get("unit"))
    unit_sentence_id = row.get("unit_sentence_id", article_sentence_id)
    unit_sentence = _sentence_text(row, unit_sentence_id)
    unit_span = None
    unit_span_path = None
    if (
        value_span is not None
        and value_sentence_id == unit_sentence_id
        and value_surface.endswith(unit_surface)
        and unit_surface
    ):
        unit_start = int(value_span["end"]) - len(unit_surface)
        unit_end = int(value_span["end"])
        if unit_sentence[unit_start:unit_end] == unit_surface:
            unit_span = {"start": unit_start, "end": unit_end, "text": unit_surface}
            unit_span_path = "value_span_id.unit_suffix"
    if unit_span is None:
        unit_span = _unique_occurrence(unit_sentence, unit_surface)
        unit_span_path = "sentence_text.unit" if unit_span else None
    unit_prov = _claim_provenance(
        row,
        sentence_id=unit_sentence_id,
        span=unit_span,
        span_path=unit_span_path,
    )
    if unit_sentence:
        unit_prov["sentence_text"] = unit_sentence
    period_input = fields.get("period_absolute") if fields.get("period_absolute") not in (None, "") else row.get("period_raw")
    period_surface: Any = period_input if isinstance(period_input, Mapping) else _text(period_input)
    period_sentence_id = row.get("period_sentence_id", article_sentence_id)
    period_sentence = _sentence_text(row, period_sentence_id)
    linked_year_span = None
    if not period_surface and period_sentence_id == value_sentence_id:
        linked_year_span = _value_linked_year_span(period_sentence, value_span)
        if linked_year_span is not None:
            period_surface = linked_year_span["text"]
    raw_period_surface = _text(row.get("period_raw") or fields.get("period"))
    if linked_year_span is not None:
        raw_period_surface = linked_year_span["text"]
    period_span_surface = raw_period_surface or (_text(period_input) if not isinstance(period_input, Mapping) else _text(period_input.get("start_prd_de") or period_input.get("period")))
    period_span = linked_year_span or _unique_occurrence(period_sentence, period_span_surface)
    period_span_path = "value_span_id.preceding_explicit_year" if linked_year_span else ("sentence_text.period_raw" if period_span else None)
    period_prov = _claim_provenance(row, sentence_id=period_sentence_id, span=period_span, span_path=period_span_path)
    if period_sentence:
        period_prov["sentence_text"] = period_sentence
    period_prov["structured_surface"] = period_surface
    period_prov["derivation_input"] = {
        "period_raw": raw_period_surface,
        "period_absolute": fields.get("period_absolute"),
    }
    if linked_year_span is not None:
        period_prov["value_linked_normalization"] = {
            "rule_id": "value-linked-preceding-explicit-year",
            "rule_version": 1,
            "value_span_id": row.get("value_span_id"),
            "lossless": True,
        }
    anchored_period = _anchored_period_normalization(
        raw_period_surface, period_surface, row.get("article_date")
    )
    if anchored_period is not None:
        anchored_period["anchor_date_provenance"] = dict(
            row.get("article_date_provenance")
            if isinstance(row.get("article_date_provenance"), Mapping)
            else {}
        )
        period_prov["anchored_normalization"] = anchored_period

    population_raw = fields.get("population") or row.get("population") or ()
    if isinstance(population_raw, str):
        population = (population_raw,) if population_raw else ()
    else:
        population = tuple(sorted({_text(x) for x in population_raw if _text(x)})) if isinstance(population_raw, (list, tuple, set)) else ()
    population_prov = _claim_provenance(row, sentence_id=article_sentence_id, span=None)
    if indicator_span is not None:
        population_prov["context_span"] = dict(indicator_span)
    raw_population_evidence = row.get("population_evidence")
    population_evidence_values = [raw_population_evidence] if isinstance(raw_population_evidence, Mapping) else (list(raw_population_evidence) if isinstance(raw_population_evidence, (list, tuple)) else [])
    validated_population_evidence: list[dict[str, Any]] = []
    for evidence_index, evidence in enumerate(population_evidence_values):
        if not isinstance(evidence, Mapping):
            continue
        sentence_id = evidence.get("sentence_id", article_sentence_id)
        sentence = _sentence_text(row, sentence_id)
        span = _span_from_evidence(evidence, fallback_text=_text(evidence.get("surface")))
        surface = _text(evidence.get("surface") or evidence.get("text"))
        if not span or not sentence or not surface:
            continue
        start, end = span["start"], span["end"]
        slice_text = sentence[start:end] if 0 <= start <= end <= len(sentence) else ""
        if slice_text == span.get("text") and _surface_norm(slice_text) == _surface_norm(surface):
            validated_population_evidence.append({
                "article_idx": row.get("article_idx"),
                "article_id": row.get("article_id"),
                "sentence_id": sentence_id,
                "span_path": f"population_evidence[{evidence_index}]",
                "start": start,
                "end": end,
                "text": slice_text,
            })
    if validated_population_evidence:
        population_prov["evidence"] = validated_population_evidence

    # source_subtype is intentionally not evidence.  Only a structured region
    # evidence mapping can make this atom explicit.
    region_evidence = _evidence_map(row.get("region_evidence"))
    region_surface = _text(region_evidence.get("surface") if region_evidence else "")
    region_sentence_id = region_evidence.get("sentence_id") if region_evidence else None
    region_sentence = _sentence_text(row, region_sentence_id)
    region_span = _span_from_evidence(region_evidence, fallback_text=region_surface)
    # Structured evidence becomes EXPLICIT only when its coordinates and text
    # agree with the actual sentence slice and its surface label.
    if region_span and region_sentence:
        start, end = region_span["start"], region_span["end"]
        slice_text = region_sentence[start:end] if 0 <= start <= end <= len(region_sentence) else ""
        if slice_text != region_span.get("text") or _surface_norm(slice_text) != _surface_norm(region_surface):
            region_span = None
    else:
        region_span = None
    region_status = "EXPLICIT" if region_evidence and region_surface and region_span and region_sentence_id is not None else "UNKNOWN"
    region_prov = _claim_provenance(row, sentence_id=region_sentence_id, span=region_span, span_path="region_evidence.span" if region_span else None)
    if region_sentence:
        region_prov["sentence_text"] = region_sentence

    measurement = fields.get("measurement_type")
    measurement_status = "EXPLICIT" if measurement in {"LEVEL", "CHANGE_RATE", "CHANGE_POINT"} else "UNKNOWN"
    atoms = {
        "value": _atom("value", value_surface, "EXPLICIT" if value_surface else "UNKNOWN", value_prov),
        "unit": _atom("unit", unit_surface, "EXPLICIT" if unit_surface else "UNKNOWN", unit_prov),
        "indicator": _atom("indicator", indicator_surface, indicator_status, indicator_prov),
        "period": _atom("period", period_surface, "EXPLICIT" if period_surface else "UNKNOWN", period_prov),
        "population": _atom("population", population, "EXPLICIT" if population else "UNKNOWN", population_prov),
        "region": _atom("region", region_surface, region_status, region_prov),
        "measurement_op": _atom("measurement_op", measurement if measurement_status == "EXPLICIT" else "", measurement_status, _claim_provenance(row, sentence_id=article_sentence_id, span=None)),
        "comparison_op": _atom("comparison_op", "", "UNKNOWN", _claim_provenance(row, sentence_id=None, span=None)),
    }
    # Numeric claim values are retained as claim evidence but never projected.
    proposal = {
        "indicator": indicator_surface,
        "unit": unit_surface,
        "population": population,
        "region": region_surface,
        "period_shape": re.sub(r"[0-9]+(?:[.,][0-9]+)*", "", str(period_surface)),
        "measurement_op": measurement if measurement_status == "EXPLICIT" else "",
    }
    provenance = {
        "article_idx": row.get("article_idx"),
        "article_id": row.get("article_id"),
        "article_sentence_id": article_sentence_id,
        "value_span_id": row.get("value_span_id"),
    }
    data = {
        "atoms": {name: asdict(atom) for name, atom in atoms.items()},
        "proposal_view": proposal,
        "provenance": provenance,
        "contract_version": CONTRACT_VERSION,
    }
    digest = hashlib.sha256(_canonical_bytes(data)).hexdigest()
    return ClaimCore(atoms, proposal, provenance, CONTRACT_VERSION, digest)


# Short aliases are intentionally provided for callers migrating from v1.
build_claim_core = build_claim_core_v2


def claim_core_to_dict_v2(core: ClaimCore) -> dict[str, Any]:
    return asdict(core)


claim_core_to_dict = claim_core_to_dict_v2


def canonical_bytes(core_dict_without_sha: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(core_dict_without_sha)
