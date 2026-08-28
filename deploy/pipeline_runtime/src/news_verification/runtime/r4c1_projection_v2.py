"""Deterministic R4-C1 v2 candidate projection and target validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import re
from itertools import product
from typing import Any, Mapping, Sequence

from src.news_verification.runtime.r4c1_binding_proposer_v1 import propose_semantic_alias_matches


CONTRACT_VERSION = "r4c1-projection-v2"
PROJECTION_CONTRACT_VERSION = CONTRACT_VERSION
QUERY_FIELDS = ("org_id", "tbl_id", "itm_id", "prd_se", "start_prd_de", "end_prd_de", "obj_levels")
_INDICATOR_METRIC_SUFFIX_RE = re.compile(
    r"\s*(?:증가|감소|상승|하락|증감|변화|변동|성장)\s*(?:율|률|량|폭)$"
)


def _norm(value: Any) -> str:
    return re.sub(r"[\s\-_./:(),]+", "", str(value or "")).casefold()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _get_atom(core: Any, role: str) -> Mapping[str, Any]:
    atoms = core.get("atoms", {}) if isinstance(core, Mapping) else getattr(core, "atoms", {})
    atom = atoms.get(role, {}) if isinstance(atoms, Mapping) else {}
    return atom if isinstance(atom, Mapping) else (asdict(atom) if hasattr(atom, "__dataclass_fields__") else {})


def _get(atom: Mapping[str, Any], key: str, default: Any = None) -> Any:
    return atom.get(key, default)


def _norm_map(surface: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(surface):
        if not re.match(r"[\s\-_./:(),]+", char):
            chars.append(char.casefold())
            positions.append(index)
    return "".join(chars), positions


def _submatches(surface: str, label: str) -> list[tuple[int, int, str]]:
    """Find every contiguous normalized label occurrence in a claim surface."""
    normalized, positions = _norm_map(surface)
    target = _norm(label)
    if not normalized or not target:
        return []
    result: list[tuple[int, int, str]] = []
    for match in re.finditer(re.escape(target), normalized):
        start = positions[match.start()]
        end = positions[match.end() - 1] + 1
        result.append((start, end, surface[start:end]))
    return result


def _base_claim_prov(atom: Mapping[str, Any]) -> Mapping[str, Any]:
    prov = _get(atom, "provenance", {})
    return prov if isinstance(prov, Mapping) else {}


def _own_claim_span(atom: Mapping[str, Any]) -> dict[str, Any] | None:
    prov = _base_claim_prov(atom)
    span = {key: prov.get(key) for key in ("article_idx", "article_id", "sentence_id", "span_path", "start", "end", "text")}
    return span if _complete_span(span) else None


def _complete_span(value: Mapping[str, Any] | None) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("article_idx") is not None
        and value.get("sentence_id") is not None
        and isinstance(value.get("start"), int)
        and isinstance(value.get("end"), int)
        and value.get("end") >= value.get("start")
        and bool(value.get("text"))
    )


def _translate_surface_span(surface_source: str, parent_source: str, start: int, end: int) -> tuple[int, int] | None:
    """Translate original offsets when parent text differs only by spacing."""
    if 0 <= start <= end <= len(parent_source) and surface_source == parent_source:
        return start, end
    _, surface_positions = _norm_map(surface_source)
    _, parent_positions = _norm_map(parent_source)
    if not surface_positions or not parent_positions:
        return None
    normalized_start = sum(1 for position in surface_positions if position < start)
    normalized_end = sum(1 for position in surface_positions if position < end)
    if normalized_start >= len(parent_positions) or normalized_end <= 0 or normalized_end > len(parent_positions):
        return None
    return parent_positions[normalized_start], parent_positions[normalized_end - 1] + 1


def _subspan_provenance(atom: Mapping[str, Any], start: int, end: int, text: str, path: str) -> dict[str, Any] | None:
    prov = dict(_base_claim_prov(atom))
    sentence_id = prov.get("sentence_id")
    article_idx = prov.get("article_idx")
    sentence_text = prov.get("sentence_text")
    parent_start, parent_end = prov.get("start"), prov.get("end")
    parent_text = prov.get("text")
    if isinstance(parent_start, int) and isinstance(parent_end, int):
        # _submatches returns offsets in the original claim surface (not in
        # its whitespace-stripped representation).  Preserve those offsets
        # when translating into the article sentence.
        source = parent_text if isinstance(parent_text, str) and parent_text else str(_get(atom, "surface", ""))
        translated = _translate_surface_span(str(_get(atom, "surface", "")), source, start, end)
        if translated is not None:
            relative_start, relative_end = translated
            absolute_start = parent_start + relative_start
            absolute_end = parent_start + relative_end
            result = {
                "article_idx": article_idx,
                "article_id": prov.get("article_id"),
                "sentence_id": sentence_id,
                "span_path": path,
                "start": absolute_start,
                "end": absolute_end,
                "text": sentence_text[absolute_start:absolute_end] if isinstance(sentence_text, str) and absolute_end <= len(sentence_text) else text,
            }
            return result if _complete_span(result) else None
    # A sentence-level occurrence is still valid evidence if the core could
    # not precompute a whole-indicator span.
    if isinstance(sentence_text, str) and sentence_text:
        occurrences = _submatches(sentence_text, str(_get(atom, "surface", "")))
        if len(occurrences) == 1:
            pstart, pend, _ = occurrences[0]
            source = sentence_text[pstart:pend]
            translated = _translate_surface_span(str(_get(atom, "surface", "")), source, start, end)
            if translated is not None:
                relative_start, relative_end = translated
                absolute_start = pstart + relative_start
                absolute_end = pstart + relative_end
                result = {
                    "article_idx": article_idx,
                    "article_id": prov.get("article_id"),
                    "sentence_id": sentence_id,
                    "span_path": path,
                    "start": absolute_start,
                    "end": absolute_end,
                    "text": sentence_text[absolute_start:absolute_end],
                }
                return result if _complete_span(result) else None
        # An inferred indicator may contain a candidate-independent qualifier
        # that is absent from the sentence (for example ``(달러)``).  A unique
        # literal occurrence of the actually consumed proposal text still
        # supplies recomputable claim provenance.
        consumed_occurrences = _submatches(sentence_text, text)
        if len(consumed_occurrences) == 1:
            absolute_start, absolute_end, _ = consumed_occurrences[0]
            result = {
                "article_idx": article_idx,
                "article_id": prov.get("article_id"),
                "sentence_id": sentence_id,
                "span_path": path,
                "start": absolute_start,
                "end": absolute_end,
                "text": sentence_text[absolute_start:absolute_end],
            }
            return result if _complete_span(result) else None
    return None


def _context_provenance(atom: Mapping[str, Any], path: str) -> dict[str, Any] | None:
    prov = dict(_base_claim_prov(atom))
    start, end, text = prov.get("start"), prov.get("end"), prov.get("text")
    if not (isinstance(start, int) and isinstance(end, int) and end >= start and text):
        context = prov.get("context_span")
        if isinstance(context, Mapping):
            start, end, text = context.get("start"), context.get("end"), context.get("text")
    if not (isinstance(start, int) and isinstance(end, int) and end >= start and text):
        return None
    result = {
        "article_idx": prov.get("article_idx"),
        "article_id": prov.get("article_id"),
        "sentence_id": prov.get("sentence_id"),
        "span_path": path,
        "start": start,
        "end": end,
        "text": text,
    }
    return result if _complete_span(result) else None


def _indicator_subject_context_provenance(
    atom: Mapping[str, Any], path: str,
) -> dict[str, Any] | None:
    """Recover one source-backed subject span when the whole indicator span is absent."""
    provenance = _base_claim_prov(atom)
    sentence = provenance.get("sentence_text")
    indicator = str(_get(atom, "surface", "") or "").strip()
    if not isinstance(sentence, str) or not sentence or not indicator:
        return None
    subject = _INDICATOR_METRIC_SUFFIX_RE.sub("", indicator).strip() or indicator
    matches = _submatches(sentence, subject)
    if len(matches) != 1:
        return None
    start, end, text = matches[0]
    result = {
        "article_idx": provenance.get("article_idx"),
        "article_id": provenance.get("article_id"),
        "sentence_id": provenance.get("sentence_id"),
        "span_path": path,
        "start": start,
        "end": end,
        "text": text,
    }
    return result if _complete_span(result) else None


def _evidence(atom: Mapping[str, Any], *, profile_path: str, profile_label: Any, profile_id: Any,
              path: str, consumed: Mapping[str, Any] | None, rule: str, required: bool = True) -> dict[str, Any] | None:
    claim = dict(_base_claim_prov(atom))
    if consumed is not None:
        claim = dict(consumed)
        if not _complete_span(claim):
            return None
    elif required:
        claim = _context_provenance(atom, path) or {}
        if not claim:
            return None
        claim["consumed_span"] = None
    claim["consumed_span"] = dict(consumed) if consumed is not None else None
    return {
        "claim_provenance": claim,
        "profile_inventory_path": profile_path,
        "profile_label": str(profile_label),
        "profile_id": None if profile_id is None else str(profile_id),
        "match_rule": rule,
        # Flat fields make audit consumers independent of a nested schema.
        "span_path": claim.get("span_path"),
        "article_idx": claim.get("article_idx"),
        "sentence_id": claim.get("sentence_id"),
        "start": claim.get("start"),
        "end": claim.get("end"),
        "text": claim.get("text"),
        "consumed_span": dict(consumed) if consumed is not None else None,
    }


def _annotate_dimension_evidence(evidence: dict[str, Any], *, dindex: int,
                                dimension: Mapping[str, Any], vindex: int,
                                value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep axis and selected-value inventory provenance separately."""
    evidence["axis_evidence"] = {
        "profile_inventory_path": f"dimensions[{dindex}]",
        "profile_label": str(dimension.get("obj_nm")),
        "profile_id": str(dimension.get("obj_id")),
    }
    evidence["dimension_order"] = dindex + 1
    evidence["value_evidence"] = {
        "profile_inventory_path": f"dimensions[{dindex}].values[{vindex}]",
        "profile_label": str(value.get("value_name")),
        "profile_id": str(value.get("value_id")),
    }
    return evidence


def _overlaps(bindings: Sequence["AxisBinding"]) -> bool:
    spans: list[tuple[Any, int, int]] = []
    for binding in bindings:
        span = binding.evidence.get("consumed_span")
        if not isinstance(span, Mapping):
            continue
        start, end = span.get("start"), span.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        sentence = binding.evidence.get("sentence_id")
        for old_sentence, old_start, old_end in spans:
            if sentence == old_sentence and start < old_end and old_start < end:
                return True
        spans.append((sentence, start, end))
    return False


