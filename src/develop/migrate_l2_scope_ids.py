"""Move ``scope_id`` ownership from the reviewer to the machine.

Contract v3 exposed ``scope_id`` and ``attribution_type`` as human input.  Two
failure modes followed: a scope borrowed from the inheritance dropdown could
reuse another sentence's ID, and several indicators inside one sentence could
share a single ID, which made the value-to-indicator link ambiguous.

This migration keeps every human judgement (indicator label, evidence span,
attached values, source region, dominance decision) and only rewrites the
identifiers, assigning one unique ID per scope entry.  Boundaries are re-keyed
positionally because they were emitted in scope order; a row whose counts do
not line up is reported instead of guessed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .l2_label_assembly import INHERITED, allocate_id
except ImportError:  # pragma: no cover - direct script execution
    from l2_label_assembly import INHERITED, allocate_id  # type: ignore


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _arr(row: dict[str, Any], field: str) -> list[dict[str, Any]]:
    raw = row.get(field)
    if not raw:
        return []
    return json.loads(raw) if isinstance(raw, str) else raw


def migrate_rows(
    rows: list[dict[str, Any]],
    context_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return rows with machine-owned scope IDs plus a migration report."""
    taken: dict[str, set[str]] = {}
    for row in context_rows:
        if row.get("row_kind") != "자동확정":
            continue
        article_idx = str(row.get("article_idx") or "")
        if row.get("scope_id"):
            taken.setdefault(article_idx, set()).add(str(row["scope_id"]))

    ambiguous: list[dict[str, Any]] = []
    label_only = 0
    reassigned = 0
    migrated: list[dict[str, Any]] = []
    for row in rows:
        article_idx = str(row.get("article_idx") or "")
        scopes = _arr(row, "indicator_scopes_json")
        bounds = _arr(row, "clause_value_boundaries_json")
        if not scopes:
            migrated.append(dict(row))
            continue

        pool = taken.setdefault(article_idx, set())
        new_scopes: list[dict[str, Any]] = []
        for scope in scopes:
            scope_id = allocate_id(article_idx, "scope", pool)
            pool.add(scope_id)
            if str(scope.get("scope_id") or "") != scope_id:
                reassigned += 1
            entry: dict[str, Any] = {
                "scope_id": scope_id,
                "indicator_label": scope.get("indicator_label") or "",
            }
            span = str(scope.get("source_span_text") or "").strip()
            if span:
                entry["source_span_text"] = span
                if scope.get("occurrence_index") is not None:
                    entry["occurrence_index"] = scope["occurrence_index"]
                entry["attribution_type"] = "이 문장에서 도입"
            else:
                # v3 "앞에서 상속" entries carried a label but no local span.
                # Every one of them named an indicator specific to its own
                # sentence rather than the scope it pointed at, so they are
                # this sentence's own scopes, not references.  The label is the
                # graded signal, so it is kept and marked rather than dropped
                # or back-filled with a guess.
                entry["attribution_type"] = "이 문장에서 도입"
                entry["span_provenance"] = "V3_INHERITED_NO_LOCAL_SPAN"
                label_only += 1
            new_scopes.append(entry)

        # Boundaries were appended in scope order, skipping scopes with no
        # values, so equal counts recover the original pairing exactly.
        if bounds and len(bounds) == len(new_scopes):
            new_bounds = []
            for scope, bound in zip(new_scopes, bounds):
                item = dict(bound)
                item["scope_id"] = scope["scope_id"]
                new_bounds.append(item)
        elif bounds:
            # Keeping the old identifiers would leave boundaries pointing at
            # IDs that no longer exist, which reads as a link but resolves to
            # nothing.  The values are preserved in the report and the row is
            # returned unlinked so a reviewer re-attaches them.
            new_bounds = []
            ambiguous.append({
                "sentence_review_id": row.get("sentence_review_id"),
                "scopes": len(new_scopes),
                "boundaries": len(bounds),
                "reason": "scope/boundary 개수 불일치로 위치 복원 불가",
                "indicator_labels": [
                    scope["indicator_label"] for scope in new_scopes
                ],
                "unlinked_value_span_ids": [
                    value
                    for bound in bounds
                    for value in bound.get("target_value_span_ids") or []
                ],
            })
        else:
            new_bounds = []

        updated = dict(row)
        updated["indicator_scopes_json"] = json.dumps(
            new_scopes, ensure_ascii=False
        )
        updated["clause_value_boundaries_json"] = (
            json.dumps(new_bounds, ensure_ascii=False) if new_bounds else ""
        )
        if bounds and not new_bounds:
            # The row lost its value links, so it is no longer confirmed.
            updated["review_status"] = "미검토"
            updated["label_provenance"] = "UNREVIEWED"
        migrated.append(updated)

    duplicates = 0
    for row in migrated:
        ids = [scope["scope_id"] for scope in _arr(row, "indicator_scopes_json")]
        duplicates += len(ids) - len(set(ids))

    report = {
        "contract_version": "l2_sentence_regions_v3_machine_scope_ids",
        "rows": len(migrated),
        "scope_ids_reassigned": reassigned,
        "label_only_scopes": label_only,
        "remaining_duplicate_scope_ids": duplicates,
        "ambiguous_rows": ambiguous,
        "human_fields_preserved": [
            "indicator_label",
            "source_span_text",
            "target_value_span_ids",
            "source_regions_json",
            "dominant_region_decision",
            "reviewer_note",
        ],
    }
    return migrated, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--human-jsonl", type=Path, required=True)
    parser.add_argument("--context-jsonl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    migrated, report = migrate_rows(
        _read_jsonl(args.human_jsonl),
        _read_jsonl(args.context_jsonl),
    )
    _write_jsonl(args.output, migrated)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
