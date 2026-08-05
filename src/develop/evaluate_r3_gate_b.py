"""Build R3 human field gold and score Gate B only after adjudication is complete.

The full sentence sheet is intentionally not a scoreable artifact while any
sentence or KOSIS value remains unreviewed.  This module closes that safety
gap: it validates the article-scoped L2 contract, reconstructs value-level
indicator gold from the human scope/boundary decisions, then reports routing
and three-field metrics with explicit numerators and denominators.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .evaluate_six_fields import evaluate as evaluate_fields
from .l5_routing import evaluate_routing
from .validate_l2_review_ingest import validate_l2_review_ingest


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _array(row: dict[str, Any], field: str) -> list[dict[str, Any]]:
    raw = row.get(field)
    if not raw:
        return []
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list):
        raise ValueError(f"{row.get('sentence_review_id')} {field} is not an array")
    return value


def build_value_gold(
    human_rows: list[dict[str, Any]],
    context_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    pending = [
        row for row in human_rows if row.get("review_status") != "검토완료"
    ]
    if pending:
        raise ValueError(
            f"R3_NOT_READY: human sentence review {len(human_rows) - len(pending)}/"
            f"{len(human_rows)}; {len(pending)} rows remain"
        )
    validation = validate_l2_review_ingest(human_rows, context_rows)
    if validation["kosis_field_gold_confirmed"] != validation["kosis_field_gold_total"]:
        raise ValueError(
            "R3_NOT_READY: KOSIS field gold "
            f"{validation['kosis_field_gold_confirmed']}/"
            f"{validation['kosis_field_gold_total']}"
        )

    field_gold: list[dict[str, Any]] = []
    routing_gold: list[dict[str, Any]] = []
    for row in human_rows:
        scopes = {
            str(item.get("scope_id") or ""): item
            for item in _array(row, "indicator_scopes_json")
        }
        indicator_by_value: dict[str, str] = {}
        for boundary in _array(row, "clause_value_boundaries_json"):
            label = str(
                (scopes.get(str(boundary.get("scope_id") or "")) or {}).get(
                    "indicator_label"
                )
                or ""
            ).strip()
            for span_id in boundary.get("target_value_span_ids") or []:
                span_id = str(span_id)
                if span_id in indicator_by_value:
                    raise ValueError(
                        f"{row.get('sentence_review_id')} value linked twice"
                    )
                indicator_by_value[span_id] = label
        fields = {
            str(item.get("value_span_id") or ""): item
            for item in _array(row, "value_field_gold_json")
        }
        for routing in _array(row, "routing_gold_by_value_json"):
            span_id = str(routing.get("value_span_id") or "")
            judged_class = str(routing.get("judged_class") or "")
            routing_gold.append({
                "article_idx": str(row.get("article_idx") or ""),
                "value_span_id": span_id,
                "judged_class": judged_class,
            })
            field = fields.get(span_id) or {}
            is_kosis = judged_class == "KOSIS_CANDIDATE"
            field_gold.append({
                "article_idx": str(row.get("article_idx") or ""),
                "sentence_id": row.get("sentence_id"),
                "원문 문장": row.get("text") or "",
                "target value": routing.get("value_text") or "",
                "candidate span ID": span_id,
                "claim 여부": "YES" if is_kosis else "NO",
                "검증대상 gold": judged_class,
                "indicator gold": indicator_by_value.get(span_id, "없음"),
                "measurement gold": field.get("measurement_gold") or "",
                "period gold": field.get("period_gold") or "",
                "population gold": "없음",
                "item gold": "없음",
                "dimension gold": "없음",
            })
    return field_gold, routing_gold, validation


def evaluate_gate_b(
    human_rows: list[dict[str, Any]],
    context_rows: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    holdout_routing_f1: float | None = None,
) -> dict[str, Any]:
    field_gold, routing_gold, validation = build_value_gold(
        human_rows, context_rows
    )
    routing = evaluate_routing(routing_gold, predictions)
    fields = evaluate_fields(field_gold, predictions)
    dev_f1 = routing["f1"]
    gap = (
        abs(dev_f1 - holdout_routing_f1)
        if holdout_routing_f1 is not None
        else None
    )
    conditions = {
        "routing_f1_at_least_0_65": dev_f1 >= 0.65,
        "dev_holdout_gap_at_most_0_15": gap is not None and gap <= 0.15,
        "joint_three_relaxed_at_least_0_55": fields["joint_three_relaxed"] >= 0.55,
    }
    return {
        "artifact_status": "HUMAN_GOLD_COMPLETE",
        "validation": validation,
        "routing": routing,
        "three_field": {
            "scored": fields["scored"],
            "joint_exact": fields["joint_three_exact"],
            "joint_relaxed": fields["joint_three_relaxed"],
            "per_field": {
                name: fields["per_field"][name]
                for name in ("indicator", "measurement", "period")
            },
            "blocking_combination": fields["three_field_blocking_combination"],
        },
        "holdout_routing_f1": holdout_routing_f1,
        "dev_holdout_absolute_gap": gap,
        "gate_b_conditions": conditions,
        "gate_b_pass": all(conditions.values()),
        "note": (
            "A missing holdout routing F1 is a failed/unmeasured condition, not a pass."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--human-jsonl", type=Path, required=True)
    parser.add_argument("--context-jsonl", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--holdout-routing-f1", type=float)
    parser.add_argument("--field-gold-output", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    human = read_jsonl(args.human_jsonl)
    context = read_jsonl(args.context_jsonl)
    predictions = read_jsonl(args.predictions)
    field_gold, _, _ = build_value_gold(human, context)
    result = evaluate_gate_b(
        human,
        context,
        predictions,
        holdout_routing_f1=args.holdout_routing_f1,
    )
    if args.field_gold_output:
        args.field_gold_output.parent.mkdir(parents=True, exist_ok=True)
        args.field_gold_output.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in field_gold),
            encoding="utf-8",
        )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