def _period_frequency(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if text in {"y", "year", "annual", "annually", "연", "년", "연간"}:
        return "Y"
    if text in {"m", "month", "monthly", "월", "월간"}:
        return "M"
    if text in {"q", "quarter", "quarterly", "분기", "분기별"}:
        return "Q"
    return None


def _parse_period(value: Any) -> tuple[str, str, str] | None:
    """Parse an absolute period with a full-match, frequency-aware grammar."""
    declared: str | None = None
    if isinstance(value, Mapping):
        declared = _period_frequency(value.get("prd_se") or value.get("frequency") or value.get("period_type"))
        raw_value = value.get("start_prd_de")
        if raw_value in (None, ""):
            raw_value = value.get("period")
        if raw_value in (None, ""):
            raw_value = value.get("value")
    else:
        raw_value = value
    raw = str(raw_value or "").strip()
    if not raw:
        return None

    def done(frequency: str, api: str) -> tuple[str, str, str] | None:
        if declared is not None and declared != frequency:
            return None
        return frequency, api, api

    # Explicit quarter month ranges are allowed only when they agree with Q.
    with_range = re.fullmatch(
        r"(?P<year>\d{4})\s*년?\s*(?P<q>[1-4])\s*분기\s*"
        r"\(\s*(?P<start>\d{1,2})\s*[~〜]\s*(?P<end>\d{1,2})\s*월\s*\)",
        raw,
        re.IGNORECASE,
    )
    if with_range:
        q = int(with_range.group("q"))
        if int(with_range.group("start")) != (q - 1) * 3 + 1 or int(with_range.group("end")) != q * 3:
            return None
        return done("Q", f"{with_range.group('year')}0{q}")

    korean_q = re.fullmatch(r"(?P<year>\d{4})\s*년?\s*(?P<q>[1-4])\s*분기", raw, re.IGNORECASE)
    if korean_q:
        return done("Q", f"{korean_q.group('year')}0{korean_q.group('q')}")
    slash_q = re.fullmatch(r"(?P<year>\d{4})\s*-?\s*(?P<q>[1-4])\s*/\s*4", raw)
    if slash_q:
        return done("Q", f"{slash_q.group('year')}0{slash_q.group('q')}")
    compact_q = re.fullmatch(r"(?P<year>\d{4})\s*-?\s*[Qq]\s*(?P<q>[1-4])", raw)
    if compact_q:
        return done("Q", f"{compact_q.group('year')}0{compact_q.group('q')}")

    dot_m = re.fullmatch(r"(?P<year>\d{4})\.(?P<month>\d{2})", raw)
    if dot_m and 1 <= int(dot_m.group("month")) <= 12:
        return done("M", f"{dot_m.group('year')}{int(dot_m.group('month')):02d}")
    compact_m = re.fullmatch(r"(?P<year>\d{4})(?P<month>0[1-9]|1[0-2])", raw)
    if compact_m:
        return done("M", f"{compact_m.group('year')}{compact_m.group('month')}")
    separator_m = re.fullmatch(
        r"(?P<year>\d{4})\s*(?:[-/]\s*|\s+)(?P<month>0?[1-9]|1[0-2])", raw
    )
    if separator_m:
        return done("M", f"{separator_m.group('year')}{int(separator_m.group('month')):02d}")

    year = re.fullmatch(r"(?P<year>\d{4})년?", raw)
    if year:
        return done("Y", year.group("year"))
    return None


def _period_order(frequency: str, api_period: str) -> int | None:
    """Return a comparable key; malformed bounds never become a sentinel."""
    text = str(api_period or "")
    if frequency == "Y" and re.fullmatch(r"\d{4}", text):
        return int(text) * 100
    if frequency == "M" and re.fullmatch(r"\d{4}(?:0[1-9]|1[0-2])", text):
        return int(text)
    if frequency == "Q" and re.fullmatch(r"\d{4}0[1-4]", text):
        return int(text[:4]) * 10 + int(text[-1])
    return None


def _claim_period(period_atom: Mapping[str, Any]) -> tuple[tuple[str, str, str] | None, dict[str, Any] | None, str | None]:
    """Require raw period evidence to be absolute and lossless."""
    provenance = _base_claim_prov(period_atom)
    structured_surface = _get(period_atom, "surface", "")
    structured = _parse_period(structured_surface)
    if structured is None:
        return None, None, "PERIOD_INVALID"
    derivation = provenance.get("derivation_input")
    derivation = derivation if isinstance(derivation, Mapping) else {}
    period_raw = derivation.get("period_raw")
    has_raw_period = period_raw not in (None, "")
    anchored = provenance.get("anchored_normalization")
    anchored = anchored if isinstance(anchored, Mapping) else None
    if anchored is not None:
        anchored_structured = _parse_period(anchored.get("structured_surface"))
        required = (
            anchored.get("rule_id"),
            anchored.get("rule_version"),
            anchored.get("anchor_date"),
            anchored.get("source_granularity"),
        )
        if (
            anchored.get("lossless") is not True
            or not all(required)
            or anchored_structured is None
            or anchored_structured[:2] != structured[:2]
        ):
            return None, None, "PERIOD_INVALID"
        frequency, api_period, _ = structured
        return structured, {
            "rule_id": str(anchored["rule_id"]),
            "rule_version": int(anchored["rule_version"]),
            "source_granularity": str(anchored["source_granularity"]),
            "lossless": True,
            "normalized_frequency": frequency,
            "normalized_start": api_period,
            "normalized_end": api_period,
            "raw_source_path": "retrieval_fields.period",
            "raw_text": str(anchored.get("raw_text") or ""),
            "anchor_date_provenance": dict(anchored.get("anchor_date_provenance") or {}),
        }, None

    # Dot-month and Korean quarter surfaces are newly anchored syntax.  They
    # may only enter runtime when the upstream raw span is present and parses
    # to the identical absolute period.  Older Y/M/Q spellings retain their
    # established behaviour when only period_absolute is available.
    if isinstance(structured_surface, Mapping):
        structured_text = structured_surface.get("start_prd_de")
        if structured_text in (None, ""):
            structured_text = structured_surface.get("period") or structured_surface.get("value")
    else:
        structured_text = structured_surface
    structured_text = str(structured_text or "").strip()
    new_dot_month = re.fullmatch(r"\d{4}\.\d{2}", structured_text) is not None
    new_korean_quarter = re.fullmatch(
        r"\d{4}\s*년?\s*[1-4]\s*분기(?:\s*\(\s*\d{1,2}\s*[~〜]\s*\d{1,2}\s*월\s*\))?",
        structured_text,
    ) is not None
    if (new_dot_month or new_korean_quarter) and not has_raw_period:
        return None, None, "PERIOD_INVALID"

    raw_value = period_raw
    raw_path = "period_raw"
    if not has_raw_period:
        raw_value = derivation.get("period_absolute")
        raw_path = "retrieval_fields.period_absolute"
    if raw_value not in (None, ""):
        raw_parsed = _parse_period(raw_value)
        if raw_parsed is None or raw_parsed[:2] != structured[:2]:
            return None, None, "PERIOD_INVALID"
    frequency, api_period, _ = structured
    normalization = {
        "rule_id": "absolute-period-fullmatch",
        "rule_version": 1,
        "source_granularity": {"Y": "YEAR", "M": "MONTH", "Q": "QUARTER"}[frequency],
        "lossless": True,
        "normalized_frequency": frequency,
        "normalized_start": api_period,
        "normalized_end": api_period,
        "raw_source_path": raw_path if raw_value not in (None, "") else None,
        "raw_text": None if raw_value in (None, "") else str(raw_value),
        "anchor_date_provenance": None,
    }
    return structured, normalization, None


def _profile_periods(profile: Mapping[str, Any], claim_period: Any) -> tuple[list[dict[str, Any]], str | None]:
    # Callers may pass the already validated normalized tuple.  Keeping the
    # parser here as a fallback preserves the public helper's old behaviour,
    # while projection itself never re-parses a lossy structured period.
    parsed = claim_period if (
        isinstance(claim_period, tuple)
        and len(claim_period) == 3
        and all(isinstance(value, str) for value in claim_period)
    ) else _parse_period(claim_period)
    if parsed is None:
        return [], "PERIOD_INVALID"
    frequency, api, _ = parsed
    candidates: list[dict[str, Any]] = []
    same_frequency = False
    valid_bounds = False
    for index, period in enumerate(profile.get("periods") or []):
        if not isinstance(period, Mapping):
            continue
        pf = _period_frequency(period.get("PRD_SE") or period.get("prd_se") or period.get("frequency"))
        if pf != frequency:
            continue
        same_frequency = True
        start = str(period.get("STRT_PRD_DE") or period.get("start_prd_de") or "")
        end = str(period.get("END_PRD_DE") or period.get("end_prd_de") or "")
        if not start or not end:
            continue
        start_parsed = _parse_period({"prd_se": frequency, "start_prd_de": start})
        end_parsed = _parse_period({"prd_se": frequency, "start_prd_de": end})
        start_key = _period_order(frequency, start_parsed[1]) if start_parsed else None
        end_key = _period_order(frequency, end_parsed[1]) if end_parsed else None
        request_key = _period_order(frequency, api)
        if start_key is None or end_key is None or request_key is None or start_key > end_key:
            continue
        valid_bounds = True
        if start_key <= request_key <= end_key:
            candidates.append({"index": index, "prd_se": frequency, "start": start, "end": end, "api": api})
    if candidates:
        # All matching range rows produce the same API request; retain one.
        return [sorted(candidates, key=lambda x: (x["start"], x["end"], x["index"]))[0]], None
    if same_frequency:
        return [], "PERIOD_OUT_OF_RANGE" if valid_bounds else "PROFILE_INCOMPLETE"
    return [], "PERIOD_FREQUENCY_MISMATCH"


@dataclass(frozen=True)
class AxisBinding:
    axis_kind: str
    axis_id: str
    value_id: str | None
    bound_atom: str
    binding_basis: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class CandidateAssignment:
    table_key: str
    bindings: tuple[AxisBinding, ...]

    @property
    def signature(self) -> tuple[Any, ...]:
        def binding_identity(binding: AxisBinding) -> tuple[Any, ...]:
            evidence = binding.evidence
            consumed = evidence.get("consumed_span") if isinstance(evidence, Mapping) else None
            def stable(value: Any) -> str:
                return "" if value is None else str(value)
            return (
                stable(binding.axis_kind),
                stable(binding.axis_id),
                stable(binding.value_id),
                stable(binding.bound_atom),
                stable(evidence.get("profile_inventory_path") if isinstance(evidence, Mapping) else None),
                stable(evidence.get("profile_id") if isinstance(evidence, Mapping) else None),
                stable(consumed.get("sentence_id") if isinstance(consumed, Mapping) else None),
                stable(consumed.get("start") if isinstance(consumed, Mapping) else None),
                stable(consumed.get("end") if isinstance(consumed, Mapping) else None),
            )
        return (
            self.table_key,
            tuple(sorted(binding_identity(b) for b in self.bindings)),
        )


@dataclass(frozen=True)
class CandidateProjection:
    table_key: str
    assignments: tuple[CandidateAssignment, ...]
    abstained: tuple[tuple[str, str], ...]
    projection_status: str
    hold_reasons: tuple[str, ...]
    canonical_sha256: str
    slot_diagnostics: tuple[Mapping[str, Any], ...] = ()

    @property
    def contract_version(self) -> str:
        return CONTRACT_VERSION

    @property
    def bindings(self) -> tuple[AxisBinding, ...]:
        return self.assignments[0].bindings if len(self.assignments) == 1 else ()


@dataclass(frozen=True)
class TargetResolution:
    outcome: str
    hold_reason: str | None
    query_plan: dict[str, Any] | None
    chosen_table_key: str | None
    compatible_series: tuple[Any, ...]
    audit: dict[str, Any]
    canonical_sha256: str


def _profile_table(profile: Mapping[str, Any]) -> tuple[str, str, str] | None:
    table = str(profile.get("table_key") or "").strip()
    supplied_org = str(profile.get("org_id") or "").strip()
    supplied_tbl = str(profile.get("tbl_id") or "").strip()
    if table and ":" not in table:
        return None
    if table and supplied_org and supplied_tbl and table != f"{supplied_org}:{supplied_tbl}":
        return None
    if ":" not in table:
        org, tbl = supplied_org, supplied_tbl
        table = f"{org}:{tbl}" if org and tbl else ""
    if table.count(":") != 1:
        return None
    org, tbl = table.split(":", 1)
    if not org or not tbl:
        return None
    return table, org, tbl


def _incomplete(profile: Mapping[str, Any]) -> bool:
    if _profile_table(profile) is None:
        return True
    items = profile.get("items")
    dimensions = profile.get("dimensions")
    periods = profile.get("periods")
    if not isinstance(items, list) or not items or not isinstance(dimensions, list) or not dimensions or len(dimensions) > 8 or not isinstance(periods, list) or not periods:
        return True
    if any(not isinstance(item, Mapping) or not item.get("itm_id") or not item.get("itm_nm") for item in items):
        return True
    for dim in dimensions:
        if not isinstance(dim, Mapping) or not dim.get("obj_id") or not dim.get("obj_nm"):
            return True
        values = dim.get("values")
        if not isinstance(values, list) or not values or any(not isinstance(v, Mapping) or not v.get("value_id") or not v.get("value_name") for v in values):
            return True
    return False


def _selected_series_unit_source(
    item: Mapping[str, Any],
    item_index: int,
    dimension_bindings: Sequence[AxisBinding],
    dimensions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str] | None, bool]:
    """Return the one official unit carried by the selected full series.

    The KOSIS ITM metadata endpoint can place UNIT_NM on the top-level ITEM or
    on a selected dimension value.  A unit is usable only when every non-empty
    source selected by the assignment agrees.  Conflicting source units fail
    closed instead of being resolved by inventory order.
    """

    sources: list[dict[str, str]] = []
    item_unit = str(item.get("unit_nm") or "").strip()
    if item_unit:
        sources.append(
            {
                "profile_inventory_path": f"items[{item_index}].unit_nm",
                "profile_label": item_unit,
                "profile_id": str(item.get("unit_id") or item.get("itm_id") or ""),
            }
        )
    for binding in dimension_bindings:
        for dindex, dimension in enumerate(dimensions):
            if str(dimension.get("obj_id") or "") != binding.axis_id:
                continue
            for vindex, value in enumerate(dimension.get("values") or []):
                if str(value.get("value_id") or "") != str(binding.value_id or ""):
                    continue
                value_unit = str(value.get("unit_nm") or "").strip()
                if value_unit:
                    sources.append(
                        {
                            "profile_inventory_path": f"dimensions[{dindex}].values[{vindex}].unit_nm",
                            "profile_label": value_unit,
                            "profile_id": str(value.get("unit_id") or value.get("value_id") or ""),
                        }
                    )
                break
            break
    normalized = {_norm(source["profile_label"]) for source in sources}
    if len(normalized) > 1:
        return None, True
    return (sources[0], False) if sources else (None, False)


