"""Assemble L2 contract v3 review JSON from click-level labelling decisions.

The reviewer never types JSON, IDs or character offsets.  The labeller UI
reports which text was selected and which value candidates were attached;
this module turns that into the contract fields and allocates article-scoped
IDs so the reviewer does no bookkeeping.
"""

from __future__ import annotations

import json
import re
from typing import Any

try:
    from .l2_span_resolver import (
        SpanResolutionError,
        parse_value_candidate_span_ids,
        resolve_span,
    )
except ImportError:  # pragma: no cover - direct script execution
    from l2_span_resolver import (  # type: ignore[no-redef]
        SpanResolutionError,
        parse_value_candidate_span_ids,
        resolve_span,
    )


NO_DOMINANT_REGION = "지배 없음"
UNDECIDED = "판단 불가"
INTRODUCED = "이 문장에서 도입"
INHERITED = "앞에서 상속"

# ``지배 없음`` is an answer: no source region governs this sentence.
# ``판단 불가`` is an abstention: the reviewer could not decide.  Gate L2 has
# to tell those apart, so abstention is recorded as a value rather than as an
# empty cell.
NON_REFERENCE_DOMINANCE = frozenset({NO_DOMINANT_REGION, UNDECIDED})


def occurrence_index_for_offset(
    sentence_text: str,
    span_text: str,
    start: object,
) -> int | None:
    """Return which occurrence of ``span_text`` begins at ``start``.

    The UI reports the offset of the reviewer's selection, so the occurrence
    is known exactly and never has to be guessed or typed.
    """
    if not isinstance(sentence_text, str) or not isinstance(span_text, str):
        return None
    if not span_text:
        return None
    try:
        offset = int(start)
    except (TypeError, ValueError):
        return None
    index = 0
    cursor = sentence_text.find(span_text)
    while cursor >= 0:
        if cursor == offset:
            return index
        index += 1
        cursor = sentence_text.find(span_text, cursor + 1)
    return None


def _id_number(value: str) -> int:
    match = re.search(r"(\d+)$", value or "")
    return int(match.group(1)) if match else 0


def allocate_id(article_idx: str, kind: str, taken: set[str]) -> str:
    """Return the next unused article-scoped scope/region ID."""
    prefix = "SC" if kind == "scope" else "R"
    used = {value for value in taken if value.startswith(f"{article_idx}-{prefix}")}
    number = max((_id_number(value) for value in used), default=0) + 1
    return f"{article_idx}-{prefix}{number:02d}"


def existing_ids(
    article_idx: str,
    context_rows: list[dict[str, Any]],
    human_rows: list[dict[str, Any]],
    kind: str,
) -> set[str]:
    """Return every scope/region ID already defined inside one article."""
    field = "scope_id" if kind == "scope" else "region_id"
    found: set[str] = set()
    for row in context_rows:
        if str(row.get("article_idx") or "") != article_idx:
            continue
        if row.get("row_kind") == "자동확정" and row.get(field):
            found.add(str(row[field]))
    json_field = (
        "indicator_scopes_json" if kind == "scope" else "source_regions_json"
    )
    for row in human_rows:
        if str(row.get("article_idx") or "") != article_idx:
            continue
        raw = row.get(json_field)
        if not raw:
            continue
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        for entry in entries or []:
            if kind == "scope" and entry.get("attribution_type") == INHERITED:
                continue
            if entry.get(field):
                found.add(str(entry[field]))
    return found


