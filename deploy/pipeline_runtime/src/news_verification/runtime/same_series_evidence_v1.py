"""Read-only same-series evidence synthesis for the evidence-first shadow path.

This module deliberately sits after candidate-specific resolution.  It may
requery only the already selected query plan with different period bounds; it
never searches, reranks, projects, selects, or writes to a KOSIS data layer.
Every range operation has an explicit raw-cell cardinality and closes on any
missing, duplicate, unexpected, or identity-changing cell.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.news_verification.runtime.canonical_quantity import (
    QuantityNormalizationError,
    normalize_quantity,
)


CONTRACT_VERSION = "same-series-evidence-v1"
CURRENT_RELEASE = "CURRENT_RELEASE"
FEATURE_GATE_ENV = "EVIDENCE_FIRST_STATISTICS_SHADOW_ENABLED"

_ROLE_SUFFIXES = (
    "증가율", "감소율", "상승률", "하락률", "증감률", "변화율", "변동률",
    "증가량", "감소량", "상승량", "하락량", "증감량", "변화량", "변동량",
    "증가폭", "감소폭", "상승폭", "하락폭", "증감폭", "변화폭", "변동폭",
    "증가 폭", "감소 폭", "상승 폭", "하락 폭", "증감 폭", "변화 폭", "변동 폭",
    "건수", "규모", "수준", "수",
)
_FREQUENCY_PREFIX_RE = re.compile(r"^(?:월별)\s+")
_GENERIC_MEASURE_INDICATOR_RE = re.compile(r"^(?:증가|감소|상승|하락|증감|변화|변동)\s*(?:율|폭|량)$")
_GENERIC_TOPIC_RE = re.compile(r"(?P<body>(?:증가|감소|상승|하락|증감|변화|변동)\s*(?:율|폭|량))\s*(?:은|는|이|가)(?=\s|,|$)")
_RANKING_PREFIX = re.compile(
    r"^(?:(?:기존에|과거|이전|종전)\s*)?(?:(?:가장|최고|최대|최저|최소)\s*)?"
    r"(?:(?:높았던|많았던|컸던|낮았던|적었던)\s*)?"
)
_TOPIC_RE = re.compile(r"(?P<body>[^,;:()]+?)\s*(?:은|는|이|가)(?=\s|,|$)")
_MONTH_RE = re.compile(r"(?<!\d)(?P<year>\d{4})년\s*(?P<month>0?[1-9]|1[0-2])월")
_SHORT_YEAR_MONTH_RE = re.compile(r"(?<!\d)(?P<year>\d{2})년\s*(?P<month>0?[1-9]|1[0-2])월")
_NAMED_MONTH_RE = re.compile(r"(?P<qualifier>올해|지난)\s*(?P<month>0?[1-9]|1[0-2])월")
_MONTH_ANCHOR_RE = re.compile(
    r"(?P<anchor>\d{4})년\s*(?P<month>0?[1-9]|1[0-2])월"
    r"[^.]{0,100}?이후\s*(?P<years>\d+)년\s*만에\s*가장\s*(?:많|높|큰)"
)
_STREAK_RE = re.compile(
    r"(?P<start>\d{4})년\s*(?P<start_month>0?[1-9]|1[0-2])월부터\s*"
    r"(?P<end>올해|지난해|작년|\d{4}년)\s*(?P<end_month>0?[1-9]|1[0-2])월까지\s*"
    r"(?P<count>\d+)개월\s*연속"
)
_SINCE_RATE_RE = re.compile(r"(?P<year>\d{4})년\s*통계\s*집계\s*이래")
_DIFFERENCE_RE = re.compile(
    r"(?P<anchor>\d{4})년\s*(?P<month>0?[1-9]|1[0-2])월"
    r"[^.]{0,100}?이후\s*(?P<years>\d+)년\s*만에\s*가장\s*컸"
)


class SameSeriesEvidenceError(ValueError):
    """A stable, user-safe shadow evidence failure."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class RangeSpec:
    operator: str
    start_period: str
    end_period: str
    expected_periods: tuple[str, ...]
    comparison_periods: tuple[str, ...]
    anchor_period: str | None = None
    anchor_inclusion: str = "NONE"
    claim_sentence_id: Any = None
    source_provenance: Mapping[str, Any] | None = None
    claim_count: int | None = None


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _memoized_cell_fetcher_v2l(
    fetcher: Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
) -> tuple[
    Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
    dict[str, Any],
]:
    """Memoize full query plans for one evidence synthesis invocation."""

    cache: dict[str, Any] = {}
    stats: dict[str, Any] = {
        "contract_version": "same-series-cell-fetch-cache-v2l",
        "scope": "ONE_SYNTHESIS_INVOCATION",
        "key_contract": "canonical-full-query-plan-sha256-v1",
        "misses": 0,
        "hits": 0,
        "entries": 0,
        "upstream_calls": 0,
        "entry_receipts": [],
    }

    def memoized(query: dict[str, Any]) -> list[dict[str, Any]] | dict[str, Any]:
        query_snapshot = copy.deepcopy(dict(query))
        query_sha = sha256_json(query_snapshot)
        if query_sha in cache:
            stats["hits"] += 1
            return copy.deepcopy(cache[query_sha])
        stats["upstream_calls"] += 1
        response = fetcher(query_snapshot)
        cached = copy.deepcopy(response)
        cache[query_sha] = cached
        stats["misses"] += 1
        stats["entries"] += 1
        stats["entry_receipts"].append({
            "query_sha256": query_sha,
            "response_sha256": sha256_json(response),
        })
        stats["entry_receipts"].sort(key=lambda row: row["query_sha256"])
        return copy.deepcopy(cached)

    return memoized, stats


def _period_key(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})[.\-/]?(\d{1,2})", text)
    if not match or not 1 <= int(match.group(2)) <= 12:
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}"


def period_api_value(value: Any) -> str:
    key = _period_key(value)
    return key.replace("-", "") if key else ""