_KRW_UNIT_SCALE = {
    "원": 1,
    "천원": 1_000,
    "만원": 10_000,
    "백만원": 1_000_000,
    "억원": 100_000_000,
    "조원": 1_000_000_000_000,
}


def _unit_compatibility(claim_unit: Any, profile_unit: Any) -> dict[str, Any] | None:
    claim = str(claim_unit or "").strip()
    profile = str(profile_unit or "").strip()
    if not claim or not profile:
        return None
    if _norm(claim) == _norm(profile):
        return {"rule_id": "unit-exact", "rule_version": 1, "family": "EXACT"}
    if claim in _KRW_UNIT_SCALE and profile in _KRW_UNIT_SCALE:
        return {
            "rule_id": "krw-scale-compatible",
            "rule_version": 1,
            "family": "KRW",
            "claim_unit": claim,
            "profile_unit": profile,
            "claim_to_base_factor": _KRW_UNIT_SCALE[claim],
            "profile_to_base_factor": _KRW_UNIT_SCALE[profile],
        }
    return None


def _make_projection(
    table: str,
    assignments: Sequence[CandidateAssignment],
    abstained: Sequence[tuple[str, str]],
    reasons: Sequence[str],
    slot_diagnostics: Sequence[Mapping[str, Any]] = (),
) -> CandidateProjection:
    ordered_assignments = tuple(sorted(assignments, key=lambda a: a.signature))
    data = {
        "table_key": table,
        "assignments": [
            {"table_key": a.table_key, "bindings": [asdict(b) for b in a.bindings]}
            for a in ordered_assignments
        ],
        "abstained": sorted(set(abstained)),
        "projection_status": "PROJECTED" if ordered_assignments else "ABSTAIN",
        "hold_reasons": tuple(sorted(set(reasons))),
        "slot_diagnostics": [dict(item) for item in slot_diagnostics],
    }
    return CandidateProjection(table, ordered_assignments, tuple(sorted(set(abstained))), data["projection_status"], tuple(sorted(set(reasons))), _sha(data), tuple(dict(item) for item in slot_diagnostics))


