"""Validate article-scoped IDs, references and span text in L2 review rows."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _json_array(row: dict[str, Any], field: str) -> list[dict[str, Any]]:
    raw = row.get(field)
    if raw in (None, ""):
        return []
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise ValueError(
            f"{row.get('sentence_review_id')} {field} must be a JSON array"
        )
    return value


def _check_id_format(article_idx: str, value: str, kind: str) -> None:
    suffix = "SC" if kind == "scope" else "R"
    if not re.fullmatch(rf"{re.escape(article_idx)}-{suffix}\d{{2,}}", value):
        raise ValueError(
            f"invalid {kind}_id for article {article_idx}: {value}"
        )


def validate_l2_review_ingest(
    human_rows: list[dict[str, Any]],
    context_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    scope_definitions: dict[str, set[str]] = defaultdict(set)
    region_definitions: dict[str, set[str]] = defaultdict(set)
    errors: list[str] = []

    def add_definition(
        article_idx: str,
        value: str,
        kind: str,
        owner: str,
    ) -> None:
        if not value:
            return
        try:
            _check_id_format(article_idx, value, kind)
        except ValueError as exc:
            errors.append(str(exc))
            return
        definitions = (
            scope_definitions if kind == "scope" else region_definitions
        )
        if value in definitions[article_idx]:
            errors.append(
                f"duplicate {kind}_id in article {article_idx}: "
                f"{value} ({owner})"
            )
            return
        definitions[article_idx].add(value)

    disagreement_count = 0
    for row in context_rows:
        article_idx = str(row.get("article_idx") or "")
        review_id = str(row.get("sentence_review_id") or "")
        if row.get("row_kind") == "자동확정":
            add_definition(
                article_idx,
                str(row.get("scope_id") or ""),
                "scope",
                review_id,
            )
            add_definition(
                article_idx,
                str(row.get("region_id") or ""),
                "region",
                review_id,
            )
            if str(row.get("disagree_flag") or "").strip():
                disagreement_count += 1
                if not str(row.get("reviewer_note") or "").strip():
                    errors.append(
                        f"{review_id} disagree_flag requires reviewer_note"
                    )

    inherited_scope_references: list[tuple[str, str, str]] = []
    dominant_region_references: list[tuple[str, str, str]] = []
    resolved_span_count = 0
    for row in human_rows:
        article_idx = str(row.get("article_idx") or "")
        review_id = str(row.get("sentence_review_id") or "")
        try:
            indicator_scopes = _json_array(row, "indicator_scopes_json")
            source_regions = _json_array(row, "source_regions_json")
            period_contexts = _json_array(row, "period_contexts_json")
            boundaries = _json_array(row, "clause_value_boundaries_json")
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            continue
        sentence_text = str(row.get("text") or "")
        for field, entries in (
            ("indicator_scopes_json", indicator_scopes),
            ("source_regions_json", source_regions),
            ("period_contexts_json", period_contexts),
        ):
            for entry in entries:
                if "source_char_start" in entry or "source_char_end" in entry:
                    errors.append(
                        f"{review_id} {field} must not carry hand-entered "
                        "offsets; offsets are derived from source_span_text"
                    )
                    continue
                if (
                    field == "indicator_scopes_json"
                    and entry.get("attribution_type") == "앞에서 상속"
                    and not str(entry.get("source_span_text") or "").strip()
                ):
                    # An inherited scope is introduced by an earlier sentence,
                    # so its evidence span lives on the defining row.
                    continue
                if entry.get("span_provenance") == "V3_INHERITED_NO_LOCAL_SPAN":
                    # Migrated from contract v3, where these entries were
                    # recorded without a local span.  The label is graded;
                    # the span is reported as missing rather than invented.
                    continue
                try:
                    resolve_span(
                        sentence_text,
                        entry.get("source_span_text"),
                        entry.get("occurrence_index"),
                    )
                except SpanResolutionError as exc:
                    errors.append(f"{review_id} {field}: {exc}")
                    continue
                resolved_span_count += 1
        offered_span_ids = set(
            parse_value_candidate_span_ids(row.get("value_candidate_span_ids"))
        )
        row_scope_ids = {
            str(scope.get("scope_id") or "")
            for scope in indicator_scopes
        }
        for boundary in boundaries:
            scope_id = str(boundary.get("scope_id") or "")
            if scope_id not in row_scope_ids:
                # A boundary that names a scope absent from its own row links
                # values to nothing.
                errors.append(
                    f"{review_id} boundary references a scope missing from "
                    f"this row: {scope_id or '(빈값)'}"
                )
            targets = boundary.get("target_value_span_ids") or []
            if isinstance(targets, str):
                targets = [
                    value.strip()
                    for value in targets.split("|")
                    if value.strip()
                ]
            if not targets:
                errors.append(
                    f"{review_id} clause_value_boundaries_json requires "
                    "target_value_span_ids"
                )
                continue
            unknown = [
                target for target in targets
                if target not in offered_span_ids
            ]
            if unknown:
                errors.append(
                    f"{review_id} unknown target_value_span_ids: "
                    f"{', '.join(unknown)}"
                )
            clause_text = boundary.get("clause_text")
            if clause_text in (None, ""):
                continue
            try:
                resolve_span(
                    sentence_text,
                    clause_text,
                    boundary.get("occurrence_index"),
                )
            except SpanResolutionError as exc:
                errors.append(
                    f"{review_id} clause_value_boundaries_json: {exc}"
                )
        for scope in indicator_scopes:
            scope_id = str(scope.get("scope_id") or "")
            if scope.get("attribution_type") == "앞에서 상속":
                inherited_scope_references.append(
                    (article_idx, scope_id, review_id)
                )
            else:
                add_definition(article_idx, scope_id, "scope", review_id)
        for region in source_regions:
            add_definition(
                article_idx,
                str(region.get("region_id") or ""),
                "region",
                review_id,
            )
        dominant = str(row.get("dominant_region_decision") or "").strip()
        if dominant and dominant not in {"지배 없음", "판단 불가"}:
            dominant_region_references.append(
                (article_idx, dominant, review_id)
            )

    for article_idx, scope_id, review_id in inherited_scope_references:
        if scope_id not in scope_definitions[article_idx]:
            errors.append(
                f"{review_id} unresolved inherited scope reference: "
                f"{scope_id}"
            )
    for article_idx, region_id, review_id in dominant_region_references:
        if region_id not in region_definitions[article_idx]:
            errors.append(
                f"{review_id} unresolved dominant region reference: "
                f"{region_id}"
            )

    if errors:
        raise ValueError("\n".join(errors))
    return {
        "status": "VALID",
        "contract_version": "l2_sentence_regions_v3",
        "human_rows": len(human_rows),
        "context_rows": len(context_rows),
        "scope_definition_count": sum(map(len, scope_definitions.values())),
        "region_definition_count": sum(map(len, region_definitions.values())),
        "resolved_span_count": resolved_span_count,
        "auto_context_disagreement_count": disagreement_count,
        "label_provenance_counts": dict(
            Counter(str(row.get("label_provenance") or "") for row in human_rows)
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--human-jsonl", type=Path, required=True)
    parser.add_argument("--context-jsonl", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_l2_review_ingest(
        _read_jsonl(args.human_jsonl),
        _read_jsonl(args.context_jsonl),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
