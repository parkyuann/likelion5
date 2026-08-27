"""Deterministic R4-C1 v2 candidate projection and target validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from itertools import product
from typing import Any, Mapping, Sequence

from src.news_verification.runtime.r4c1_binding_proposer_v1 import propose_semantic_alias_matches


CONTRACT_VERSION = "r4c1-projection-v2"
PROJECTION_CONTRACT_VERSION = CONTRACT_VERSION
QUERY_FIELDS = ("org_id", "tbl_id", "itm_id", "prd_se", "start_prd_de", "end_prd_de", "obj_levels")


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


def _make_projection(table: str, assignments: Sequence[CandidateAssignment], abstained: Sequence[tuple[str, str]], reasons: Sequence[str]) -> CandidateProjection:
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
    }
    return CandidateProjection(table, ordered_assignments, tuple(sorted(set(abstained))), data["projection_status"], tuple(sorted(set(reasons))), _sha(data))


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
    evidence = _evidence(
        indicator_atom,
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
        return _make_projection("", (), (), ("PROFILE_UNAVAILABLE",))
    table_info = _profile_table(profile)
    table = table_info[0] if table_info else ""
    if _incomplete(profile):
        return _make_projection(table, (), (), ("PROFILE_INCOMPLETE",))

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
    return _make_projection(table, assignments, abstained, reasons)


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


# Migration aliases.
project_candidate = project_candidate_v2
validate_target = validate_target_v2