def _slot_diagnostics(
    table: str,
    profile: Mapping[str, Any] | None,
    assignments: Sequence[CandidateAssignment],
    abstained: Sequence[tuple[str, str]],
    reasons: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    """Expose missing-slot evidence without turning it into a selector result."""
    result: list[dict[str, Any]] = []
    known_roles = {str(kind) for kind, _ in abstained}
    role_reason = {
        "ITEM": "indicator", "PERIOD": "period", "DIMENSION": "classification",
        "UNIT": "unit",
    }
    if assignments:
        bound = {str(binding.axis_kind) for assignment in assignments for binding in assignment.bindings}
        for kind in ("ITEM", "PERIOD", "UNIT"):
            if kind not in bound:
                known_roles.add(kind)
    dimensions = profile.get("dimensions") if isinstance(profile, Mapping) and isinstance(profile.get("dimensions"), list) else []
    for dimension in dimensions:
        if not isinstance(dimension, Mapping):
            continue
        label = str(dimension.get("obj_nm") or dimension.get("name") or "")
        normalized = re.sub(r"\s+", "", label)
        matches = [
            role for role, terms in (("region", ("행정구역", "지역", "시도")), ("sex", ("성별", "성")), ("age", ("연령", "연령별", "나이")))
            if any(term in normalized for term in terms)
        ]
        axis_role = matches[0] if len(matches) == 1 else "classification"
        values = dimension.get("values") if isinstance(dimension.get("values"), list) else []
        inventory = []
        for value in values:
            if isinstance(value, Mapping):
                inventory.append({"label": str(value.get("obj_nm") or value.get("value_nm") or value.get("name") or ""), "axis_id": dimension.get("obj_id"), "value_id": value.get("value_id") or value.get("obj_id")})
        status = "RESOLVED" if any(getattr(binding, "axis_id", None) == str(dimension.get("obj_id") or "") for assignment in assignments for binding in assignment.bindings) else "MISSING"
        result.append({
            "role": axis_role, "status": status, "table_key": table,
            "profile_sha256": profile.get("profile_sha256") if isinstance(profile, Mapping) else None,
            "axis_semantic_role": axis_role, "axis_inventory_path": "dimensions",
            "option_inventory": inventory, "reason": "DIMENSION_UNBOUND" if status != "RESOLVED" else "",
        })
    for kind, reason in sorted(set(abstained)):
        role = role_reason.get(str(kind), "classification")
        result.append({"role": role, "status": "AMBIGUOUS" if "AMBIG" in str(reason) else "MISSING", "table_key": table, "profile_sha256": profile.get("profile_sha256") if isinstance(profile, Mapping) else None, "axis_semantic_role": role, "axis_inventory_path": None, "option_inventory": [], "reason": str(reason)})
    for reason in sorted(set(reasons)):
        role = "period" if str(reason).startswith("PERIOD") else "unit" if str(reason).startswith("UNIT") else None
        if role and not any(item.get("role") == role for item in result):
            result.append({"role": role, "status": "UNSUPPORTED" if "UNSUPPORTED" in str(reason) else "MISSING", "table_key": table, "profile_sha256": profile.get("profile_sha256") if isinstance(profile, Mapping) else None, "axis_semantic_role": role, "axis_inventory_path": None, "option_inventory": [], "reason": str(reason)})
    return tuple(result)


def _prune_strictly_subsumed_axis_matches(options: Sequence[AxisBinding]) -> list[AxisBinding]:
    """Drop partial lexical matches dominated by a containing same-axis span.

    Equal spans, disjoint spans, and overlapping-but-not-containing spans are
    preserved, so duplicate IDs and genuinely separate occurrences still
    reach the global ambiguity validator.
    """

    kept: list[AxisBinding] = []
    for option in options:
        start, end = option.evidence.get("start"), option.evidence.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            kept.append(option)
            continue
        dominators = [
            other
            for other in options
            if other is not option
            and other.axis_id == option.axis_id
            and isinstance(other.evidence.get("start"), int)
            and isinstance(other.evidence.get("end"), int)
            and other.evidence["start"] <= start
            and other.evidence["end"] >= end
            and (other.evidence["start"], other.evidence["end"]) != (start, end)
        ]
        if dominators:
            continue
        kept.append(option)
    return kept


def _geographic_nationwide_default(
    indicator_atom: Mapping[str, Any],
    *,
    dindex: int,
    dimension: Mapping[str, Any],
    values: Sequence[Mapping[str, Any]],
) -> AxisBinding | None:
    """Return one disclosed nationwide default for an unqualified geo axis."""
    axis = _norm(dimension.get("obj_nm"))
    if not any(token in axis for token in ("행정구역", "지역", "시도")):
        return None
    totals = [
        (index, value)
        for index, value in enumerate(values)
        if _norm(value.get("value_name")) in {"전국", "전체", "총계", "계"}
    ]
    if len(totals) != 1:
        return None
    vindex, value = totals[0]
    context = _context_provenance(indicator_atom, "indicator.unqualified_nationwide_scope")
    if context is None:
        context = _indicator_subject_context_provenance(
            indicator_atom, "indicator.unqualified_nationwide_scope"
        )
    context_atom = dict(indicator_atom)
    context_atom["provenance"] = {
        **dict(_base_claim_prov(indicator_atom)),
        **({"context_span": context} if context is not None else {}),
    }
    evidence = _evidence(
        context_atom,
        profile_path=f"dimensions[{dindex}].values[{vindex}]",
        profile_label=value.get("value_name"),
        profile_id=value.get("value_id"),
        path="indicator.unqualified_nationwide_scope",
        consumed=None,
        rule="unqualified-geographic-axis-nationwide",
    )
    if evidence is None:
        return None
    evidence["inference_disclosure"] = {
        "rule_id": "unqualified-geographic-axis-nationwide",
        "rule_version": 1,
        "assumption": "UNQUALIFIED_QUERY_MEANS_NATIONWIDE",
    }
    evidence["dimension_order"] = dindex + 1
    return AxisBinding(
        "DIMENSION",
        str(dimension["obj_id"]),
        str(value["value_id"]),
        "inferred_scope",
        "DISCLOSED_NATIONWIDE_DEFAULT",
        _annotate_dimension_evidence(
            evidence, dindex=dindex, dimension=dimension, vindex=vindex, value=value
        ),
    )


def project_candidate_v2(
    claim_core: Any,
    profile: Mapping[str, Any] | None,
    *,
    allow_unqualified_nationwide: bool = False,
) -> CandidateProjection:
    if profile is None:
        return _make_projection("", (), (), ("PROFILE_UNAVAILABLE",), ({"role": "classification", "status": "PROFILE_INCOMPLETE", "reason": "PROFILE_UNAVAILABLE", "option_inventory": []},))
    table_info = _profile_table(profile)
    table = table_info[0] if table_info else ""
    if _incomplete(profile):
        return _make_projection(table, (), (), ("PROFILE_INCOMPLETE",), ({"role": "classification", "status": "PROFILE_INCOMPLETE", "table_key": table, "profile_sha256": profile.get("profile_sha256"), "reason": "PROFILE_INCOMPLETE", "option_inventory": []},))

    items = list(profile["items"])
    dimensions = list(profile["dimensions"])
    indicator_atom = _get_atom(claim_core, "indicator")
    indicator = str(_get(indicator_atom, "surface", "") or "")
    unit_atom = _get_atom(claim_core, "unit")
    period_atom = _get_atom(claim_core, "period")
    region_atom = _get_atom(claim_core, "region")
    population_atom = _get_atom(claim_core, "population")
    abstained: list[tuple[str, str]] = []
    reasons: list[str] = []

    # ITEM candidates: a real subspan is preferred; generic fallback is legal
    # only for a singleton inventory and has no consumed indicator span.
    item_options: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = []
    for idx, item in enumerate(items):
        for start, end, text in _submatches(indicator, str(item.get("itm_nm"))):
            prov = _subspan_provenance(indicator_atom, start, end, text, f"indicator.item[{idx}]")
            if prov is not None:
                item_options.append((item, prov, "indicator_item_subspan"))
        for proposal in propose_semantic_alias_matches(
            indicator,
            item.get("itm_nm"),
            allow_parenthetical_base=allow_unqualified_nationwide,
        ):
            prov = _subspan_provenance(
                indicator_atom,
                proposal.start,
                proposal.end,
                proposal.text,
                f"indicator.item[{idx}].semantic_alias",
            )
            if prov is not None:
                item_options.append((item, prov, proposal.rule_id))
    if not item_options:
        if len(items) == 1:
            context = _context_provenance(indicator_atom, "indicator.generic_item")
            if context is not None:
                item_options.append((items[0], None, "SINGLETON_INVENTORY"))
            else:
                reasons.append("CLAIM_PROVENANCE_MISSING")
        else:
            abstained.append(("ITEM", "NO_SUBSPAN_MATCH"))
    item_options = list({(str(i.get("itm_id")), p.get("start") if p else None, p.get("end") if p else None): (i, p, r) for i, p, r in item_options}.values())

    # Build ITEM bindings first.  Unit compatibility is checked later against
    # the complete ITEM + DIMENSION assignment because KOSIS may attach a
    # series unit to a selected dimension value.
    checked_items: list[tuple[dict[str, Any], dict[str, Any] | None, str, AxisBinding]] = []
    for item, prov, rule in item_options:
        profile_index = next((index for index, candidate in enumerate(items) if candidate is item), items.index(item))
        item_evidence = _evidence(indicator_atom, profile_path=f"items[{profile_index}].itm_nm", profile_label=item.get("itm_nm"), profile_id=item.get("itm_id"), path="indicator", consumed=prov, rule=rule)
        if item_evidence is None:
            reasons.append("CLAIM_PROVENANCE_MISSING")
            continue
        checked_items.append((item, prov, rule, AxisBinding("ITEM", str(item["itm_id"]), None, "indicator", "EXACT_LABEL" if prov else "SINGLETON_INVENTORY", {
            **item_evidence,
            "consumed_span": prov,
        })))
    if not checked_items and not item_options:
        reasons.append("NO_COMPATIBLE_SERIES")

    # Period is structured and frequency-aware.  The raw period provenance is
    # a lossless gate for newly anchored syntax: a derived structured value
    # (for example raw ``5월 셋째 주`` -> ``2025.05``) is never silently
    # promoted to a monthly query.
    period_options: list[AxisBinding] = []
    if _get(period_atom, "status") in {"EXPLICIT", "INFERRED"}:
        parsed_period, period_normalization, claim_period_reason = _claim_period(period_atom)
        if claim_period_reason:
            matched_periods, period_reason = [], claim_period_reason
        else:
            matched_periods, period_reason = _profile_periods(profile, parsed_period)
        if period_reason:
            reasons.append(period_reason)
        for p in matched_periods:
            period_span = _own_claim_span(period_atom)
            evidence = _evidence(period_atom, profile_path=f"periods[{p['index']}].PRD_SE", profile_label=p["prd_se"], profile_id=p["prd_se"], path="period", consumed=period_span, rule="period_frequency_and_range") if period_span is not None else None
            if evidence is not None:
                if period_normalization is not None:
                    evidence["period_normalization"] = dict(period_normalization)
                period_options.append(AxisBinding("PERIOD", p["prd_se"], p["api"], "period", "PERIOD_RANGE_COMPATIBLE", evidence))
            else:
                reasons.append("CLAIM_PROVENANCE_MISSING")
    else:
        reasons.append("PERIOD_UNKNOWN")

    # Each dimension must resolve to a value.  Values/axes may be found in the
    # indicator, while explicit region/population atoms add their own claims.
    populations = tuple(_get(population_atom, "surface", ()) or ())
    dim_options: list[list[AxisBinding]] = []
    region_options: list[tuple[int, AxisBinding]] = []
    population_options: list[tuple[str, int, AxisBinding]] = []
    population_targets: dict[str, list[tuple[int, int]]] = {str(pop): [] for pop in populations}
    for dindex, dimension in enumerate(dimensions):
        values = list(dimension["values"])
        options: list[AxisBinding] = []
        axis_matches = _submatches(indicator, str(dimension.get("obj_nm")))
        for vindex, value in enumerate(values):
            value_matches = _submatches(indicator, str(value.get("value_name")))
            semantic_matches = propose_semantic_alias_matches(
                indicator,
                value.get("value_name"),
                allow_parenthetical_base=allow_unqualified_nationwide,
            )
            # Explicit region is a constraint on exactly one dimension/value;
            # it is enumerated globally instead of being imposed on every axis.
            if _get(region_atom, "status") == "EXPLICIT":
                if _norm(_get(region_atom, "surface", "")) == _norm(value.get("value_name")):
                    rp = _base_claim_prov(region_atom)
                    region_evidence = _evidence(region_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]", profile_label=value.get("value_name"), profile_id=value.get("value_id"), path="region_evidence", consumed=dict(rp), rule="region_evidence_dimension_value")
                    if region_evidence is not None:
                        region_options.append((dindex, AxisBinding("DIMENSION", str(dimension["obj_id"]), str(value["value_id"]), "region", "EXACT_LABEL", _annotate_dimension_evidence(region_evidence, dindex=dindex, dimension=dimension, vindex=vindex, value=value))))
                    else:
                        reasons.append("CLAIM_PROVENANCE_MISSING")
            population_matches = []
            if populations:
                population_evidence = _base_claim_prov(population_atom).get("evidence", ())
                for pop in populations:
                    if _norm(pop) == _norm(value.get("value_name")):
                        population_targets.setdefault(str(pop), []).append((dindex, vindex))
                        matches = [dict(pp) for pp in population_evidence if isinstance(pp, Mapping) and _norm(pp.get("text")) == _norm(pop)]
                        population_matches.extend(matches or [None])
            if population_matches:
                for population_span in population_matches:
                    if population_span is None:
                        reasons.append("CLAIM_PROVENANCE_MISSING")
                        continue
                    evidence = _evidence(population_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]", profile_label=value.get("value_name"), profile_id=value.get("value_id"), path="population", consumed=population_span, rule="population_dimension_value")
                    if evidence:
                        evidence["dimension_order"] = dindex + 1
                        population_options.append((str(next((pop for pop in populations if _norm(pop) == _norm(value.get("value_name"))), "")), dindex, AxisBinding("DIMENSION", str(dimension["obj_id"]), str(value["value_id"]), "population", "EXACT_LABEL", _annotate_dimension_evidence(evidence, dindex=dindex, dimension=dimension, vindex=vindex, value=value))))
            if value_matches:
                for start, end, text in value_matches:
                    vp = _subspan_provenance(indicator_atom, start, end, text, f"indicator.dimensions[{dindex}].values[{vindex}]")
                    if vp is not None:
                        evidence = _evidence(indicator_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]", profile_label=value.get("value_name"), profile_id=value.get("value_id"), path="indicator", consumed=vp, rule="indicator_dimension_value")
                        if evidence:
                            evidence["dimension_order"] = dindex + 1
                            options.append(AxisBinding("DIMENSION", str(dimension["obj_id"]), str(value["value_id"]), "indicator", "EXACT_LABEL", _annotate_dimension_evidence(evidence, dindex=dindex, dimension=dimension, vindex=vindex, value=value)))
            for proposal in semantic_matches:
                vp = _subspan_provenance(
                    indicator_atom,
                    proposal.start,
                    proposal.end,
                    proposal.text,
                    f"indicator.dimensions[{dindex}].values[{vindex}].semantic_alias",
                )
                if vp is not None:
                    evidence = _evidence(
                        indicator_atom,
                        profile_path=f"dimensions[{dindex}].values[{vindex}]",
                        profile_label=value.get("value_name"),
                        profile_id=value.get("value_id"),
                        path="indicator",
                        consumed=vp,
                        rule=proposal.rule_id,
                    )
                    if evidence:
                        evidence["match_rule_version"] = proposal.rule_version
                        evidence["dimension_order"] = dindex + 1
                        options.append(
                            AxisBinding(
                                "DIMENSION",
                                str(dimension["obj_id"]),
                                str(value["value_id"]),
                                "indicator",
                                "SEMANTIC_ALIAS",
                                _annotate_dimension_evidence(
                                    evidence,
                                    dindex=dindex,
                                    dimension=dimension,
                                    vindex=vindex,
                                    value=value,
                                ),
                            )
                        )
            if not value_matches and not semantic_matches and axis_matches and len(values) == 1:
                for start, end, text in axis_matches:
                    ap = _subspan_provenance(indicator_atom, start, end, text, f"indicator.dimensions[{dindex}]")
                    if ap is not None:
                        evidence = _evidence(indicator_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]", profile_label=value.get("value_name"), profile_id=value.get("value_id"), path="indicator", consumed=ap, rule="axis_label_singleton_value")
                        if evidence:
                            evidence["dimension_order"] = dindex + 1
                            options.append(AxisBinding("DIMENSION", str(dimension["obj_id"]), str(value["value_id"]), "indicator", "NORMALIZED_CONTAINMENT", _annotate_dimension_evidence(evidence, dindex=dindex, dimension=dimension, vindex=vindex, value=value)))
        options = _prune_strictly_subsumed_axis_matches(options)
        if (
            not options
            and allow_unqualified_nationwide
            and _get(region_atom, "status") != "EXPLICIT"
        ):
            nationwide = _geographic_nationwide_default(
                indicator_atom, dindex=dindex, dimension=dimension, values=values
            )
            if nationwide is not None:
                options.append(nationwide)
        if not options:
            abstained.append(("DIMENSION", f"UNBOUND:{dimension.get('obj_id')}"))
        dim_options.append(list({(b.axis_id, b.value_id, b.evidence.get("start"), b.evidence.get("end")): b for b in options}.values()))
    if _get(region_atom, "status") == "EXPLICIT" and not region_options:
        reasons.append("REGION_UNBOUND")
    if populations:
        for pop in populations:
            choices = [option for option in population_options if option[0] == str(pop)]
            if not population_targets.get(str(pop)) or not choices:
                reasons.append("POPULATION_UNBOUND")

    assignments: list[CandidateAssignment] = []
    unit_combo_failed = False
    unit_provenance_missing = False
    if checked_items and period_options:
        dimension_products: list[tuple[AxisBinding, ...]] = []
        region_scopes = [(None, None)]
        if _get(region_atom, "status") == "EXPLICIT":
            region_scopes = region_options
        population_scopes = [((), {})]
        if populations:
            by_surface = [[option for option in population_options if option[0] == str(pop)] for pop in populations]
            population_scopes = []
            if all(by_surface):
                for choice in product(*by_surface):
                    selected = {dindex: binding for _, dindex, binding in choice}
                    if len(selected) == len(choice):
                        population_scopes.append((choice, selected))
        for region_scope in region_scopes:
            region_index, region_binding = region_scope
            for _, population_selected in population_scopes:
                scoped = []
                for index, options in enumerate(dim_options):
                    if region_index is not None and index == region_index:
                        scoped.append([region_binding])
                    elif index in population_selected:
                        scoped.append([population_selected[index]])
                    else:
                        scoped.append(options)
                if all(scoped):
                    dimension_products.extend(tuple(product(*scoped)))
        for combo in product(checked_items, period_options, dimension_products):
            item, item_span, item_rule, item_binding = combo[0]
            period_binding = combo[1]
            dim_bindings = list(combo[2])
            unit_binding = None
            if _get(unit_atom, "status") == "EXPLICIT":
                item_index = next(
                    (index for index, candidate in enumerate(items) if candidate is item),
                    items.index(item),
                )
                unit_source, unit_conflict = _selected_series_unit_source(
                    item, item_index, dim_bindings, dimensions
                )
                claim_unit = str(_get(unit_atom, "surface", "") or "")
                unit_normalization = (
                    _unit_compatibility(claim_unit, unit_source["profile_label"])
                    if unit_source is not None and not unit_conflict
                    else None
                )
                if unit_normalization is None:
                    unit_combo_failed = True
                    continue
                unit_span = _own_claim_span(unit_atom)
                unit_evidence = (
                    _evidence(
                        unit_atom,
                        profile_path=unit_source["profile_inventory_path"],
                        profile_label=unit_source["profile_label"],
                        profile_id=unit_source["profile_id"],
                        path="unit",
                        consumed=unit_span,
                        rule=(
                            "selected_series_unit_exact"
                            if unit_normalization["rule_id"] == "unit-exact"
                            else "selected_series_unit_scale_compatible"
                        ),
                    )
                    if unit_span is not None
                    else None
                )
                if unit_evidence is None:
                    unit_provenance_missing = True
                    continue
                unit_evidence["unit_normalization"] = unit_normalization
                unit_binding = AxisBinding(
                    "UNIT",
                    unit_source["profile_id"],
                    unit_source["profile_label"],
                    "unit",
                    "UNIT_COMPATIBLE",
                    unit_evidence,
                )
            bindings = [item_binding, *([unit_binding] if unit_binding is not None else []), period_binding, *dim_bindings]
            if _overlaps(bindings):
                reasons.append("SPAN_REUSE")
                continue
            if any(not b.evidence.get("profile_inventory_path") or not b.evidence.get("claim_provenance") for b in bindings):
                reasons.append("CLAIM_PROVENANCE_MISSING")
                continue
            assignments.append(CandidateAssignment(table, tuple(sorted(bindings, key=lambda b: (b.axis_kind, b.axis_id, b.value_id or "", b.bound_atom)))))
    if not assignments and unit_provenance_missing:
        reasons.append("CLAIM_PROVENANCE_MISSING")
    elif not assignments and unit_combo_failed:
        reasons.append("UNIT_MISMATCH")
    if not assignments and not reasons:
        reasons.append("NO_COMPATIBLE_SERIES")
    return _make_projection(table, assignments, abstained, reasons, _slot_diagnostics(table, profile, assignments, abstained, reasons))


def _query_plan(assignment: CandidateAssignment) -> dict[str, Any] | None:
    table = assignment.table_key
    if ":" not in table:
        return None
    org_id, tbl_id = table.split(":", 1)
    item = next((b for b in assignment.bindings if b.axis_kind == "ITEM"), None)
    period = next((b for b in assignment.bindings if b.axis_kind == "PERIOD"), None)
    dimensions = sorted((b for b in assignment.bindings if b.axis_kind == "DIMENSION"), key=lambda b: b.evidence.get("dimension_order", b.axis_id))
    if item is None or period is None or not dimensions:
        return None
    obj_levels = {f"objL{index}": b.value_id for index, b in enumerate(dimensions, 1)}
    if any(not value for value in obj_levels.values()):
        return None
    return {
        "org_id": org_id,
        "tbl_id": tbl_id,
        "itm_id": item.axis_id,
        "prd_se": period.axis_id,
        "start_prd_de": period.value_id,
        "end_prd_de": period.value_id,
        "obj_levels": obj_levels,
    }


def validate_target_v2(projections: Sequence[CandidateProjection]) -> TargetResolution:
    ordered = tuple(sorted(projections, key=lambda p: (p.table_key, p.canonical_sha256)))
    assignments: dict[tuple[Any, ...], CandidateAssignment] = {}
    reasons: list[str] = []
    for projection in ordered:
        reasons.extend(projection.hold_reasons)
        for assignment in projection.assignments:
            assignments.setdefault(assignment.signature, assignment)
    signatures = tuple(sorted(assignments))
    base_audit = {"projection_count": len(ordered), "assignment_count": len(assignments)}
    if len(signatures) > 1:
        data = {"outcome": "HOLD", "hold_reason": "MULTIPLE_COMPATIBLE_SERIES", "query_plan": None, "chosen_table_key": None, "compatible_series": signatures, "audit": base_audit}
        return TargetResolution(**data, canonical_sha256=_sha(data))
    if len(signatures) == 1:
        assignment = assignments[signatures[0]]
        plan = _query_plan(assignment)
        if plan is not None:
            data = {"outcome": "QUERY_READY", "hold_reason": None, "query_plan": plan, "chosen_table_key": assignment.table_key, "compatible_series": signatures, "audit": base_audit}
            return TargetResolution(**data, canonical_sha256=_sha(data))
        reasons.append("QUERY_PLAN_INVALID")
    unavailable = [projection for projection in ordered if "PROFILE_UNAVAILABLE" in projection.hold_reasons]
    incomplete = [projection for projection in ordered if "PROFILE_INCOMPLETE" in projection.hold_reasons]
    available = [projection for projection in ordered if "PROFILE_UNAVAILABLE" not in projection.hold_reasons and "PROFILE_INCOMPLETE" not in projection.hold_reasons]
    if not available:
        if ordered and len(unavailable) == len(ordered):
            reason = "PROFILE_UNAVAILABLE"
        elif incomplete:
            reason = "PROFILE_INCOMPLETE"
        else:
            reason = "NO_COMPATIBLE_SERIES"
        data = {"outcome": "HOLD", "hold_reason": reason, "query_plan": None, "chosen_table_key": None, "compatible_series": signatures, "audit": base_audit}
        return TargetResolution(**data, canonical_sha256=_sha(data))
    priority = ("REGION_UNBOUND", "POPULATION_UNBOUND", "SPAN_REUSE", "UNIT_MISMATCH", "PERIOD_FREQUENCY_MISMATCH", "PERIOD_OUT_OF_RANGE", "PERIOD_INVALID", "CLAIM_PROVENANCE_MISSING", "PERIOD_UNKNOWN", "NO_COMPATIBLE_SERIES")
    reason = next((candidate for candidate in priority if candidate in reasons), "NO_COMPATIBLE_SERIES")
    data = {"outcome": "HOLD", "hold_reason": reason, "query_plan": None, "chosen_table_key": None, "compatible_series": signatures, "audit": base_audit}
    return TargetResolution(**data, canonical_sha256=_sha(data))


MONTHLY_PROFILE_RECEIPT_CONTRACT_V2H = "monthly-profile-receipt-v2f"
MONTHLY_MEMBERSHIP_RECEIPT_CONTRACT_V2H = "monthly-membership-receipt-v2g"
MONTHLY_PROFILE_STATUSES_V2H = {
    "COMPLETE", "UNAVAILABLE", "INCOMPLETE", "TABLE_KEY_MISMATCH",
    "TRANSFORM_ERROR", "TRANSFORM_INVALID", "TRANSFORM_TABLE_KEY_MISMATCH",
    "TRANSFORM_INCOMPLETE",
}


def membership_receipt_sha256_monthly_v2h(
    candidate_membership: Sequence[str],
    profile_receipts: Sequence[Mapping[str, Any]],
) -> str:
    envelope = {
        "contract_version": MONTHLY_MEMBERSHIP_RECEIPT_CONTRACT_V2H,
        "candidate_membership": sorted({str(key) for key in candidate_membership}),
        "profile_receipts": [
            dict(receipt)
            for receipt in sorted(profile_receipts, key=lambda row: str(row.get("table_key") or ""))
        ],
    }
    return _sha(envelope)


def placeholder_projection_monthly_v2h(table_key: str, reason: str) -> CandidateProjection:
    data = {
        "table_key": str(table_key),
        "assignments": [],
        "abstained": [],
        "projection_status": "ABSTAIN",
        "hold_reasons": [reason],
    }
    return CandidateProjection(
        str(table_key), (), (), "ABSTAIN", (reason,), _sha(data)
    )


def project_candidate_monthly_v2h(
    claim_core: Any,
    profile: Mapping[str, Any] | None,
    *,
    table_key: str,
    allow_unqualified_nationwide: bool = False,
) -> CandidateProjection:
    """Project one already pinned and identity-validated monthly profile."""

    if profile is None:
        return placeholder_projection_monthly_v2h(table_key, "PROFILE_UNAVAILABLE")
    projection = project_candidate_v2(
        claim_core, profile,
        allow_unqualified_nationwide=allow_unqualified_nationwide,
    )
    return projection


_MONTHLY_SURFACE_V2J = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_MONTHLY_SHA256_V2J = re.compile(r"^[0-9a-f]{64}$")


def _canonical_equal_monthly_v2j(left: Any, right: Any) -> bool:
    return _json_bytes(left) == _json_bytes(right)


def _recompute_month_anchor_v2j(raw_text: str, article_date: str) -> tuple[str, str, str] | None:
    try:
        anchor = date.fromisoformat(article_date)
    except (TypeError, ValueError):
        return None
    raw = re.sub(r"\s+", " ", raw_text).strip()
    previous = re.fullmatch(r"지난 ([1-9]|1[0-2])월", raw)
    current_year = re.fullmatch(r"올해 ([1-9]|1[0-2])월", raw)
    explicit = re.fullmatch(r"((?:19|20)\d{2})년 ([1-9]|1[0-2])월", raw)
    if previous:
        month = int(previous.group(1))
        if month == anchor.month:
            return None
        year = anchor.year if month < anchor.month else anchor.year - 1
        return (
            f"{year:04d}-{month:02d}",
            "anchor-most-recent-named-month-same-year-v2e" if month < anchor.month
            else "anchor-most-recent-named-month-previous-year-v2e",
            "SAME_YEAR" if month < anchor.month else "PREVIOUS_YEAR",
        )
    if current_year:
        month = int(current_year.group(1))
        if month > anchor.month:
            return None
        return f"{anchor.year:04d}-{month:02d}", "anchor-current-year-observed-month-v2e", "CURRENT_YEAR_OBSERVED"
    if explicit:
        year, month = int(explicit.group(1)), int(explicit.group(2))
        if (year, month) > (anchor.year, anchor.month):
            return None
        return f"{year:04d}-{month:02d}", "explicit-korean-year-month-v2e", "EXPLICIT_YEAR_MONTH"
    return None


def _claim_period_monthly_v2j(
    period_atom: Mapping[str, Any],
) -> tuple[tuple[str, str, str] | None, dict[str, Any] | None, str | None]:
    surface = str(_get(period_atom, "surface", "") or "")
    if not _MONTHLY_SURFACE_V2J.fullmatch(surface):
        return None, None, "PERIOD_INVALID"
    try:
        date.fromisoformat(f"{surface}-01")
    except ValueError:
        return None, None, "PERIOD_INVALID"
    provenance = _base_claim_prov(period_atom)
    sentence_text = provenance.get("sentence_text")
    period_evidence = provenance.get("period_evidence")
    anchor_receipt = provenance.get("anchor_receipt")
    date_receipt = provenance.get("anchor_date_provenance")
    if not all(isinstance(value, Mapping) for value in (period_evidence, anchor_receipt, date_receipt)):
        return None, None, "PERIOD_INVALID"
    text = period_evidence.get("text")
    start, end = period_evidence.get("start"), period_evidence.get("end")
    sentence_id = period_evidence.get("sentence_id")
    if (
        not isinstance(sentence_text, str)
        or not isinstance(text, str) or not text
        or type(start) is not int or type(end) is not int
        or sentence_id is None or not (0 <= start < end <= len(sentence_text))
        or sentence_text[start:end] != text
        or provenance.get("sentence_id") != sentence_id
    ):
        return None, None, "PERIOD_INVALID"
    article_date = anchor_receipt.get("article_date")
    anchor_valid = (
        anchor_receipt.get("contract_version") == "monthly-period-anchor-v2h"
        and anchor_receipt.get("lossless") is True
        and anchor_receipt.get("date_source") == "client_asserted"
        and anchor_receipt.get("raw_text") == text
        and anchor_receipt.get("structured_period") == surface
        and isinstance(article_date, str)
    )
    date_valid = (
        date_receipt.get("date_source") == "client_asserted"
        and date_receipt.get("source_path") in {"terminal_argument", "backend_request"}
        and date_receipt.get("date_field") == "date"
        and isinstance(date_receipt.get("article_text_sha256"), str)
        and bool(_MONTHLY_SHA256_V2J.fullmatch(date_receipt["article_text_sha256"]))
    )
    recalculated = _recompute_month_anchor_v2j(text, article_date) if anchor_valid else None
    if (
        not anchor_valid or not date_valid or recalculated is None
        or recalculated != (
            anchor_receipt.get("structured_period"),
            anchor_receipt.get("rule_id"),
            anchor_receipt.get("calculation_branch"),
        )
    ):
        return None, None, "PERIOD_INVALID"
    api_period = surface.replace("-", "")
    return ("M", api_period, api_period), {
        "rule_id": str(anchor_receipt["rule_id"]),
        "rule_version": 2,
        "calculation_branch": str(anchor_receipt["calculation_branch"]),
        "source_granularity": "MONTH",
        "lossless": True,
        "normalized_frequency": "M",
        "normalized_start": api_period,
        "normalized_end": api_period,
        "raw_source_path": "monthly_v2h.period_evidence",
        "raw_text": text,
        "anchor_date_provenance": dict(date_receipt),
    }, None


def _monthly_core_valid_v2j(claim_core: Any) -> bool:
    contract = getattr(claim_core, "contract_version", None)
    atoms = getattr(claim_core, "atoms", None)
    proposal = getattr(claim_core, "proposal_view", None)
    provenance = getattr(claim_core, "provenance", None)
    digest = getattr(claim_core, "canonical_sha256", None)
    if (
        contract != "monthly-claim-core-v2h" or not isinstance(atoms, Mapping)
        or not isinstance(proposal, Mapping) or not isinstance(provenance, Mapping)
        or not isinstance(digest, str)
    ):
        return False
    try:
        serialized_atoms = {
            name: asdict(atom) if hasattr(atom, "__dataclass_fields__") else dict(atom)
            for name, atom in atoms.items()
        }
    except (TypeError, ValueError):
        return False
    envelope = {
        "atoms": serialized_atoms,
        "proposal_view": proposal,
        "provenance": provenance,
        "contract_version": contract,
    }
    if hashlib.sha256(_json_bytes(envelope)).hexdigest() != digest:
        return False
    period_atom = _get_atom(claim_core, "period")
    indicator_atom = _get_atom(claim_core, "indicator")
    period_prov = _base_claim_prov(period_atom)
    indicator_prov = _base_claim_prov(indicator_atom)
    return (
        _canonical_equal_monthly_v2j(provenance.get("period_evidence"), period_prov.get("period_evidence"))
        and _canonical_equal_monthly_v2j(provenance.get("anchor_receipt"), period_prov.get("anchor_receipt"))
        and _canonical_equal_monthly_v2j(provenance.get("indicator_receipt"), indicator_prov.get("indicator_receipt"))
        and _canonical_equal_monthly_v2j(provenance.get("article_date_provenance"), period_prov.get("anchor_date_provenance"))
    )


def _whitespace_norm_v2j(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _monthly_derived_count_dimension_matches_v2j(
    indicator_atom: Mapping[str, Any],
    dimension: Mapping[str, Any],
    *,
    dimension_index: int,
) -> tuple[AxisBinding, ...]:
    provenance = _base_claim_prov(indicator_atom)
    receipt = provenance.get("indicator_receipt")
    sentence_text = provenance.get("sentence_text")
    if not isinstance(receipt, Mapping) or not isinstance(sentence_text, str):
        return ()
    subject = receipt.get("subject")
    start, end = receipt.get("subject_start"), receipt.get("subject_end")
    normalized_indicator = receipt.get("normalized_indicator")
    if (
        receipt.get("contract_version") != "monthly-indicator-receipt-v2h"
        or receipt.get("rule_id") != "subject-particle-count-noun-v2b"
        or receipt.get("sentence_id") is None
        or provenance.get("sentence_id") != receipt.get("sentence_id")
        or not isinstance(subject, str) or not subject
        or type(start) is not int or type(end) is not int
        or not (0 <= start < end <= len(sentence_text))
        or sentence_text[start:end] != subject
        or provenance.get("start") != start or provenance.get("end") != end
        or provenance.get("text") != subject
        or receipt.get("removed_particle") not in {"은", "는"}
        or receipt.get("added_count_suffix") != "수"
        or _whitespace_norm_v2j(normalized_indicator) != _whitespace_norm_v2j(subject + "수")
    ):
        return ()
    subject_span = {
        "article_idx": provenance.get("article_idx"),
        "article_id": provenance.get("article_id"),
        "sentence_id": receipt.get("sentence_id"),
        "span_path": "monthly_v2j.indicator_subject",
        "start": start,
        "end": end,
        "text": subject,
    }
    matches: list[AxisBinding] = []
    for value_index, value in enumerate(dimension.get("values") or []):
        if not isinstance(value, Mapping):
            continue
        label = str(value.get("value_name") or "")
        parenthetical = re.search(r"\(([^()]*)\)\s*$", label)
        inference = value.get("unit_inference")
        unit_nm = str(value.get("unit_nm") or "").strip()
        unit_id = str(value.get("unit_id") or "").strip()
        if (
            parenthetical is None or not isinstance(inference, Mapping)
            or inference.get("rule_id") != "terminal-parenthetical-unit"
            or inference.get("source_label") != label
            or parenthetical.group(1).strip() != unit_nm
            or not unit_nm or unit_id != f"LABEL:{unit_nm}"
        ):
            continue
        base = label[:parenthetical.start()]
        if _whitespace_norm_v2j(base) != _whitespace_norm_v2j(normalized_indicator):
            continue
        profile_path = f"dimensions[{dimension_index}].values[{value_index}]"
        profile_unit = {
            "unit_inference": dict(inference),
            "unit_nm": unit_nm,
            "unit_id": unit_id,
        }
        evidence = {
            "claim_provenance": dict(subject_span),
            "consumed_span": dict(subject_span),
            "subject_consumed_span": dict(subject_span),
            "derived_suffix_receipt": {
                "rule_id": receipt.get("rule_id"),
                "added_count_suffix": "수",
                "source_span": None,
            },
            "profile_inventory_path": profile_path,
            "profile_id": str(value.get("value_id") or ""),
            "profile_label": label,
            "profile_unit_provenance": profile_unit,
            "match_rule": "monthly-derived-count-dimension-base-v2j",
            "rule_id": "monthly-derived-count-dimension-base-v2j",
            "span_path": subject_span["span_path"],
            "article_idx": subject_span["article_idx"],
            "sentence_id": subject_span["sentence_id"],
            "start": start,
            "end": end,
            "text": subject,
            "dimension_order": dimension_index + 1,
            "axis_evidence": {
                "profile_inventory_path": f"dimensions[{dimension_index}]",
                "profile_label": str(dimension.get("obj_nm") or ""),
                "profile_id": str(dimension.get("obj_id") or ""),
            },
            "value_evidence": {
                "profile_inventory_path": profile_path,
                "profile_label": label,
                "profile_id": str(value.get("value_id") or ""),
            },
        }
        matches.append(AxisBinding(
            "DIMENSION", str(dimension.get("obj_id") or ""),
            str(value.get("value_id") or ""), "indicator",
            "DERIVED_COUNT_EXACT_BASE", evidence,
        ))
    return tuple(matches)


def project_candidate_monthly_v2j(
    claim_core: Any,
    profile: Mapping[str, Any] | None,
    *,
    table_key: str,
    allow_unqualified_nationwide: bool = False,
) -> CandidateProjection:
    """Project a receipt-validated monthly core without generic period fallback."""

    if not _monthly_core_valid_v2j(claim_core):
        return placeholder_projection_monthly_v2h(table_key, "PERIOD_INVALID")
    if profile is None:
        return placeholder_projection_monthly_v2h(table_key, "PROFILE_UNAVAILABLE")
    table_info = _profile_table(profile)
    table = table_info[0] if table_info else ""
    if table != str(table_key):
        return placeholder_projection_monthly_v2h(table_key, "PROFILE_INCOMPLETE")
    if _incomplete(profile):
        return placeholder_projection_monthly_v2h(table_key, "PROFILE_INCOMPLETE")

    items = list(profile["items"])
    dimensions = list(profile["dimensions"])
    indicator_atom = _get_atom(claim_core, "indicator")
    indicator = str(_get(indicator_atom, "surface", "") or "")
    unit_atom = _get_atom(claim_core, "unit")
    period_atom = _get_atom(claim_core, "period")
    region_atom = _get_atom(claim_core, "region")
    population_atom = _get_atom(claim_core, "population")
    abstained: list[tuple[str, str]] = []
    reasons: list[str] = []

    item_options: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = []
    for idx, item in enumerate(items):
        for start, end, text in _submatches(indicator, str(item.get("itm_nm"))):
            prov = _subspan_provenance(indicator_atom, start, end, text, f"indicator.item[{idx}]")
            if prov is not None:
                item_options.append((item, prov, "indicator_item_subspan"))
        for proposal in propose_semantic_alias_matches(
            indicator, item.get("itm_nm"),
            allow_parenthetical_base=allow_unqualified_nationwide,
        ):
            prov = _subspan_provenance(
                indicator_atom, proposal.start, proposal.end, proposal.text,
                f"indicator.item[{idx}].semantic_alias",
            )
            if prov is not None:
                item_options.append((item, prov, proposal.rule_id))
    if not item_options:
        if len(items) == 1:
            if _context_provenance(indicator_atom, "indicator.generic_item") is not None:
                item_options.append((items[0], None, "SINGLETON_INVENTORY"))
            else:
                reasons.append("CLAIM_PROVENANCE_MISSING")
        else:
            abstained.append(("ITEM", "NO_SUBSPAN_MATCH"))
    item_options = list({
        (str(item.get("itm_id")), prov.get("start") if prov else None, prov.get("end") if prov else None):
        (item, prov, rule) for item, prov, rule in item_options
    }.values())
    checked_items: list[tuple[dict[str, Any], dict[str, Any] | None, str, AxisBinding]] = []
    for item, prov, rule in item_options:
        index = next((i for i, candidate in enumerate(items) if candidate is item), items.index(item))
        evidence = _evidence(
            indicator_atom, profile_path=f"items[{index}].itm_nm",
            profile_label=item.get("itm_nm"), profile_id=item.get("itm_id"),
            path="indicator", consumed=prov, rule=rule,
        )
        if evidence is None:
            reasons.append("CLAIM_PROVENANCE_MISSING")
            continue
        checked_items.append((item, prov, rule, AxisBinding(
            "ITEM", str(item["itm_id"]), None, "indicator",
            "EXACT_LABEL" if prov else "SINGLETON_INVENTORY",
            {**evidence, "consumed_span": prov},
        )))
    if not checked_items and not item_options:
        reasons.append("NO_COMPATIBLE_SERIES")

    period_options: list[AxisBinding] = []
    if _get(period_atom, "status") in {"EXPLICIT", "INFERRED"}:
        parsed, normalization, period_reason = _claim_period_monthly_v2j(period_atom)
        matched, range_reason = ([], period_reason) if period_reason else _profile_periods(profile, parsed)
        if range_reason:
            reasons.append(range_reason)
        for candidate in matched:
            span = _own_claim_span(period_atom)
            evidence = _evidence(
                period_atom, profile_path=f"periods[{candidate['index']}].PRD_SE",
                profile_label=candidate["prd_se"], profile_id=candidate["prd_se"],
                path="period", consumed=span, rule="period_frequency_and_range",
            ) if span is not None else None
            if evidence is None:
                reasons.append("CLAIM_PROVENANCE_MISSING")
                continue
            evidence["period_normalization"] = dict(normalization or {})
            period_options.append(AxisBinding(
                "PERIOD", candidate["prd_se"], candidate["api"], "period",
                "PERIOD_RANGE_COMPATIBLE", evidence,
            ))
    else:
        reasons.append("PERIOD_UNKNOWN")

    populations = tuple(_get(population_atom, "surface", ()) or ())
    dim_options: list[list[AxisBinding]] = []
    region_options: list[tuple[int, AxisBinding]] = []
    population_options: list[tuple[str, int, AxisBinding]] = []
    population_targets: dict[str, list[tuple[int, int]]] = {str(pop): [] for pop in populations}
    derived_match_axes = 0
    for dindex, dimension in enumerate(dimensions):
        values = list(dimension["values"])
        options: list[AxisBinding] = list(_monthly_derived_count_dimension_matches_v2j(
            indicator_atom, dimension, dimension_index=dindex,
        ))
        if options:
            derived_match_axes += 1
        axis_matches = _submatches(indicator, str(dimension.get("obj_nm")))
        for vindex, value in enumerate(values):
            value_matches = _submatches(indicator, str(value.get("value_name")))
            semantic_matches = propose_semantic_alias_matches(
                indicator, value.get("value_name"),
                allow_parenthetical_base=allow_unqualified_nationwide,
            )
            if _get(region_atom, "status") == "EXPLICIT" and _norm(_get(region_atom, "surface", "")) == _norm(value.get("value_name")):
                evidence = _evidence(
                    region_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]",
                    profile_label=value.get("value_name"), profile_id=value.get("value_id"),
                    path="region_evidence", consumed=dict(_base_claim_prov(region_atom)),
                    rule="region_evidence_dimension_value",
                )
                if evidence is not None:
                    region_options.append((dindex, AxisBinding(
                        "DIMENSION", str(dimension["obj_id"]), str(value["value_id"]),
                        "region", "EXACT_LABEL", _annotate_dimension_evidence(
                            evidence, dindex=dindex, dimension=dimension,
                            vindex=vindex, value=value,
                        ),
                    )))
                else:
                    reasons.append("CLAIM_PROVENANCE_MISSING")
            population_matches: list[dict[str, Any] | None] = []
            if populations:
                population_evidence = _base_claim_prov(population_atom).get("evidence", ())
                for pop in populations:
                    if _norm(pop) == _norm(value.get("value_name")):
                        population_targets.setdefault(str(pop), []).append((dindex, vindex))
                        matches = [
                            dict(span) for span in population_evidence
                            if isinstance(span, Mapping) and _norm(span.get("text")) == _norm(pop)
                        ]
                        population_matches.extend(matches or [None])
            for population_span in population_matches:
                if population_span is None:
                    reasons.append("CLAIM_PROVENANCE_MISSING")
                    continue
                evidence = _evidence(
                    population_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]",
                    profile_label=value.get("value_name"), profile_id=value.get("value_id"),
                    path="population", consumed=population_span,
                    rule="population_dimension_value",
                )
                if evidence:
                    pop = str(next((p for p in populations if _norm(p) == _norm(value.get("value_name"))), ""))
                    population_options.append((pop, dindex, AxisBinding(
                        "DIMENSION", str(dimension["obj_id"]), str(value["value_id"]),
                        "population", "EXACT_LABEL", _annotate_dimension_evidence(
                            evidence, dindex=dindex, dimension=dimension,
                            vindex=vindex, value=value,
                        ),
                    )))
            for start, end, text in value_matches:
                span = _subspan_provenance(
                    indicator_atom, start, end, text,
                    f"indicator.dimensions[{dindex}].values[{vindex}]",
                )
                if span is not None:
                    evidence = _evidence(
                        indicator_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]",
                        profile_label=value.get("value_name"), profile_id=value.get("value_id"),
                        path="indicator", consumed=span, rule="indicator_dimension_value",
                    )
                    if evidence:
                        options.append(AxisBinding(
                            "DIMENSION", str(dimension["obj_id"]), str(value["value_id"]),
                            "indicator", "EXACT_LABEL", _annotate_dimension_evidence(
                                evidence, dindex=dindex, dimension=dimension,
                                vindex=vindex, value=value,
                            ),
                        ))
            for proposal in semantic_matches:
                span = _subspan_provenance(
                    indicator_atom, proposal.start, proposal.end, proposal.text,
                    f"indicator.dimensions[{dindex}].values[{vindex}].semantic_alias",
                )
                if span is not None:
                    evidence = _evidence(
                        indicator_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]",
                        profile_label=value.get("value_name"), profile_id=value.get("value_id"),
                        path="indicator", consumed=span, rule=proposal.rule_id,
                    )
                    if evidence:
                        evidence["match_rule_version"] = proposal.rule_version
                        options.append(AxisBinding(
                            "DIMENSION", str(dimension["obj_id"]), str(value["value_id"]),
                            "indicator", "SEMANTIC_ALIAS", _annotate_dimension_evidence(
                                evidence, dindex=dindex, dimension=dimension,
                                vindex=vindex, value=value,
                            ),
                        ))
            if not value_matches and not semantic_matches and axis_matches and len(values) == 1:
                for start, end, text in axis_matches:
                    span = _subspan_provenance(
                        indicator_atom, start, end, text, f"indicator.dimensions[{dindex}]",
                    )
                    if span is not None:
                        evidence = _evidence(
                            indicator_atom, profile_path=f"dimensions[{dindex}].values[{vindex}]",
                            profile_label=value.get("value_name"), profile_id=value.get("value_id"),
                            path="indicator", consumed=span, rule="axis_label_singleton_value",
                        )
                        if evidence:
                            options.append(AxisBinding(
                                "DIMENSION", str(dimension["obj_id"]), str(value["value_id"]),
                                "indicator", "NORMALIZED_CONTAINMENT", _annotate_dimension_evidence(
                                    evidence, dindex=dindex, dimension=dimension,
                                    vindex=vindex, value=value,
                                ),
                            ))
        options = _prune_strictly_subsumed_axis_matches(options)
        if not options and allow_unqualified_nationwide and _get(region_atom, "status") != "EXPLICIT":
            nationwide = _geographic_nationwide_default(
                indicator_atom, dindex=dindex, dimension=dimension, values=values,
            )
            if nationwide is not None:
                options.append(nationwide)
        if not options:
            abstained.append(("DIMENSION", f"UNBOUND:{dimension.get('obj_id')}"))
        dim_options.append(list({
            (binding.axis_id, binding.value_id, binding.evidence.get("start"), binding.evidence.get("end")): binding
            for binding in options
        }.values()))
    if _get(region_atom, "status") == "EXPLICIT" and not region_options:
        reasons.append("REGION_UNBOUND")
    for pop in populations:
        if not population_targets.get(str(pop)) or not [row for row in population_options if row[0] == str(pop)]:
            reasons.append("POPULATION_UNBOUND")

    cross_axis_derived_ambiguous = derived_match_axes > 1
    if cross_axis_derived_ambiguous:
        reasons.append("MULTIPLE_COMPATIBLE_SERIES")

    assignments: list[CandidateAssignment] = []
    unit_combo_failed = False
    unit_provenance_missing = False
    if checked_items and period_options and not cross_axis_derived_ambiguous:
        dimension_products: list[tuple[AxisBinding, ...]] = []
        region_scopes = region_options if _get(region_atom, "status") == "EXPLICIT" else [(None, None)]
        population_scopes: list[tuple[Any, dict[int, AxisBinding]]] = [((), {})]
        if populations:
            by_surface = [[row for row in population_options if row[0] == str(pop)] for pop in populations]
            population_scopes = []
            if all(by_surface):
                for choice in product(*by_surface):
                    selected = {dindex: binding for _, dindex, binding in choice}
                    if len(selected) == len(choice):
                        population_scopes.append((choice, selected))
        for region_index, region_binding in region_scopes:
            for _, selected in population_scopes:
                scoped: list[list[AxisBinding]] = []
                for index, options in enumerate(dim_options):
                    if region_index is not None and index == region_index:
                        scoped.append([region_binding])
                    elif index in selected:
                        scoped.append([selected[index]])
                    else:
                        scoped.append(options)
                if all(scoped):
                    dimension_products.extend(tuple(product(*scoped)))
        for item_tuple, period_binding, dim_tuple in product(checked_items, period_options, dimension_products):
            item, _, _, item_binding = item_tuple
            dim_bindings = list(dim_tuple)
            unit_binding = None
            if _get(unit_atom, "status") == "EXPLICIT":
                item_index = next((i for i, candidate in enumerate(items) if candidate is item), items.index(item))
                unit_source, conflict = _selected_series_unit_source(item, item_index, dim_bindings, dimensions)
                claim_unit = str(_get(unit_atom, "surface", "") or "")
                normalization = (
                    _unit_compatibility(claim_unit, unit_source["profile_label"])
                    if unit_source is not None and not conflict else None
                )
                if normalization is None:
                    unit_combo_failed = True
                    continue
                unit_span = _own_claim_span(unit_atom)
                evidence = _evidence(
                    unit_atom, profile_path=unit_source["profile_inventory_path"],
                    profile_label=unit_source["profile_label"], profile_id=unit_source["profile_id"],
                    path="unit", consumed=unit_span,
                    rule="selected_series_unit_exact" if normalization["rule_id"] == "unit-exact"
                    else "selected_series_unit_scale_compatible",
                ) if unit_span is not None else None
                if evidence is None:
                    unit_provenance_missing = True
                    continue
                evidence["unit_normalization"] = normalization
                unit_binding = AxisBinding(
                    "UNIT", unit_source["profile_id"], unit_source["profile_label"],
                    "unit", "UNIT_COMPATIBLE", evidence,
                )
            bindings = [item_binding, *([unit_binding] if unit_binding else []), period_binding, *dim_bindings]
            if _overlaps(bindings):
                reasons.append("SPAN_REUSE")
                continue
            if any(not binding.evidence.get("profile_inventory_path") or not binding.evidence.get("claim_provenance") for binding in bindings):
                reasons.append("CLAIM_PROVENANCE_MISSING")
                continue
            assignments.append(CandidateAssignment(
                table, tuple(sorted(
                    bindings, key=lambda binding: (
                        binding.axis_kind, binding.axis_id,
                        binding.value_id or "", binding.bound_atom,
                    ),
                )),
            ))
    if not assignments and unit_provenance_missing:
        reasons.append("CLAIM_PROVENANCE_MISSING")
    elif not assignments and unit_combo_failed:
        reasons.append("UNIT_MISMATCH")
    if not assignments and not reasons:
        reasons.append("NO_COMPATIBLE_SERIES")
    return _make_projection(table, assignments, abstained, reasons, _slot_diagnostics(table, profile, assignments, abstained, reasons))