def month_periods(start: Any, end: Any) -> tuple[str, ...]:
    """Return every inclusive monthly bucket, or an empty tuple if invalid."""
    first, last = _period_key(start), _period_key(end)
    if not first or not last:
        return ()
    fy, fm = int(first[:4]), int(first[5:])
    ly, lm = int(last[:4]), int(last[5:])
    first_serial, last_serial = fy * 12 + fm - 1, ly * 12 + lm - 1
    if first_serial > last_serial:
        return ()
    return tuple(
        f"{serial // 12:04d}-{serial % 12 + 1:02d}"
        for serial in range(first_serial, last_serial + 1)
    )


def same_month_periods(start: Any, end: Any) -> tuple[str, ...]:
    """Return the inclusive same-month series (one cell per year)."""
    first, last = _period_key(start), _period_key(end)
    if not first or not last or first[5:] != last[5:] or int(first[:4]) > int(last[:4]):
        return ()
    month = first[5:]
    return tuple(f"{year:04d}-{month}" for year in range(int(first[:4]), int(last[:4]) + 1))


def shift_month(value: Any, amount: int) -> str:
    key = _period_key(value)
    periods = month_periods(key, key)
    if not periods:
        return ""
    serial = int(key[:4]) * 12 + int(key[5:]) - 1 + amount
    return f"{serial // 12:04d}-{serial % 12 + 1:02d}"


def period_only_plan(plan: Mapping[str, Any], start: Any, end: Any) -> dict[str, Any]:
    """Change only the two KOSIS period bounds in a query plan."""
    if not isinstance(plan, Mapping):
        raise SameSeriesEvidenceError("QUERY_PLAN_INVALID")
    start_api, end_api = period_api_value(start), period_api_value(end)
    if not start_api or not end_api:
        raise SameSeriesEvidenceError("RANGE_PERIOD_INVALID")
    result = dict(plan)
    result["start_prd_de"] = start_api
    result["end_prd_de"] = end_api
    return result


def plan_identity_without_period(plan: Mapping[str, Any]) -> str:
    """Canonical identity used by period-only requery validation."""
    if not isinstance(plan, Mapping):
        raise SameSeriesEvidenceError("QUERY_PLAN_INVALID")
    identity = dict(plan)
    identity.pop("start_prd_de", None)
    identity.pop("end_prd_de", None)
    return sha256_json(identity)


def validate_period_only_identity(
    current_plan: Mapping[str, Any], requery_plan: Mapping[str, Any],
    *, current_release_id: Any = "", requery_release_id: Any = "",
    current_profile_sha256: Any = "", requery_profile_sha256: Any = "",
    selected_assignment_provenance: Any = None,
    requery_assignment_provenance: Any = None,
) -> None:
    """Fail closed unless every non-period identity is byte-equivalent."""
    if plan_identity_without_period(current_plan) != plan_identity_without_period(requery_plan):
        raise SameSeriesEvidenceError("SERIES_IDENTITY_CHANGED")
    if current_release_id != requery_release_id:
        raise SameSeriesEvidenceError("SERIES_IDENTITY_CHANGED")
    if current_profile_sha256 != requery_profile_sha256:
        raise SameSeriesEvidenceError("SERIES_IDENTITY_CHANGED")
    if canonical_json(selected_assignment_provenance) != canonical_json(requery_assignment_provenance):
        raise SameSeriesEvidenceError("SERIES_IDENTITY_CHANGED")


def _cell_period(cell: Mapping[str, Any]) -> str:
    return _period_key(cell.get("PRD_DE"))


# KOSIS Param API의 공식 셀 row 계약은 대문자 키를 사용한다. 확인되지 않은
# 별칭으로 누락된 좌표를 보완하지 않는다.
_KOSIS_CELL_RESPONSE_ALIASES: Mapping[str, tuple[str, ...]] = {
    "ORG_ID": ("ORG_ID",),
    "TBL_ID": ("TBL_ID",),
    "ITM_ID": ("ITM_ID",),
    "PRD_SE": ("PRD_SE",),
    "PRD_DE": ("PRD_DE",),
}


