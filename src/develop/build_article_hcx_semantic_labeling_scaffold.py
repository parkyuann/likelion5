"""Build a non-authoritative semantic-role labeling scaffold from source candidates.

The existing adjudicated gold rows remain unchanged.  This module only attaches
source candidate suggestions and explicit review states so population/item
labels are never silently invented by code.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .article_claim_pipeline import (
    _evidence_supports_anchor,
    _indicator_anchor_terms,
    build_claim_skeleton_candidate_catalog,
)
from .article_hcx_gold_fixture import load_jsonl, load_saved_articles


def _best_indicator_candidate(anchor: str, candidates: list[dict[str, Any]],
                              value_sentence_id: object) -> dict[str, Any] | None:
    matches = [
        candidate for candidate in candidates
        if _evidence_supports_anchor(str(candidate.get("text") or ""), anchor)
    ]
    if not matches:
        return None
    anchor_compact = "".join(character.casefold() for character in anchor if character.isalnum())

    def compact_source(value: object) -> str:
        text = str(value or "")
        while "(" in text and ")" in text and text.index("(") < text.index(")"):
            start, end = text.index("("), text.index(")")
            text = text[:start] + text[end + 1:]
        return "".join(character.casefold() for character in text if character.isalnum())

    return min(
        matches,
        key=lambda candidate: (
            compact_source(candidate.get("text")) != anchor_compact,
            candidate.get("sentence_candidate_id") != f"sentence:s{value_sentence_id:04d}"
            if isinstance(value_sentence_id, int) else True,
            len(str(candidate.get("text") or "")),
            str(candidate.get("semantic_evidence_candidate_id") or ""),
        ),
    )


def build_semantic_labeling_scaffold(gold_rows: list[dict[str, Any]],
                                     articles: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach candidate suggestions without promoting them to human gold."""
    output_rows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    catalogs = {
        article_idx: build_claim_skeleton_candidate_catalog(
            article["article_text"],
            include_semantic_evidence=True,
        )
        for article_idx, article in articles.items()
    }
    for gold in gold_rows:
        article_idx = str(gold.get("article_idx") or "")
        catalog = catalogs.get(article_idx, {
            "value_candidates": [], "semantic_evidence_candidates": [], "dimension_candidates": [],
        })
        value_matches = [
            candidate for candidate in catalog["value_candidates"]
            if candidate.get("text") == gold.get("value_text")
            and candidate.get("sentence_candidate_id") == (
                f"sentence:s{gold.get('value_sentence_id'):04d}"
                if isinstance(gold.get("value_sentence_id"), int) else None
            )
        ]
        anchors = sorted(_indicator_anchor_terms(gold.get("indicator_norm")))
        indicator_matches = {
            anchor: _best_indicator_candidate(
                anchor,
                catalog["semantic_evidence_candidates"],
                gold.get("value_sentence_id"),
            )
            for anchor in anchors
        }
        covered_anchors = sorted(anchor for anchor, match in indicator_matches.items() if match)
        uncovered_anchors = sorted(set(anchors) - set(covered_anchors))
        indicator_ids = list(dict.fromkeys(
            match["semantic_evidence_candidate_id"]
            for match in indicator_matches.values()
            if match
        ))
        dimension_texts = gold.get("dimension_texts", [])
        dimension_texts = dimension_texts if isinstance(dimension_texts, list) else []
        dimension_ids = [
            candidate["dimension_candidate_id"]
            for candidate in catalog["dimension_candidates"]
            if candidate.get("text") in dimension_texts
            and candidate.get("sentence_candidate_id") == (
                f"sentence:s{gold.get('value_sentence_id'):04d}"
                if isinstance(gold.get("value_sentence_id"), int) else None
            )
        ]
        role_draft = {
            "adjudication_status": "DRAFT_NEEDS_HUMAN_ADJUDICATION",
            "target_value_candidate_ids_draft": [
                candidate["value_candidate_id"] for candidate in value_matches
            ],
            "indicator_evidence_candidate_ids_draft": indicator_ids,
            "population_evidence_candidate_ids_draft": [],
            "item_evidence_candidate_ids_draft": [],
            "dimension_candidate_ids_draft": dimension_ids,
            "indicator_anchor_terms": anchors,
            "covered_indicator_anchor_terms": covered_anchors,
            "uncovered_indicator_anchor_terms": uncovered_anchors,
            "field_status": {
                "indicator": (
                    "DRAFT_CANDIDATE_COMPLETE"
                    if anchors and not uncovered_anchors else "DRAFT_CANDIDATE_PARTIAL"
                ),
                "population": "NEEDS_HUMAN_LABEL",
                "item": "NEEDS_HUMAN_LABEL",
                "dimension": (
                    "DRAFT_FROM_EXISTING_GOLD"
                    if dimension_texts and len(dimension_ids) == len(dimension_texts)
                    else "NEEDS_HUMAN_LABEL"
                ),
            },
        }
        output_rows.append({**gold, "semantic_role_labeling": role_draft})
        reports.append({
            "fixture_id": gold.get("fixture_id"),
            "article_idx": article_idx,
            "eligibility": gold.get("eligibility"),
            "target_value_candidate_match": len(value_matches) == 1,
            "indicator_anchor_count": len(anchors),
            "covered_indicator_anchor_count": len(covered_anchors),
            "indicator_candidate_complete": bool(anchors) and not uncovered_anchors,
            "dimension_gold_count": len(dimension_texts),
            "dimension_candidate_match_count": len(dimension_ids),
            "adjudication_status": role_draft["adjudication_status"],
        })
    kosis_reports = [row for row in reports if row["eligibility"] == "KOSIS_CANDIDATE"]
    return output_rows, {
        "artifact_status": "DRAFT_NEEDS_HUMAN_ADJUDICATION",
        "gold_rows_preserved": len(output_rows),
        "kosis_candidate_rows": len(kosis_reports),
        "target_value_candidate_exact_rows": sum(row["target_value_candidate_match"] for row in kosis_reports),
        "indicator_candidate_complete_rows": sum(row["indicator_candidate_complete"] for row in kosis_reports),
        "indicator_anchor_count": sum(row["indicator_anchor_count"] for row in kosis_reports),
        "covered_indicator_anchor_count": sum(row["covered_indicator_anchor_count"] for row in kosis_reports),
        "dimension_gold_count": sum(row["dimension_gold_count"] for row in kosis_reports),
        "dimension_candidate_match_count": sum(row["dimension_candidate_match_count"] for row in kosis_reports),
        "population_labels_adjudicated": 0,
        "item_labels_adjudicated": 0,
        "reports": reports,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--saved-run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    rows, report = build_semantic_labeling_scaffold(
        load_jsonl(args.gold),
        load_saved_articles(args.saved_run_root),
    )
    write_jsonl(args.output, rows)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "reports"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