def _monthly_identity_audit_v2h(
    projections: Sequence[CandidateProjection],
    candidate_membership: Sequence[str],
    membership_sha256: str,
) -> tuple[dict[str, Any], bool]:
    membership = sorted({str(key) for key in candidate_membership})
    projection_keys = [str(projection.table_key) for projection in projections]
    duplicate_projection_keys = sorted({
        key for key in projection_keys if projection_keys.count(key) > 1
    })
    missing = sorted(set(membership) - set(projection_keys))
    unexpected = sorted(set(projection_keys) - set(membership))
    assignment_mismatches = sorted(
        (
            {
                "projection_table_key": str(projection.table_key),
                "assignment_table_key": str(assignment.table_key),
            }
            for projection in projections
            for assignment in projection.assignments
            if str(assignment.table_key) != str(projection.table_key)
        ),
        key=lambda row: (row["projection_table_key"], row["assignment_table_key"]),
    )
    audit = {
        "candidate_membership": membership,
        "projection_table_keys": sorted(projection_keys),
        "duplicate_projection_table_keys": duplicate_projection_keys,
        "missing_projection_table_keys": missing,
        "unexpected_projection_table_keys": unexpected,
        "assignment_parent_mismatches": assignment_mismatches,
        "membership_receipt_sha256": membership_sha256,
    }
    mismatch = (
        len(projections) != len(set(membership))
        or len(candidate_membership) != len(set(candidate_membership))
        or bool(duplicate_projection_keys or missing or unexpected or assignment_mismatches)
    )
    return audit, mismatch


