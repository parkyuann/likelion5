"""Evaluate HCX population/item/dimension selections against confirmed gold."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .article_hcx_gold_fixture import load_jsonl


ROLES = ("population", "item", "dimension")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _gold_ids(row: dict[str, Any], role: str) -> set[str]:
    gold = row.get("semantic_role_gold")
    gold = gold if isinstance(gold, dict) else {}
    return {
        str(value)
        for value in gold.get(f"{role}_evidence_candidate_ids", [])
        if value
    } if role != "dimension" else {
        str(value)
        for value in gold.get("dimension_candidate_ids", [])
        if value
    }


def _target_ids(row: dict[str, Any]) -> set[str]:
    gold = row.get("semantic_role_gold")
    gold = gold if isinstance(gold, dict) else {}
    return {
        str(value)
        for value in gold.get("target_value_candidate_ids", [])
        if value
    }


def _metric_block(rows: list[dict[str, Any]], *, covered_only: bool) -> dict[str, Any]:
    selected = [row for row in rows if row["covered"]] if covered_only else rows
    metrics: dict[str, Any] = {}
    for role in ROLES:
        tp = fp = fn = 0
        exact_rows = 0
        for row in selected:
            gold = set(row["gold_ids"][role])
            predicted = set(row["predicted_ids"][role])
            tp += len(gold & predicted)
            fp += len(predicted - gold)
            fn += len(gold - predicted)
            exact_rows += gold == predicted
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else None
        )
        metrics[role] = {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "exact_rows": exact_rows,
            "evaluated_rows": len(selected),
            "exact_row_accuracy": exact_rows / len(selected) if selected else None,
        }
    all_role_exact_rows = sum(
        all(
            set(row["gold_ids"][role]) == set(row["predicted_ids"][role])
            for role in ROLES
        )
        for row in selected
    )
    return {
        "evaluated_rows": len(selected),
        "all_role_exact_rows": all_role_exact_rows,
        "all_role_exact_accuracy": (
            all_role_exact_rows / len(selected) if selected else None
        ),
        "roles": metrics,
    }


def _evaluate_stage_rows(
    gold_rows: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for gold in gold_rows:
        fixture_id = str(gold.get("fixture_id") or "")
        prediction = predictions.get(fixture_id, {})
        rows.append({
            "fixture_id": fixture_id,
            "article_idx": str(gold.get("article_idx") or ""),
            "value_text": str(gold.get("value_text") or ""),
            "value_sentence_id": gold.get("value_sentence_id"),
            "covered": bool(prediction.get("covered")),
            "gold_ids": {
                role: sorted(_gold_ids(gold, role))
                for role in ROLES
            },
            "predicted_ids": {
                role: sorted(set(prediction.get(role, [])))
                for role in ROLES
            },
        })
    return rows


def evaluate_semantic_roles(
    run_root: Path,
    fixture_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible = [
        row
        for row in fixture_rows
        if row.get("eligibility") == "KOSIS_CANDIDATE"
        and row.get("semantic_role_gold", {}).get("adjudication_status") == "CONFIRMED"
    ]
    candidate_rows = _read_jsonl(run_root / "span_candidates.jsonl")
    binding_rows = _read_jsonl(run_root / "bindings.jsonl")
    passed_rows = _read_jsonl(run_root / "pass_observations.jsonl")

    binding_by_key = {
        (str(row.get("article_idx") or ""), row.get("claim_index")): row.get("binding", {})
        for row in binding_rows
    }
    raw_predictions: dict[str, dict[str, Any]] = {}
    validated_predictions: dict[str, dict[str, Any]] = {}
    for gold in eligible:
        fixture_id = str(gold.get("fixture_id") or "")
        article_idx = str(gold.get("article_idx") or "")
        target_ids = _target_ids(gold)

        raw_matches = [
            row for row in candidate_rows
            if str(row.get("article_idx") or "") == article_idx
            and target_ids.intersection(
                str(value)
                for value in row.get("candidate_filter", {}).get(
                    "target_value_span_ids", []
                )
            )
        ]
        raw_prediction: dict[str, Any] = {
            "covered": bool(raw_matches),
            "population": [],
            "item": [],
            "dimension": [],
        }
        for match in raw_matches:
            binding = binding_by_key.get(
                (article_idx, match.get("claim_index")),
                {},
            )
            raw_prediction["population"].extend(
                str(value)
                for value in binding.get("population_evidence_span_ids", [])
            )
            raw_prediction["item"].extend(
                str(value)
                for value in binding.get("item_evidence_span_ids", [])
            )
            for observation in binding.get("observations", []):
                if str(observation.get("value_span_id") or "") in target_ids:
                    raw_prediction["dimension"].extend(
                        str(value)
                        for value in observation.get("dimension_span_ids", [])
                    )
        raw_predictions[fixture_id] = raw_prediction

        validated_matches = [
            row for row in passed_rows
            if str(row.get("article_idx") or "") == article_idx
            and str(
                row.get("validation", {}).get("value_span", {}).get("span_id") or ""
            ) in target_ids
        ]
        validated_prediction: dict[str, Any] = {
            "covered": bool(validated_matches),
            "population": [],
            "item": [],
            "dimension": [],
        }
        for match in validated_matches:
            evidence = match.get("semantic_role_evidence", {})
            validated_prediction["population"].extend(
                str(span.get("span_id"))
                for span in evidence.get("population_evidence_spans", [])
                if span.get("span_id")
            )
            validated_prediction["item"].extend(
                str(span.get("span_id"))
                for span in evidence.get("item_evidence_spans", [])
                if span.get("span_id")
            )
            validated_prediction["dimension"].extend(
                str(span.get("span_id"))
                for span in match.get("validation", {}).get("dimension_spans", [])
                if span.get("span_id")
            )
        validated_predictions[fixture_id] = validated_prediction

    stages: dict[str, Any] = {}
    for stage, predictions in (
        ("raw_binding", raw_predictions),
        ("validated_pass", validated_predictions),
    ):
        rows = _evaluate_stage_rows(eligible, predictions)
        stages[stage] = {
            "covered_rows": sum(row["covered"] for row in rows),
            "total_rows": len(rows),
            "end_to_end": _metric_block(rows, covered_only=False),
            "covered_only": _metric_block(rows, covered_only=True),
            "rows": rows,
        }
    return {
        "gold_status": "CONFIRMED",
        "eligible_gold_rows": len(eligible),
        "evaluation_unit": "source_candidate_id_set",
        "stages": stages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = evaluate_semantic_roles(
        args.run_root,
        load_jsonl(args.fixture),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        stage: {
            "covered_rows": block["covered_rows"],
            "end_to_end": block["end_to_end"],
            "covered_only": block["covered_only"],
        }
        for stage, block in result["stages"].items()
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
