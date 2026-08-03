"""Score L3 on the value-to-indicator links the reviewer actually authored.

L-GOLD records which indicator each value belongs to (76 links).  That is the
only thing this layer is responsible for, so it is scored directly instead of
being inferred from end-to-end routing, which mixes in every other layer.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .evaluate_l2_segmentation import label_similarity
except ImportError:  # pragma: no cover - direct script execution
    from evaluate_l2_segmentation import label_similarity


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _arr(row: dict[str, Any], field: str) -> list[dict[str, Any]]:
    raw = row.get(field)
    if not raw:
        return []
    value = json.loads(raw) if isinstance(raw, str) else raw
    return [item for item in value if isinstance(item, dict)]


def build_value_gold(
    human_rows: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """Return ``(article_idx, value_span_id) -> indicator label``."""
    gold: dict[tuple[str, str], str] = {}
    for row in human_rows:
        article_idx = str(row.get("article_idx"))
        labels = {
            str(scope.get("scope_id")): str(scope.get("indicator_label") or "")
            for scope in _arr(row, "indicator_scopes_json")
        }
        for boundary in _arr(row, "clause_value_boundaries_json"):
            label = labels.get(str(boundary.get("scope_id")), "")
            if not label:
                continue
            for span_id in boundary.get("target_value_span_ids") or []:
                gold[(article_idx, str(span_id))] = label
    return gold


def evaluate(
    gold: dict[tuple[str, str], str],
    assignments: list[dict[str, Any]],
    *,
    threshold: float = 0.5,
) -> dict[str, Any]:
    predicted = {
        (str(row.get("article_idx")), str(row.get("value_span_id"))): row
        for row in assignments
    }
    correct = wrong = missing = 0
    by_pairing: dict[str, Counter] = {}
    by_source: dict[str, Counter] = {}
    examples: list[dict[str, Any]] = []

    for key, gold_label in gold.items():
        row = predicted.get(key)
        if row is None or not row.get("indicator_label"):
            missing += 1
            continue
        score = label_similarity(gold_label, row["indicator_label"])
        hit = score >= threshold
        correct += hit
        wrong += not hit
        pairing = str(row.get("indicator_pairing") or "NONE")
        by_pairing.setdefault(pairing, Counter())[
            "correct" if hit else "wrong"
        ] += 1
        source = str(row.get("indicator_source") or "NONE")
        by_source.setdefault(source, Counter())[
            "correct" if hit else "wrong"
        ] += 1
        if not hit and len(examples) < 15:
            examples.append({
                "article_idx": key[0],
                "value_span_id": key[1],
                "value_text": row.get("value_text"),
                "gold": gold_label,
                "predicted": row["indicator_label"],
            })

    scored = correct + wrong
    return {
        "gold_links": len(gold),
        "scored": scored,
        "value_not_assigned": missing,
        "correct": correct,
        "wrong": wrong,
        "value_indicator_accuracy": correct / scored if scored else 0.0,
        "coverage": scored / len(gold) if gold else 0.0,
        "by_pairing": {key: dict(value) for key, value in by_pairing.items()},
        "by_indicator_source": {
            key: dict(value) for key, value in by_source.items()
        },
        "wrong_examples": examples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold-human", type=Path, required=True)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(
        build_value_gold(_read_jsonl(args.gold_human)),
        _read_jsonl(args.assignments),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
