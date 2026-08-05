"""Reproduce the preregistered R2 holdout comparison from frozen artefacts.

The changed routed file already contains the one-shot L2→L5 output.  The
baseline differs at exactly one registered reason, so it is reconstructed by
removing only ``VALUE_REPEATED_INSIDE_INDICATOR`` decisions.  No model call,
threshold tuning, or gold mutation happens here.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from .l5_routing import (
    DEFAULT_THRESHOLD,
    KOSIS_CANDIDATE,
    NOT_CLAIM,
    OFFICIAL_SUBTYPE,
    evaluate_routing,
)

REGISTERED_REASON = "VALUE_REPEATED_INSIDE_INDICATOR"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def baseline_without_registered_block(
    routed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Undo only the registered hard block, preserving every other L5 input."""
    baseline = deepcopy(routed)
    for row in baseline:
        if row.get("reason") != REGISTERED_REASON:
            continue
        if str(row.get("source_subtype") or "").strip() == OFFICIAL_SUBTYPE:
            row.update({
                "routing_class": KOSIS_CANDIDATE,
                "confidence": 1.0,
                "reason": "OFFICIAL_AGGREGATE_REGION",
            })
        elif str(row.get("indicator_label") or "").strip():
            confidence = 0.5
            row.update({
                "routing_class": (
                    KOSIS_CANDIDATE
                    if confidence >= float(row.get("threshold", DEFAULT_THRESHOLD))
                    else NOT_CLAIM
                ),
                "confidence": confidence,
                "reason": "INDICATOR_WITHOUT_RESOLVED_REGION",
            })
        else:
            row.update({
                "routing_class": NOT_CLAIM,
                "confidence": 1.0,
                "reason": "NO_INDICATOR_NO_REGION",
            })
    return baseline


def compare(
    gold: list[dict[str, Any]],
    changed: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = baseline_without_registered_block(changed)
    gold_by_key = {
        (str(row.get("article_idx")), str(row.get("value_span_id"))):
        str(row.get("judged_class") or "")
        for row in gold
    }
    new_blocks = [row for row in changed if row.get("reason") == REGISTERED_REASON]
    block_gold = Counter(
        gold_by_key.get(
            (str(row.get("article_idx")), str(row.get("value_span_id"))),
            "MISSING_GOLD",
        )
        for row in new_blocks
    )
    correct = sum(cls not in {KOSIS_CANDIDATE, "MISSING_GOLD"} for cls in block_gold.elements())
    loss = block_gold.get(KOSIS_CANDIDATE, 0)
    return {
        "contract_version": "r2-l5-holdout2-comparison-v1",
        "registered_reason": REGISTERED_REASON,
        "baseline": evaluate_routing(gold, baseline),
        "changed": evaluate_routing(gold, changed),
        "new_block_total_n": len(new_blocks),
        "new_block_correct_n": correct,
        "kosis_loss_n": loss,
        "new_block_gold_classes": dict(block_gold),
        "adoption": (
            "REJECT_HARD_BLOCK"
            if loss
            else "PROVISIONAL_SMALL_N"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--changed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(read_jsonl(args.gold), read_jsonl(args.changed))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
