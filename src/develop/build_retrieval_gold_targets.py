"""Assemble the human adjudication targets for retrieval gold.

CLAUDE.md P0: without adjudicated gold a candidate agreement rate is not
recall.  This builds what a person actually adjudicates — a routed value, the
fields the structuring layers produced for it, and Top-K tables to choose from
— because 265,096 tables cannot be searched by hand.

Two rules keep the resulting gold honest.

* Only values the **human** routing gold marked ``KOSIS_CANDIDATE`` become
  targets.  Adjudicating the model's own routing decisions would make the
  retrieval gold inherit the routing errors it is supposed to be independent
  of.
* Targets are deduplicated by (indicator, period) and sampled per article, so
  one dense article cannot dominate the way 2703 dominated the earlier
  evaluation set.

The candidate list is lexical only and is explicitly a starting point for the
person, not a proposed answer: ``없음`` has to stay reachable, so the tool that
consumes this file must let the adjudicator reject every candidate.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from .kosis_lexical_search import LexicalIndex
    from .retrieval_query import build_query_variants
except ImportError:  # pragma: no cover - direct script execution
    from kosis_lexical_search import LexicalIndex
    from retrieval_query import build_query_variants

HUMAN_KOSIS = "KOSIS_CANDIDATE"
CANDIDATE_K = 10


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def select_targets(
    routed: list[dict[str, Any]],
    gold: list[dict[str, Any]],
    *,
    per_article: int | None = None,
) -> list[dict[str, Any]]:
    """Pick the values a person should look for a table for."""
    wanted = {
        (str(row.get("article_idx")), str(row.get("value_span_id")))
        for row in gold
        if str(row.get("judged_class") or "") == HUMAN_KOSIS
    }
    seen: set[tuple[str, str]] = set()
    by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in routed:
        key = (str(row.get("article_idx")), str(row.get("value_span_id")))
        if key not in wanted:
            continue
        fields = row.get("retrieval_fields") or {}
        indicator = str(fields.get("indicator") or "").strip()
        if not indicator:
            continue
        # The same indicator over the same period retrieves the same table, so
        # adjudicating it twice buys nothing and inflates the apparent sample.
        dedupe = (indicator, str(fields.get("period") or ""))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        by_article[str(row.get("article_idx"))].append({
            "target_id": f"{row.get('article_idx')}:{row.get('value_span_id')}",
            "article_idx": str(row.get("article_idx")),
            "value_span_id": row.get("value_span_id"),
            "value_text": row.get("value_text"),
            "value_unit": row.get("value_unit"),
            "sentence_text": row.get("sentence_text"),
            "indicator": indicator,
            "measurement_type": fields.get("measurement_type"),
            "period": fields.get("period"),
            "period_absolute": fields.get("period_absolute"),
            "population": fields.get("population") or [],
            "item": fields.get("item") or [],
            "dimension": fields.get("dimension") or [],
            "source_subtype": row.get("source_subtype"),
        })
    targets: list[dict[str, Any]] = []
    for article_idx in sorted(by_article):
        rows = by_article[article_idx]
        targets.extend(rows if per_article is None else rows[:per_article])
    return targets


def attach_candidates(
    targets: list[dict[str, Any]],
    index: LexicalIndex,
    *,
    k: int = CANDIDATE_K,
) -> list[dict[str, Any]]:
    """Add Top-K lexical candidates and the empty adjudication columns."""
    out = []
    for target in targets:
        # Queries are run separately and merged, never concatenated: folding
        # item and population into the indicator lowered recall from 0.333 to
        # 0.303 on the 2026-08-03 adjudication.
        merged: dict[str, dict[str, Any]] = {}
        for variant in build_query_variants(target, target.get("sentence_text")):
            for hit in index.search(variant["query"], k=k):
                current = merged.get(hit["tbl_name"])
                if current is None or hit["score"] > current["score"]:
                    merged[hit["tbl_name"]] = {**hit, "found_by": variant["role"]}
        hits = sorted(merged.values(), key=lambda item: -item["score"])[:k]
        out.append({
            **target,
            "candidates": [
                {
                    "rank": rank,
                    "table_key": hit["table_key"],
                    "tbl_name": hit["tbl_name"],
                    "category_paths": hit.get("category_paths") or [],
                    "score": round(hit["score"], 3),
                    "profile_present": hit.get("profile_present", False),
                    "found_by": hit.get("found_by", ""),
                    "duplicate_tables": hit.get("duplicate_tables", 1),
                }
                for rank, hit in enumerate(hits, start=1)
            ],
            # Human fields, empty on purpose.
            "gold_match_status": "",
            "gold_table_key": "",
            "gold_tbl_name": "",
            "gold_from_candidate_rank": "",
            "adjudication_note": "",
            "review_status": "미검토",
        })
    return out


def summarise(targets: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    return {
        "targets": len(targets),
        "articles": len(({row["article_idx"] for row in targets})),
        "per_article": dict(
            Counter(row["article_idx"] for row in targets)
        ),
        "targets_without_candidates": sum(
            1 for row in targets if not row.get("candidates")
        ),
        "candidate_k": CANDIDATE_K,
        # Recorded so nobody later reads a candidate hit rate as recall.
        "contains_model_output": False,
        "candidate_source": "lexical_bigram_only",
        "gold_status": "unadjudicated",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routed", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--per-article", type=int)
    args = parser.parse_args()

    targets = select_targets(
        read_jsonl(args.routed),
        read_jsonl(args.gold),
        per_article=args.per_article,
    )
    rows = attach_candidates(targets, LexicalIndex.load(args.index))
    write_jsonl(args.output, rows)
    summary = summarise(rows)
    if args.summary:
        args.summary.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