def region_choices(
    article_idx: str,
    context_rows: list[dict[str, Any]],
    human_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return the dominant-region dropdown options for one article."""
    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in context_rows:
        if str(row.get("article_idx") or "") != article_idx:
            continue
        if row.get("row_kind") != "자동확정" or not row.get("region_id"):
            continue
        region_id = str(row["region_id"])
        if region_id in seen:
            continue
        seen.add(region_id)
        choices.append({
            "region_id": region_id,
            "source_subtype": str(row.get("source_subtype") or ""),
            "origin": "자동확정",
            "sentence_review_id": str(row.get("sentence_review_id") or ""),
            "text": str(row.get("text") or "")[:60],
        })
    for row in human_rows:
        if str(row.get("article_idx") or "") != article_idx:
            continue
        raw = row.get("source_regions_json")
        if not raw:
            continue
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        for entry in entries or []:
            region_id = str(entry.get("region_id") or "")
            if not region_id or region_id in seen:
                continue
            seen.add(region_id)
            choices.append({
                "region_id": region_id,
                "source_subtype": str(entry.get("source_subtype") or ""),
                "origin": "사람 정의",
                "sentence_review_id": str(row.get("sentence_review_id") or ""),
                "text": str(entry.get("source_span_text") or ""),
            })
    choices.sort(key=lambda item: _id_number(item["region_id"]))
    return choices


def scope_choices(
    article_idx: str,
    context_rows: list[dict[str, Any]],
    human_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return inheritable scope options for one article."""
    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in context_rows:
        if str(row.get("article_idx") or "") != article_idx:
            continue
        if row.get("row_kind") != "자동확정" or not row.get("scope_id"):
            continue
        scope_id = str(row["scope_id"])
        if scope_id in seen:
            continue
        seen.add(scope_id)
        choices.append({
            "scope_id": scope_id,
            "indicator_label": str(row.get("indicator_label") or ""),
            "origin": "자동확정",
            "sentence_review_id": str(row.get("sentence_review_id") or ""),
        })
    for row in human_rows:
        if str(row.get("article_idx") or "") != article_idx:
            continue
        raw = row.get("indicator_scopes_json")
        if not raw:
            continue
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        for entry in entries or []:
            if entry.get("attribution_type") == INHERITED:
                continue
            scope_id = str(entry.get("scope_id") or "")
            if not scope_id or scope_id in seen:
                continue
            seen.add(scope_id)
            choices.append({
                "scope_id": scope_id,
                "indicator_label": str(entry.get("indicator_label") or ""),
                "origin": "사람 정의",
                "sentence_review_id": str(row.get("sentence_review_id") or ""),
            })
    choices.sort(key=lambda item: _id_number(item["scope_id"]))
    return choices


def _clean_span(entry: dict[str, Any], sentence_text: str) -> dict[str, Any]:
    span_text = str(entry.get("source_span_text") or "")
    occurrence = entry.get("occurrence_index")
    if occurrence in (None, "") and span_text:
        occurrence = occurrence_index_for_offset(
            sentence_text,
            span_text,
            entry.get("source_char_start"),
        )
    payload: dict[str, Any] = {"source_span_text": span_text}
    if occurrence not in (None, ""):
        payload["occurrence_index"] = int(occurrence)
    return payload


def assemble_review_row(
    row: dict[str, Any],
    decision: dict[str, Any],
    taken_scope_ids: set[str],
    taken_region_ids: set[str],
) -> dict[str, Any]:
    """Return ``row`` updated with contract v3 JSON built from ``decision``."""
    sentence_text = str(row.get("text") or "")
    article_idx = str(row.get("article_idx") or "")
    offered = set(
        parse_value_candidate_span_ids(row.get("value_candidate_span_ids"))
    )
    scopes: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    scope_ids = set(taken_scope_ids)
    for item in decision.get("scopes") or []:
        attribution = str(item.get("attribution_type") or INTRODUCED)
        if attribution == INHERITED:
            scope_id = str(item.get("scope_id") or "")
            if not scope_id:
                raise ValueError("상속 scope는 참조할 scope_id가 필요합니다")
            entry: dict[str, Any] = {
                "scope_id": scope_id,
                "indicator_label": str(item.get("indicator_label") or ""),
                "attribution_type": INHERITED,
            }
            if str(item.get("source_span_text") or "").strip():
                entry.update(_clean_span(item, sentence_text))
        else:
            scope_id = str(item.get("scope_id") or "")
            if not scope_id or scope_id in scope_ids:
                # ``scope_ids`` already holds every ID taken by another row and
                # by earlier entries in this one, so this covers both a
                # borrowed ID and two indicators inside one sentence claiming
                # the same ID.  One ID must never name two indicators.
                scope_id = allocate_id(article_idx, "scope", scope_ids)
            scope_ids.add(scope_id)
            entry = {
                "scope_id": scope_id,
                "indicator_label": str(item.get("indicator_label") or ""),
                "attribution_type": INTRODUCED,
            }
            entry.update(_clean_span(item, sentence_text))
            if not entry.get("source_span_text"):
                raise ValueError(
                    f"{scope_id}: 이 문장에서 도입한 지표는 근거 표현을 "
                    "원문에서 선택해야 합니다"
                )
            resolve_span(
                sentence_text,
                entry["source_span_text"],
                entry.get("occurrence_index"),
            )
        scopes.append(entry)
        value_ids = [
            str(value) for value in (item.get("value_span_ids") or [])
        ]
        unknown = [value for value in value_ids if value not in offered]
        if unknown:
            raise ValueError(
                f"{scope_id}: 이 문장의 값 후보가 아닙니다 — "
                f"{', '.join(unknown)}"
            )
        if not value_ids:
            continue
        clause_text = str(item.get("clause_text") or "").strip()
        boundary: dict[str, Any] = {
            "scope_id": scope_id,
            "boundary_type": "절" if clause_text else "값",
            "target_value_span_ids": value_ids,
        }
        if clause_text:
            resolve_span(sentence_text, clause_text)
            boundary["clause_text"] = clause_text
        boundaries.append(boundary)

    regions: list[dict[str, Any]] = []
    region_ids = set(taken_region_ids)
    for item in decision.get("regions") or []:
        region_id = str(item.get("region_id") or "")
        if not region_id or region_id in region_ids:
            region_id = allocate_id(article_idx, "region", region_ids)
        region_ids.add(region_id)
        entry = {
            "region_id": region_id,
            "source_subtype": str(item.get("source_subtype") or ""),
        }
        entry.update(_clean_span(item, sentence_text))
        if not entry.get("source_span_text"):
            raise ValueError(
                f"{region_id}: 출처 근거 표현을 원문에서 선택해야 합니다"
            )
        resolve_span(
            sentence_text,
            entry["source_span_text"],
            entry.get("occurrence_index"),
        )
        regions.append(entry)

    periods: list[dict[str, Any]] = []
    for item in decision.get("periods") or []:
        entry = {
            "period_raw": str(item.get("period_raw") or ""),
            "period_absolute": str(item.get("period_absolute") or ""),
            "published_at": str(row.get("published_at") or ""),
        }
        entry.update(_clean_span(item, sentence_text))
        if not entry.get("source_span_text"):
            raise ValueError("기간 근거 표현을 원문에서 선택해야 합니다")
        resolve_span(
            sentence_text,
            entry["source_span_text"],
            entry.get("occurrence_index"),
        )
        periods.append(entry)

    dominant = str(decision.get("dominant_region_decision") or "").strip()
    status = str(decision.get("review_status") or "미검토")
    updated = dict(row)
    updated["indicator_scopes_json"] = (
        json.dumps(scopes, ensure_ascii=False) if scopes else ""
    )
    updated["source_regions_json"] = (
        json.dumps(regions, ensure_ascii=False) if regions else ""
    )
    updated["period_contexts_json"] = (
        json.dumps(periods, ensure_ascii=False) if periods else ""
    )
    updated["clause_value_boundaries_json"] = (
        json.dumps(boundaries, ensure_ascii=False) if boundaries else ""
    )
    updated["dominant_region_decision"] = dominant
    updated["reviewer_note"] = str(decision.get("reviewer_note") or "")
    updated["review_status"] = status
    updated["label_provenance"] = (
        "HUMAN_CONFIRMED" if status == "검토완료" else "UNREVIEWED"
    )
    return updated


__all__ = [
    "INHERITED",
    "INTRODUCED",
    "NON_REFERENCE_DOMINANCE",
    "NO_DOMINANT_REGION",
    "UNDECIDED",
    "SpanResolutionError",
    "allocate_id",
    "assemble_review_row",
    "existing_ids",
    "occurrence_index_for_offset",
    "region_choices",
    "scope_choices",
]
