"""Flatten the L2 review gold into one comparable record per sentence.

Gold is authored per review row plus a context panel of automatically
confirmed sentences.  A layer evaluator needs a single view keyed by sentence
so model output can be compared without knowing which pass produced a label.

Indicator scopes are compared by source span rather than by label text: the
labels are free-form Korean written by a reviewer, so string equality would
measure phrasing rather than segmentation.  Spans are objective and already
resolved deterministically by ``l2_span_resolver``.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from .l2_span_resolver import SpanResolutionError, resolve_span
except ImportError:  # pragma: no cover - direct script execution
    from l2_span_resolver import SpanResolutionError, resolve_span  # type: ignore


NO_DOMINANT_REGION = "지배 없음"
UNDECIDED = "판단 불가"
CONTRADICTED = "모순"

# A sentence that names its own source cannot also be governed by no source.
# Such rows are excluded from the source-region denominator and reported,
# rather than silently grading a model against a label the sentence refutes.
_EXPLICIT_ATTRIBUTION_RE = __import__("re").compile(
    r"(?:에\s*따르면|가\s*발표한|이\s*발표한|가\s*집계한|조사한\s*결과|"
    r"자료에\s*따르면)"
)


def _arr(row: dict[str, Any], field: str) -> list[dict[str, Any]]:
    raw = row.get(field)
    if not raw:
        return []
    value = json.loads(raw) if isinstance(raw, str) else raw
    return [item for item in value if isinstance(item, dict)]


def _span_range(text: str, entry: dict[str, Any]) -> tuple[int, int] | None:
    span_text = str(entry.get("source_span_text") or "").strip()
    if not span_text:
        return None
    try:
        resolved = resolve_span(text, span_text, entry.get("occurrence_index"))
    except SpanResolutionError:
        return None
    return resolved["source_char_start"], resolved["source_char_end"]


def build_gold_view(
    human_rows: list[dict[str, Any]],
    context_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one gold record per sentence across every article."""
    human_by_id = {
        str(row.get("sentence_review_id")): row for row in human_rows
    }
    # region_id -> subtype, so an inherited dominance decision resolves to the
    # subtype the reviewer actually meant.
    subtype_by_region: dict[tuple[str, str], str] = {}
    for row in context_rows:
        if row.get("row_kind") == "자동확정" and row.get("region_id"):
            subtype_by_region[
                (str(row.get("article_idx")), str(row["region_id"]))
            ] = str(row.get("source_subtype") or "")
    for row in human_rows:
        article_idx = str(row.get("article_idx"))
        for region in _arr(row, "source_regions_json"):
            subtype_by_region[(article_idx, str(region.get("region_id")))] = str(
                region.get("source_subtype") or ""
            )

    records: list[dict[str, Any]] = []
    for context in context_rows:
        review_id = str(context.get("sentence_review_id"))
        article_idx = str(context.get("article_idx"))
        text = str(context.get("text") or "")
        human = human_by_id.get(review_id)
        if human is None:
            # Automatically confirmed sentence: a single explicit indicator.
            records.append({
                "sentence_review_id": review_id,
                "article_idx": article_idx,
                "sentence_id": context.get("sentence_id"),
                "text": text,
                "row_kind": "자동확정",
                "review_reason": None,
                "indicator_spans": [],
                "indicator_labels": [
                    label for label in
                    str(context.get("indicator_label") or "").split(" | ")
                    if label
                ],
                "indicator_scope_count": len([
                    label for label in
                    str(context.get("indicator_label") or "").split(" | ")
                    if label
                ]),
                "source_subtype": str(context.get("source_subtype") or ""),
                "defines_region": bool(context.get("region_id")),
                "dominant_region_decision": None,
                "dominance_class": "정의",
            })
            continue

        scopes = _arr(human, "indicator_scopes_json")
        spans = []
        for scope in scopes:
            span = _span_range(text, scope)
            if span is not None:
                spans.append(span)
        regions = _arr(human, "source_regions_json")
        dominant = str(human.get("dominant_region_decision") or "").strip()
        if regions:
            subtype = str(regions[0].get("source_subtype") or "")
            dominance_class = "정의"
        elif dominant == NO_DOMINANT_REGION:
            subtype = ""
            dominance_class = "지배 없음"
        elif dominant == UNDECIDED:
            subtype = ""
            dominance_class = "판단 불가"
        elif dominant:
            subtype = subtype_by_region.get((article_idx, dominant), "")
            dominance_class = "상속"
        else:
            subtype = ""
            dominance_class = "미판정"
        if (
            dominance_class == NO_DOMINANT_REGION
            and _EXPLICIT_ATTRIBUTION_RE.search(text)
        ):
            dominance_class = CONTRADICTED
        records.append({
            "sentence_review_id": review_id,
            "article_idx": article_idx,
            "sentence_id": context.get("sentence_id"),
            "text": text,
            "row_kind": "검토대상",
            "review_reason": human.get("review_reason"),
            "indicator_spans": spans,
            "indicator_labels": [
                str(scope.get("indicator_label") or "") for scope in scopes
            ],
            "indicator_scope_count": len(scopes),
            "source_subtype": subtype,
            "defines_region": bool(regions),
            "dominant_region_decision": dominant or None,
            "dominance_class": dominance_class,
        })
    records.sort(
        key=lambda row: (int(row["article_idx"]), int(row["sentence_id"] or 0))
    )
    return records


def gold_view_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    return {
        "sentences": len(records),
        "articles": len({row["article_idx"] for row in records}),
        "row_kind": dict(Counter(row["row_kind"] for row in records)),
        "dominance_class": dict(
            Counter(row["dominance_class"] for row in records)
        ),
        "source_subtype": dict(
            Counter(row["source_subtype"] or "(없음)" for row in records)
        ),
        "sentences_with_indicator": sum(
            1 for row in records if row["indicator_scope_count"]
        ),
        "multi_indicator_sentences": sum(
            1 for row in records if row["indicator_scope_count"] > 1
        ),
        "indicator_spans_resolved": sum(
            len(row["indicator_spans"]) for row in records
        ),
    }