def validate_target_monthly_v2h(
    projections: Sequence[CandidateProjection],
    *,
    candidate_membership: Sequence[str],
    profile_receipts: Sequence[Mapping[str, Any]],
    missing_table_keys: Sequence[str] = (),
    incomplete_table_keys: Sequence[str] = (),
) -> TargetResolution:
    """Validate candidate identity before coverage and global uniqueness."""

    membership_sha = membership_receipt_sha256_monthly_v2h(
        candidate_membership, profile_receipts
    )
    identity_audit, identity_mismatch = _monthly_identity_audit_v2h(
        projections, candidate_membership, membership_sha
    )
    if identity_mismatch:
        data = {
            "outcome": "HOLD",
            "hold_reason": "CANDIDATE_IDENTITY_MISMATCH",
            "query_plan": None,
            "chosen_table_key": None,
            "compatible_series": [],
            "audit": identity_audit,
        }
        return TargetResolution(**data, canonical_sha256=_sha(data))

    receipts = [dict(receipt) for receipt in sorted(
        profile_receipts, key=lambda row: str(row.get("table_key") or "")
    )]
    missing = sorted({str(key) for key in missing_table_keys} | {
        str(row.get("table_key") or "") for row in receipts if row.get("status") == "UNAVAILABLE"
    })
    mismatched = sorted({
        str(row.get("table_key") or "") for row in receipts
        if row.get("status") in {"TABLE_KEY_MISMATCH", "TRANSFORM_TABLE_KEY_MISMATCH"}
    })
    incomplete = sorted({str(key) for key in incomplete_table_keys} | {
        str(row.get("table_key") or "") for row in receipts
        if row.get("status") not in {"COMPLETE", "UNAVAILABLE", "TABLE_KEY_MISMATCH", "TRANSFORM_TABLE_KEY_MISMATCH"}
    })
    invalid_receipts = [
        row for row in receipts
        if row.get("status") not in MONTHLY_PROFILE_STATUSES_V2H
        or row.get("contract_version") != MONTHLY_PROFILE_RECEIPT_CONTRACT_V2H
    ]
    if missing or incomplete or mismatched or invalid_receipts:
        audit = {
            "projection_count": len(projections),
            "assignment_count": 0,
            "candidate_membership": sorted({str(key) for key in candidate_membership}),
            "membership_receipt_sha256": membership_sha,
            "missing_table_keys": missing,
            "incomplete_table_keys": sorted(set(incomplete) | {
                str(row.get("table_key") or "") for row in invalid_receipts
            }),
            "mismatched_table_keys": mismatched,
            "profile_receipts": receipts,
        }
        data = {
            "outcome": "HOLD",
            "hold_reason": "PROFILE_COVERAGE_INCOMPLETE",
            "query_plan": None,
            "chosen_table_key": None,
            "compatible_series": [],
            "audit": audit,
        }
        return TargetResolution(**data, canonical_sha256=_sha(data))

    base = validate_target_v2(projections)
    audit = {
        **base.audit,
        "candidate_membership": sorted({str(key) for key in candidate_membership}),
        "membership_receipt_sha256": membership_sha,
    }
    data = {
        "outcome": base.outcome,
        "hold_reason": base.hold_reason,
        "query_plan": base.query_plan,
        "chosen_table_key": base.chosen_table_key,
        "compatible_series": base.compatible_series,
        "audit": audit,
    }
    return TargetResolution(**data, canonical_sha256=_sha(data))


# Migration aliases.
project_candidate = project_candidate_v2
validate_target = validate_target_v2