def _kosis_cell_response_value(cell: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    for key in _KOSIS_CELL_RESPONSE_ALIASES.get(field, (field,)):
        if key in cell:
            return True, cell[key]
    return False, None


def _cell_identity_errors(plan: Mapping[str, Any], cell: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field, cell_key in (("org_id", "ORG_ID"), ("tbl_id", "TBL_ID"), ("itm_id", "ITM_ID")):
        present, actual = _kosis_cell_response_value(cell, cell_key)
        if not present or actual in (None, "") or not str(actual).strip():
            errors.append(cell_key)
        elif str(actual) != str(plan.get(field) or ""):
            errors.append(cell_key)
    frequency_present, actual_frequency = _kosis_cell_response_value(cell, "PRD_SE")
    if not frequency_present or actual_frequency in (None, "") or not str(actual_frequency).strip():
        errors.append("PRD_SE")
    elif str(actual_frequency) != str(plan.get("prd_se") or ""):
        errors.append("prd_se")
    for index, expected in enumerate((plan.get("obj_levels") or {}).values(), 1):
        field = f"C{index}"
        actual = cell.get(field)
        if field not in cell or actual in (None, "") or not str(actual).strip():
            errors.append(field)
        elif str(actual) != str(expected):
            errors.append(field)
    period_present, period = _kosis_cell_response_value(cell, "PRD_DE")
    if not period_present or period in (None, "") or not str(period).strip():
        errors.append("PRD_DE")
    return errors


def exact_cell(
    plan: Mapping[str, Any],
    fetcher: Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
) -> dict[str, Any]:
    """A local exact-one wrapper for baseline reads."""
    response = fetcher(dict(plan))
    if isinstance(response, Mapping):
        return {"status": "CELL_API_ERROR", "query": dict(plan), "response": dict(response)}
    if not isinstance(response, list):
        return {"status": "CELL_RESPONSE_INVALID", "query": dict(plan)}
    if len(response) == 0:
        return {"status": "NO_CELL", "query": dict(plan), "rows": []}
    if len(response) != 1:
        return {"status": "MULTIPLE_CELLS", "query": dict(plan), "row_count": len(response)}
    cell = dict(response[0])
    errors = _cell_identity_errors(plan, cell)
    period = _cell_period(cell)
    expected = _period_key(plan.get("start_prd_de"))
    if period and expected and period != expected:
        errors.append("prd_de")
    if errors:
        return {"status": "CELL_QUERY_MISMATCH", "query": dict(plan), "mismatch_fields": errors}
    response_sha = hashlib.sha256(canonical_json(response)).hexdigest()
    return {
        "status": "CELL_RESOLVED", "query": dict(plan), "cell": cell,
        "response_sha256": response_sha,
    }


def range_cells(
    plan: Mapping[str, Any],
    fetcher: Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
    expected_periods: Sequence[str],
) -> dict[str, Any]:
    """Fetch and validate an exact monthly set against one series identity."""
    expected = tuple(expected_periods)
    # A KOSIS monthly range returns every month between its bounds.  The
    # same-month operators intentionally require only April (or another
    # single month) cells, so issue period-only exact reads for that sparse
    # set.  The contiguous streak operator remains one range request.
    contiguous = month_periods(plan.get("start_prd_de"), plan.get("end_prd_de"))
    if expected and tuple(expected) != tuple(contiguous):
        rows: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        for period in expected:
            exact_plan = period_only_plan(plan, period, period)
            fetched = exact_cell(exact_plan, fetcher)
            receipts.append(fetched)
            if fetched.get("status") != "CELL_RESOLVED":
                status = fetched.get("status")
                if status == "NO_CELL":
                    status = "RANGE_CELLS_MISSING"
                elif status == "MULTIPLE_CELLS":
                    status = "RANGE_CELLS_DUPLICATED"
                elif status == "CELL_QUERY_MISMATCH":
                    status = "SERIES_IDENTITY_CHANGED"
                return {
                    "status": status or "CELL_API_ERROR",
                    "query": dict(plan), "periods": [_cell_period(row) for row in rows],
                    "expected_periods": list(expected), "period_receipts": receipts,
                }
            rows.append(dict(fetched["cell"]))
        response = rows
    else:
        response = fetcher(dict(plan))
    if isinstance(response, Mapping):
        return {"status": "CELL_API_ERROR", "query": dict(plan), "response": dict(response)}
    if not isinstance(response, list):
        return {"status": "CELL_RESPONSE_INVALID", "query": dict(plan)}
    periods = [_cell_period(dict(row)) if isinstance(row, Mapping) else "" for row in response]
    if len(response) != len(expected):
        # Keep the more actionable stable code when a duplicate is visible.
        if len(periods) != len(set(periods)):
            status = "RANGE_CELLS_DUPLICATED"
        else:
            status = "RANGE_CELLS_MISSING" if len(response) < len(expected) else "RANGE_CELLS_UNEXPECTED"
        return {"status": status, "query": dict(plan), "row_count": len(response), "periods": periods, "expected_periods": list(expected)}
    if any(not period for period in periods):
        return {"status": "RANGE_CELLS_UNEXPECTED", "query": dict(plan), "periods": periods, "expected_periods": list(expected)}
    if len(set(periods)) != len(periods):
        return {"status": "RANGE_CELLS_DUPLICATED", "query": dict(plan), "periods": periods, "expected_periods": list(expected)}
    if set(periods) != set(expected):
        status = "RANGE_CELLS_MISSING" if set(periods) < set(expected) else "RANGE_CELLS_UNEXPECTED"
        return {"status": status, "query": dict(plan), "periods": periods, "expected_periods": list(expected)}
    identity_errors = [
        (index, _cell_identity_errors(plan, dict(row)))
        for index, row in enumerate(response)
        if isinstance(row, Mapping) and _cell_identity_errors(plan, dict(row))
    ]
    if identity_errors:
        return {"status": "SERIES_IDENTITY_CHANGED", "query": dict(plan), "identity_errors": identity_errors}
    ordered = tuple(sorted((dict(row) for row in response), key=lambda row: _cell_period(row)))
    response_sha = hashlib.sha256(canonical_json(ordered)).hexdigest()
    return {
        "status": "RANGE_RESOLVED", "query": dict(plan), "cells": list(ordered),
        "periods": list(expected), "expected_periods": list(expected),
        "response_sha256": response_sha,
    }


def indicator_family_key(indicator: Any) -> str:
    """Remove only closed measurement-role suffixes from an indicator."""
    text = re.sub(r"\s+", " ", str(indicator or "")).strip()
    text = _FREQUENCY_PREFIX_RE.sub("", text)
    text = _RANKING_PREFIX.sub("", text)
    while text:
        changed = False
        for suffix in _ROLE_SUFFIXES:
            pattern = re.compile(rf"(?:\s*{re.escape(suffix)})$")
            match = pattern.search(text)
            if match:
                text = text[:match.start()].strip()
                changed = True
                break
        if not changed:
            break
    return text


def _sentence_indicator(sentence: str) -> str:
    match = _TOPIC_RE.search(sentence)
    if match:
        return match.group("body").strip()
    return ""


def _family_for_sentence(
    sentences: Sequence[Mapping[str, Any]], index: int,
) -> tuple[str, dict[str, Any] | None]:
    """Return a local indicator family and, only when safe, its donor."""
    current_row = sentences[index]
    current = str(current_row.get("text") or current_row.get("sentence_text") or "")
    explicit_indicator = current_row.get("indicator") or current_row.get("indicator_label")
    generic_topic = _GENERIC_TOPIC_RE.search(current)
    current_indicator = str(explicit_indicator or (generic_topic.group("body") if generic_topic else _sentence_indicator(current)))
    current_family = "" if _GENERIC_MEASURE_INDICATOR_RE.fullmatch(
        re.sub(r"\s+", " ", current_indicator).strip()
    ) else indicator_family_key(current_indicator)
    if _GENERIC_MEASURE_INDICATOR_RE.fullmatch(current_family):
        current_family = ""
    if current_family:
        return current_family, {"sentence_id": sentences[index].get("sentence_id"), "source": "explicit"}
    if index == 0:
        return "", None
    previous = sentences[index - 1]
    previous_indicator = previous.get("indicator") or previous.get("indicator_label")
    previous_family = indicator_family_key(
        str(previous_indicator or _sentence_indicator(str(previous.get("text") or previous.get("sentence_text") or "")))
    )
    if _GENERIC_MEASURE_INDICATOR_RE.fullmatch(previous_family):
        previous_family = ""
    if not previous_family:
        return "", None
    if _paragraph_id(sentences[index], previous) != _paragraph_id(
        previous, sentences[index - 2] if index > 1 else None
    ):
        return "", None
    return previous_family, {"sentence_id": previous.get("sentence_id"), "source": "adjacent_inherited"}


def _published_year_month(published_at: Any) -> tuple[int, int] | None:
    match = re.match(r"(\d{4})-(\d{1,2})", str(published_at or ""))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) if 1 <= month <= 12 else None


def parse_named_month(token: str, published_at: Any) -> str:
    match = _MONTH_RE.fullmatch(token.strip())
    if match:
        return f"{match.group('year')}-{int(match.group('month')):02d}"
    match = _SHORT_YEAR_MONTH_RE.fullmatch(token.strip())
    if match:
        return f"20{match.group('year')}-{int(match.group('month')):02d}"
    match = _NAMED_MONTH_RE.fullmatch(token.strip())
    if not match:
        return ""
    published = _published_year_month(published_at)
    if published is None:
        return ""
    year, month = published
    named = int(match.group("month"))
    if match.group("qualifier") == "지난":
        year = year if named < month else year - 1
    return f"{year:04d}-{named:02d}"


def _paragraph_id(sentence: Mapping[str, Any], previous: Mapping[str, Any] | None) -> Any:
    if "paragraph_id" in sentence:
        return sentence.get("paragraph_id")
    if previous is None:
        return 0
    gap = str(sentence.get("source_text_before") or "")
    if "\n\n" in gap:
        return (int(previous.get("paragraph_id") or 0) + 1)
    return previous.get("paragraph_id", 0)


def _month_context(
    sentences: Sequence[Mapping[str, Any]], index: int, published_at: Any,
    current_period_hint: str = "",
) -> tuple[str, dict[str, Any] | None]:
    current = sentences[index]
    text = str(current.get("text") or current.get("sentence_text") or "")
    current_hint = _period_key(current_period_hint)
    month_basis = re.search(r"(?P<month>0?[1-9]|1[0-2])월\s*기준", text)
    if month_basis and current_hint:
        return f"{current_hint[:4]}-{int(month_basis.group('month')):02d}", {
            "sentence_id": current.get("sentence_id"), "text": month_basis.group(0), "source": "explicit",
        }
    explicit = list(_MONTH_RE.finditer(text))
    if explicit:
        match = explicit[-1]
        period = parse_named_month(match.group(0), published_at)
        if current_hint:
            period = f"{current_hint[:4]}-{int(match.group('month')):02d}"
        return period, {"sentence_id": current.get("sentence_id"), "text": match.group(0), "source": "explicit"}
    named = list(_NAMED_MONTH_RE.finditer(text))
    if named:
        match = named[-1]
        period = parse_named_month(match.group(0), published_at)
        if current_hint:
            period = f"{current_hint[:4]}-{int(match.group('month')):02d}"
        return period, {"sentence_id": current.get("sentence_id"), "text": match.group(0), "source": "explicit"}
    # The receiver may inherit one immediately preceding, same-paragraph
    # month span.  No multi-sentence or cross-paragraph guessing is allowed.
    if index > 0:
        previous = sentences[index - 1]
        current_family, _ = _family_for_sentence(sentences, index)
        previous_family, _ = _family_for_sentence(sentences, index - 1)
        same_family = bool(current_family and previous_family and current_family == previous_family)
        if same_family and _paragraph_id(current, previous) == _paragraph_id(previous, sentences[index - 2] if index > 1 else None):
            previous_text = str(previous.get("text") or previous.get("sentence_text") or "")
            inherited = list(_MONTH_RE.finditer(previous_text))
            if inherited:
                match = inherited[-1]
                inherited_period = parse_named_month(match.group(0), published_at)
                if current_hint:
                    inherited_period = f"{current_hint[:4]}-{int(match.group('month')):02d}"
                return inherited_period, {
                    "sentence_id": previous.get("sentence_id"), "text": match.group(0), "source": "adjacent_inherited",
                }
            marker = re.search(r"(?P<month>0?[1-9]|1[0-2])월\s*기준", previous_text)
            if marker:
                inherited_period = parse_named_month(f"{marker.group('month')}월", published_at)
                if current_hint:
                    inherited_period = f"{current_hint[:4]}-{int(marker.group('month')):02d}"
                return inherited_period, {
                    "sentence_id": previous.get("sentence_id"), "text": marker.group(0), "source": "adjacent_inherited",
                }
    return "", None


def extract_range_specs(
    sentences: Sequence[Mapping[str, Any]], published_at: Any, current_period_hint: str = "",
) -> list[RangeSpec]:
    """Extract only the four registered range operators with provenance."""
    specs: list[RangeSpec] = []
    normalized = [dict(row) for row in sentences]
    for index, sentence_row in enumerate(normalized):
        text = str(sentence_row.get("text") or sentence_row.get("sentence_text") or "")
        sentence_id = sentence_row.get("sentence_id")
        paragraph = _paragraph_id(sentence_row, normalized[index - 1] if index else None)
        family, family_provenance = _family_for_sentence(normalized, index)
        family_meta = {
            "indicator_family_key": family,
            "indicator_family_provenance": family_provenance,
        }
        streak = _STREAK_RE.search(text)
        if streak:
            start = parse_named_month(f"{streak.group('start')}년 {streak.group('start_month')}월", published_at)
            end_token = f"{streak.group('end')} {streak.group('end_month')}월"
            end = parse_named_month(end_token, published_at)
            expected = month_periods(shift_month(start, -12), end)
            comparisons = month_periods(start, end)
            specs.append(RangeSpec(
                "YOY_STREAK", shift_month(start, -12), end, expected, comparisons,
                claim_sentence_id=sentence_id,
                source_provenance={"sentence_id": sentence_id, "paragraph_id": paragraph, "operator_source": text, "count": int(streak.group("count")), **family_meta},
                claim_count=int(streak.group("count")),
            ))
        month_anchor = _MONTH_ANCHOR_RE.search(text)
        if month_anchor:
            anchor = f"{month_anchor.group('anchor')}-{int(month_anchor.group('month')):02d}"
            current, context = _month_context(normalized, index, published_at, current_period_hint)
            if current and _period_key(current)[5:] == anchor[5:]:
                expected = same_month_periods(anchor, current)
                specs.append(RangeSpec(
                    "MONTH_OF_YEAR_MAX_SINCE", anchor, current, expected,
                    same_month_periods(shift_month(anchor, 12), current), anchor,
                    "AFTER_ANCHOR_EXCLUSIVE", sentence_id,
                    {"sentence_id": sentence_id, "paragraph_id": paragraph, "month_provenance": context, "operator_source": text, **family_meta},
                ))
            else:
                specs.append(RangeSpec(
                    "MONTH_OF_YEAR_MAX_SINCE", "", "", (), (), anchor,
                    "AFTER_ANCHOR_EXCLUSIVE", sentence_id,
                    {"sentence_id": sentence_id, "paragraph_id": paragraph, "month_provenance": context, "operator_source": text, **family_meta},
                ))
        since_rate = _SINCE_RATE_RE.search(text)
        if since_rate:
            current, context = _month_context(normalized, index, published_at, current_period_hint)
            anchor = f"{since_rate.group('year')}-{int(_period_key(current)[5:]):02d}" if current else ""
            if anchor and current:
                expected = same_month_periods(shift_month(anchor, -12), current)
                specs.append(RangeSpec(
                    "YOY_RATE_MAX_SINCE", shift_month(anchor, -12), current, expected,
                    same_month_periods(anchor, current), anchor, "SINCE_INCLUSIVE", sentence_id,
                    {"sentence_id": sentence_id, "paragraph_id": paragraph, "month_provenance": context, "operator_source": text, **family_meta},
                ))
            else:
                specs.append(RangeSpec(
                    "YOY_RATE_MAX_SINCE", "", "", (), (), anchor, "SINCE_INCLUSIVE", sentence_id,
                    {"sentence_id": sentence_id, "paragraph_id": paragraph, "month_provenance": context, "operator_source": text, **family_meta},
                ))
        difference = _DIFFERENCE_RE.search(text)
        if difference:
            anchor = f"{difference.group('anchor')}-{int(difference.group('month')):02d}"
            current, context = _month_context(normalized, index, published_at, current_period_hint)
            if current and _period_key(current)[5:] == anchor[5:]:
                expected = same_month_periods(shift_month(anchor, -12), current)
                specs.append(RangeSpec(
                    "YOY_DIFFERENCE_MAX_SINCE", shift_month(anchor, -12), current, expected,
                    same_month_periods(shift_month(anchor, 12), current), anchor,
                    "AFTER_ANCHOR_EXCLUSIVE", sentence_id,
                    {"sentence_id": sentence_id, "paragraph_id": paragraph, "month_provenance": context, "operator_source": text, **family_meta},
                ))
            else:
                specs.append(RangeSpec(
                    "YOY_DIFFERENCE_MAX_SINCE", "", "", (), (), anchor,
                    "AFTER_ANCHOR_EXCLUSIVE", sentence_id,
                    {"sentence_id": sentence_id, "paragraph_id": paragraph, "month_provenance": context, "operator_source": text, **family_meta},
                ))
    return specs


def _decimal(cell: Mapping[str, Any], unit: str) -> Decimal:
    try:
        return normalize_quantity(
            cell.get("DT"), unit,
            provenance={"source": "KOSIS", "period": _cell_period(cell)},
        ).value_base
    except (QuantityNormalizationError, InvalidOperation) as exc:
        raise SameSeriesEvidenceError("RANGE_VALUE_INVALID") from exc


def evaluate_range(
    spec: RangeSpec, cells: Sequence[Mapping[str, Any]], *, official_unit: str,
    current_period: str,
) -> dict[str, Any]:
    """Evaluate one exact-cardinality operator with Decimal arithmetic."""
    if not spec.expected_periods or not spec.start_period or not spec.end_period:
        return {"operator": spec.operator, "status": "not_evaluated", "reason": "RANGE_CLAIM_MONTH_SCOPE_AMBIGUOUS", "provenance": dict(spec.source_provenance or {})}
    by_period = {_cell_period(cell): dict(cell) for cell in cells}
    if tuple(sorted(by_period)) != tuple(sorted(spec.expected_periods)) or len(by_period) != len(cells):
        return {"operator": spec.operator, "status": "not_evaluated", "reason": "RANGE_CELLS_CARDINALITY_INVALID", "provenance": dict(spec.source_provenance or {})}
    try:
        values = {period: _decimal(cell, official_unit) for period, cell in by_period.items()}
    except SameSeriesEvidenceError as exc:
        return {"operator": spec.operator, "status": "not_evaluated", "reason": exc.code, "provenance": dict(spec.source_provenance or {})}
    finding: dict[str, Any] = {
        "operator": spec.operator, "status": "evaluated", "finding": False,
        "raw_cell_count": len(cells), "expected_raw_cell_count": len(spec.expected_periods),
        "comparison_periods": list(spec.comparison_periods),
        "anchor_period": spec.anchor_period, "anchor_inclusion": spec.anchor_inclusion,
        "input_range": {"start": spec.start_period, "end": spec.end_period},
        "provenance": dict(spec.source_provenance or {}),
    }
    if spec.operator == "YOY_STREAK":
        comparisons = []
        for period in spec.comparison_periods:
            baseline = shift_month(period, -12)
            if baseline not in values:
                return {**finding, "status": "not_evaluated", "reason": "RANGE_CELLS_MISSING"}
            comparisons.append({"period": period, "baseline_period": baseline, "current": format(values[period], "f"), "baseline": format(values[baseline], "f"), "increase": values[period] > values[baseline]})
        finding.update({"finding": bool(spec.claim_count == len(comparisons) and all(row["increase"] for row in comparisons)), "comparisons_checked": len(comparisons), "expected_comparisons": spec.claim_count, "comparisons": comparisons})
    elif spec.operator == "MONTH_OF_YEAR_MAX_SINCE":
        current = _period_key(current_period)
        comparison_values = {period: values[period] for period in spec.comparison_periods}
        finding.update({"finding": current in comparison_values and all(comparison_values[current] > value for period, value in comparison_values.items() if period != current), "current_period": current, "current_value": format(comparison_values.get(current, Decimal(0)), "f"), "maximum_period": max(comparison_values, key=comparison_values.get) if comparison_values else None, "comparison_count": len(comparison_values), "anchor_value": format(values[spec.anchor_period], "f") if spec.anchor_period in values else None})
    elif spec.operator in {"YOY_RATE_MAX_SINCE", "YOY_DIFFERENCE_MAX_SINCE"}:
        comparison_periods = list(spec.comparison_periods)
        deltas: dict[str, Decimal] = {}
        for period in comparison_periods:
            baseline = shift_month(period, -12)
            if baseline not in values:
                return {**finding, "status": "not_evaluated", "reason": "RANGE_CELLS_MISSING"}
            if values[baseline] == 0:
                return {**finding, "status": "not_evaluated", "reason": "RANGE_ZERO_BASELINE"}
            deltas[period] = values[period] - values[baseline]
        if spec.operator == "YOY_RATE_MAX_SINCE":
            rates = {period: (deltas[period] / abs(values[shift_month(period, -12)])) * Decimal(100) for period in comparison_periods}
            current = _period_key(current_period)
            finding.update({"finding": current in rates and all(rates[current] > value for period, value in rates.items() if period != current), "current_period": current, "current_rate_percent": format(rates.get(current, Decimal(0)), "f"), "maximum_period": max(rates, key=rates.get) if rates else None, "comparison_count": len(rates), "rates": {period: format(value, "f") for period, value in rates.items()}})
        else:
            current = _period_key(current_period)
            anchor_delta = None
            if spec.anchor_period and shift_month(spec.anchor_period, -12) in values:
                anchor_delta = values[spec.anchor_period] - values[shift_month(spec.anchor_period, -12)]
            finding.update({"finding": current in deltas and all(deltas[current] > value for period, value in deltas.items() if period != current), "current_period": current, "current_difference": format(deltas.get(current, Decimal(0)), "f"), "maximum_period": max(deltas, key=deltas.get) if deltas else None, "comparison_count": len(deltas), "anchor_difference": format(anchor_delta, "f") if anchor_delta is not None else None, "differences": {period: format(value, "f") for period, value in deltas.items()}})
    else:
        return {**finding, "status": "not_evaluated", "reason": "RANGE_OPERATOR_UNSUPPORTED"}
    return finding


def _format_decimal(value: Decimal) -> str:
    return format(value, ",f").rstrip("0").rstrip(".") if value % 1 else format(value, ",.0f")


def _format_period(period: Any) -> str:
    key = _period_key(period)
    return f"{key[:4]}년 {int(key[5:])}월" if key else str(period or "")


def _claim_value(rows: Sequence[Mapping[str, Any]], kind: str) -> str:
    for row in rows:
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
        if fields.get("measurement_type") == kind:
            return str(row.get("value_text") or "")
    return ""


def build_evidence_text(
    *, current_period: str, baseline_period: str, indicator: str,
    current_value: Decimal, baseline_value: Decimal, official_unit: str,
    claim_difference: str = "", claim_rate: str = "", range_findings: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Build the fixed CURRENT_RELEASE user wording from sealed quantities."""
    difference = current_value - baseline_value
    rate = (difference / abs(baseline_value) * Decimal(100)) if baseline_value else Decimal(0)
    current_label = f"{_format_decimal(current_value)}{official_unit}"
    baseline_label = f"{_format_decimal(baseline_value)}{official_unit}"
    text = (
        f"현재 KOSIS 통계표에서는 {_format_period(current_period)} {indicator}가 {current_label}이고, "
        f"{_format_period(baseline_period)}은 {baseline_label}입니다. 따라서 전년동월 대비 증가는 "
        f"{_format_decimal(difference)}{official_unit}, 증가율은 약 {rate.quantize(Decimal('0.1'))}%입니다."
    )
    claims = []
    if claim_difference:
        claims.append(claim_difference)
    if claim_rate:
        claims.append(claim_rate)
    if claims:
        text += f" 기사에는 {'·'.join(claims)}로 적혀 있습니다."
    text += " 현재 KOSIS 조회값과 기사 기재값이 다른 원인은 기사 작성 당시의 공식 통계 snapshot이 없어 확인할 수 없습니다."
    evaluated = [row for row in range_findings if row.get("status") == "evaluated"]
    if evaluated:
        descriptions: list[str] = []
        for row in evaluated:
            if row.get("operator") == "YOY_STREAK":
                descriptions.append(f"{_format_period(row['input_range']['start'])}부터 {_format_period(row['input_range']['end'])}까지 전년동월 대비 증가는 {row['comparisons_checked']}개월 연속")
            elif row.get("operator") == "MONTH_OF_YEAR_MAX_SINCE":
                descriptions.append(f"{_format_period(row['current_period'])} 출생아 수는 {_format_period(row['anchor_period'])} 이후 비교 구간에서 가장 많음")
            elif row.get("operator") == "YOY_RATE_MAX_SINCE":
                descriptions.append(f"{_format_period(row['current_period'])} 증가율은 {row['anchor_period'][:4]}년 이후 4월 기준 가장 높음")
            elif row.get("operator") == "YOY_DIFFERENCE_MAX_SINCE":
                descriptions.append(f"{_format_period(row['current_period'])} 증가폭은 {_format_period(row['anchor_period'])} 이후 비교 구간에서 가장 큼")
        if descriptions:
            text += " 같은 통계표의 현재 release 월별 자료를 같은 기준으로 계산하면 " + ", ".join(descriptions) + "입니다."
    return text


def select_primary_target(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Select one same-article-sentence series group; distinct spans are expected."""
    groups: dict[tuple[Any, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
        measurement = str(fields.get("measurement_type") or "")
        if measurement not in {"LEVEL", "CHANGE_POINT", "CHANGE_RATE"}:
            continue
        span_id = str(row.get("value_span_id") or "")
        if not span_id:
            continue
        sentence_key = row.get("article_sentence_id")
        if sentence_key is None:
            sentence_key = row.get("sentence_id")
        if sentence_key is None:
            continue
        period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
        measurement_period = ((period.get("measurement") or {}).get("absolute") if isinstance(period.get("measurement"), Mapping) else "")
        family = indicator_family_key(str(fields.get("indicator") or row.get("indicator_label") or ""))
        groups.setdefault((sentence_key, family, str(measurement_period or "")), []).append(row)
    eligible = []
    for key, group in groups.items():
        levels = [row for row in group if str((row.get("retrieval_fields") or {}).get("measurement_type") or "") == "LEVEL"]
        siblings = [row for row in group if str((row.get("retrieval_fields") or {}).get("measurement_type") or "") in {"CHANGE_POINT", "CHANGE_RATE"}]
        sibling_baselines: list[str] = []
        for sibling in siblings:
            fields = sibling.get("retrieval_fields") if isinstance(sibling.get("retrieval_fields"), Mapping) else {}
            period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
            baseline = period.get("baseline") if isinstance(period.get("baseline"), Mapping) else {}
            sibling_baselines.append(_period_key(baseline.get("absolute")))
        if (
            len(levels) == 1
            and siblings
            and all(sibling_baselines)
            and len(set(sibling_baselines)) == 1
        ):
            eligible.append((key, levels[0], siblings, sibling_baselines[0]))
    if len(eligible) != 1:
        raise SameSeriesEvidenceError("PRIMARY_TARGET_AMBIGUOUS")
    key, level, siblings, baseline_period = eligible[0]
    ordered_rows = [level, *siblings]
    return {
        "group_key": key,
        "primary": dict(level),
        "siblings": [dict(row) for row in siblings],
        "indicator_family_key": key[1],
        "baseline_period": baseline_period,
        "value_span_ids": tuple(str(row.get("value_span_id") or "") for row in ordered_rows),
    }


def synthesize_same_series_evidence(
    *, article: Mapping[str, Any], routed_rows: Sequence[Mapping[str, Any]],
    ledgers: Sequence[Mapping[str, Any]], answers: Sequence[Mapping[str, Any]],
    cell_fetcher: Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
    exact_fetcher: Callable[[Mapping[str, Any], Callable[..., Any]], dict[str, Any]] | None = None,
    feature_enabled: bool = False, release_id: str = "",
) -> dict[str, Any]:
    """Attach one sealed evidence_answer to the unique primary LEVEL answer."""
    result: dict[str, Any] = {"contract_version": CONTRACT_VERSION, "feature_enabled": feature_enabled, "truth_mode": CURRENT_RELEASE, "status": "NOT_EVALUATED"}
    if not feature_enabled:
        result["reason"] = "FEATURE_DISABLED"
        return result
    try:
        primary_info = select_primary_target(routed_rows)
    except SameSeriesEvidenceError as exc:
        result["reason"] = exc.code
        return result
    primary = primary_info["primary"]
    target_id = f"{primary.get('article_idx')}:{primary.get('value_span_id') or primary.get('sentence_id') or 'target'}"
    ledger = next((dict(row) for row in ledgers if str(row.get("target_id") or "") == target_id), None)
    if not ledger:
        result["reason"] = "PRIMARY_LEDGER_MISSING"
        return result
    current_cell = ledger.get("cell") if isinstance(ledger.get("cell"), Mapping) else {}
    resolution = ledger.get("resolution") if isinstance(ledger.get("resolution"), Mapping) else {}
    query_plan = ledger.get("query_plan") if isinstance(ledger.get("query_plan"), Mapping) else resolution.get("query_plan")
    if str(resolution.get("outcome") or "") != "QUERY_READY" or str(current_cell.get("status") or "") != "CELL_RESOLVED" or not isinstance(query_plan, Mapping):
        result["reason"] = "PRIMARY_CELL_NOT_RESOLVED"
        return result
    fields = primary.get("retrieval_fields") if isinstance(primary.get("retrieval_fields"), Mapping) else {}
    period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
    current_period = _period_key(((period.get("measurement") or {}).get("absolute") if isinstance(period.get("measurement"), Mapping) else "") or query_plan.get("start_prd_de"))
    baseline_period = _period_key(primary_info.get("baseline_period"))
    if not current_period or not baseline_period:
        result["reason"] = "PAIR_PERIOD_UNAVAILABLE"
        return result
    official_unit = str(ledger.get("official_unit") or primary.get("value_unit") or "")
    if not official_unit:
        result["reason"] = "UNIT_UNAVAILABLE"
        return result
    baseline_plan = period_only_plan(query_plan, baseline_period, baseline_period)
    current_release = str(ledger.get("release_id") or release_id or "")
    current_profile = str(ledger.get("profile_sha256") or "")
    try:
        validate_period_only_identity(
            query_plan, baseline_plan,
            current_release_id=current_release, requery_release_id=current_release,
            current_profile_sha256=current_profile, requery_profile_sha256=current_profile,
            selected_assignment_provenance=ledger.get("assignment_provenance") or ledger.get("assignment") or {},
            requery_assignment_provenance=ledger.get("assignment_provenance") or ledger.get("assignment") or {},
        )
    except SameSeriesEvidenceError as exc:
        result["reason"] = exc.code
        return result
    memoized_cell_fetcher, cell_fetch_cache = _memoized_cell_fetcher_v2l(cell_fetcher)
    exact = exact_fetcher or exact_cell
    baseline = exact(baseline_plan, memoized_cell_fetcher)
    if baseline.get("status") != "CELL_RESOLVED":
        result["reason"] = baseline.get("status") or "BASELINE_CELL_UNAVAILABLE"
        return result
    try:
        current_value = _decimal(current_cell.get("cell") or {}, official_unit)
        baseline_value = _decimal(baseline.get("cell") or {}, official_unit)
    except SameSeriesEvidenceError as exc:
        result["reason"] = exc.code
        return result
    sentence_rows = []
    try:
        from src.news_verification.runtime.l1_value_candidates import sentence_offset_map
        article_text = str(article.get("article_text") or "")
        sentence_rows = sentence_offset_map(article_text)
        paragraph = 0
        for index, sentence_row in enumerate(sentence_rows):
            start = int(sentence_row.get("char_start") or 0)
            if index and "\n\n" in article_text[int(sentence_rows[index - 1].get("char_end") or 0):start]:
                paragraph += 1
            sentence_row["paragraph_id"] = paragraph
    except Exception:
        sentence_rows = []
    range_specs = extract_range_specs(
        sentence_rows, article.get("date") or article.get("article_date"), current_period,
    )
    range_findings: list[dict[str, Any]] = []
    range_receipts: list[dict[str, Any]] = []
    for spec in range_specs:
        spec_family = str((spec.source_provenance or {}).get("indicator_family_key") or "")
        if not spec_family:
            range_findings.append({
                "operator": spec.operator, "status": "not_evaluated",
                "reason": "RANGE_CLAIM_INDICATOR_FAMILY_AMBIGUOUS",
                "expected_raw_cell_count": len(spec.expected_periods),
                "provenance": dict(spec.source_provenance or {}),
            })
            continue
        if spec_family != str(primary_info.get("indicator_family_key") or ""):
            range_findings.append({
                "operator": spec.operator, "status": "not_evaluated",
                "reason": "RANGE_CLAIM_INDICATOR_FAMILY_MISMATCH",
                "expected_raw_cell_count": len(spec.expected_periods),
                "provenance": dict(spec.source_provenance or {}),
            })
            continue
        if not spec.expected_periods:
            range_findings.append(evaluate_range(spec, (), official_unit=official_unit, current_period=current_period))
            continue
        range_plan = period_only_plan(query_plan, spec.start_period, spec.end_period)
        try:
            validate_period_only_identity(
                query_plan, range_plan,
                current_release_id=current_release, requery_release_id=current_release,
                current_profile_sha256=current_profile, requery_profile_sha256=current_profile,
                selected_assignment_provenance=ledger.get("assignment_provenance") or ledger.get("assignment") or {},
                requery_assignment_provenance=ledger.get("assignment_provenance") or ledger.get("assignment") or {},
            )
        except SameSeriesEvidenceError as exc:
            range_findings.append({
                "operator": spec.operator, "status": "not_evaluated", "reason": exc.code,
                "expected_raw_cell_count": len(spec.expected_periods),
                "provenance": dict(spec.source_provenance or {}),
            })
            continue
        fetched = range_cells(range_plan, memoized_cell_fetcher, spec.expected_periods)
        range_receipts.append({"operator": spec.operator, "query": range_plan, "status": fetched.get("status"), "response_sha256": fetched.get("response_sha256"), "expected_raw_cell_count": len(spec.expected_periods), "periods": fetched.get("periods", [])})
        if fetched.get("status") != "RANGE_RESOLVED":
            range_findings.append({"operator": spec.operator, "status": "not_evaluated", "reason": fetched.get("status"), "raw_cell_count": len(fetched.get("periods") or []), "expected_raw_cell_count": len(spec.expected_periods), "provenance": dict(spec.source_provenance or {})})
        else:
            range_findings.append(evaluate_range(spec, fetched.get("cells") or [], official_unit=official_unit, current_period=current_period))
    evidence_text = build_evidence_text(
        current_period=current_period, baseline_period=baseline_period,
        indicator=str(fields.get("indicator") or primary.get("indicator_label") or "통계 지표"),
        current_value=current_value, baseline_value=baseline_value, official_unit=official_unit,
        claim_difference=_claim_value(primary_info["siblings"], "CHANGE_POINT"),
        claim_rate=_claim_value(primary_info["siblings"], "CHANGE_RATE"),
        range_findings=range_findings,
    )
    evidence = {
        "contract_version": CONTRACT_VERSION, "truth_mode": CURRENT_RELEASE,
        "text": evidence_text, "target_id": target_id, "table_key": ledger.get("table_key") or query_plan.get("table_key"),
        "release_id": current_release, "profile_sha256": current_profile,
        "periods": {"current": current_period, "baseline": baseline_period},
        "observed_values": {"current": format(current_value, "f"), "baseline": format(baseline_value, "f")},
        "calculations": {"difference": format(current_value - baseline_value, "f"), "percent_change": format((current_value - baseline_value) / abs(baseline_value) * Decimal(100), "f") if baseline_value else None},
        "current_cell": dict(current_cell), "baseline_cell": baseline,
        "range_findings": range_findings, "range_receipts": range_receipts,
        "historical_vintage_status": "UNAVAILABLE",
        "limitation": "현재 KOSIS 조회값과 기사 기재값이 다른 원인은 기사 작성 당시의 공식 통계 snapshot이 없어 확인할 수 없습니다.",
        "identity": {"query_plan_without_period_sha256": plan_identity_without_period(query_plan), "period_only_requery": True, "search_calls": 0, "reranker_calls": 0, "projection_calls": 0, "selector_calls": 0, "cell_fetch_cache": copy.deepcopy(cell_fetch_cache)},
    }
    for answer in answers:
        if str(answer.get("target_id") or "") == target_id and isinstance(answer, dict):
            answer["evidence_answer"] = evidence
    for item in ledgers:
        if str(item.get("target_id") or "") == target_id and isinstance(item, dict):
            item["evidence_answer"] = evidence
            item["same_series_evidence"] = {"status": "EVALUATED", "baseline": baseline, "range_receipts": range_receipts}
    result.update({"status": "EVALUATED", "evidence_answer": evidence, "target_id": target_id})
    return result


__all__ = [
    "CONTRACT_VERSION", "CURRENT_RELEASE", "FEATURE_GATE_ENV", "RangeSpec",
    "SameSeriesEvidenceError", "canonical_json", "exact_cell", "evaluate_range",
    "extract_range_specs", "indicator_family_key", "month_periods", "same_month_periods", "period_api_value",
    "period_only_plan", "plan_identity_without_period", "range_cells", "select_primary_target",
    "shift_month", "synthesize_same_series_evidence", "validate_period_only_identity",
]
