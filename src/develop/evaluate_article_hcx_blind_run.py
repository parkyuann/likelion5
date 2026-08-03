"""Evaluate a deterministic article-HCX run against the confirmed blind gold."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from .apply_article_hcx_blind_review import evaluate_records


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _text(value: object) -> str:
    return str(value or "").strip()


def _role_texts(
    role_evidence: dict[str, Any],
    field: str,
) -> list[str]:
    values: list[str] = []
    for span in role_evidence.get(field, []):
        text = _text(span.get("text") if isinstance(span, dict) else None)
        if text and text not in values:
            values.append(text)
    return values


def apply_run_predictions(
    gold_records: list[dict[str, Any]],
    run_dir: Path,
) -> list[dict[str, Any]]:
    """Replace workbook-era automatic fields with one run's validated output."""
    validations: dict[tuple[str, int], dict[str, Any]] = {}
    for article in _load_jsonl(run_dir / "validation.jsonl"):
        article_idx = _text(article.get("article_idx"))
        for claim in article.get("validation", {}).get("claims", []):
            validations[(article_idx, int(claim["claim_index"]))] = claim

    output: list[dict[str, Any]] = []
    for source in gold_records:
        row = copy.deepcopy(source)
        key = (row["article_idx"], int(row["claim_index"]))
        claim = validations.get(key)
        if claim is None:
            raise ValueError(f"{row['review_id']}: claim missing from {run_dir}")
        semantic = claim.get("semantic_claim", {})
        semantic_validation = claim.get("semantic_validation", {})
        binding_validation = claim.get("validation", {})
        scope_validation = claim.get("scope_validation", {})
        role_evidence = binding_validation.get("semantic_role_evidence", {})
        target = _text(row.get("target_value"))
        matched = [
            observation
            for observation in binding_validation.get("observations", [])
            if _text((observation.get("value_span") or {}).get("text")) == target
        ]
        if target and len(matched) != 1:
            raise ValueError(
                f"{row['review_id']}: expected one observation for {target!r}, "
                f"found {len(matched)}"
            )
        observation = matched[0] if matched else {}
        effective_fields = (
            observation.get("effective_search_fields")
            if isinstance(observation.get("effective_search_fields"), dict)
            else {}
        )
        row["automatic"] = {
            "indicator": _text(
                effective_fields.get("indicator_norm")
                or semantic.get("indicator_norm")
            ),
            "measurement_type": _text(observation.get("measurement_type")),
            "period": _text(
                observation.get("period_normalized")
                or (observation.get("period_span") or {}).get("text")
            ),
            "population": (
                list(effective_fields.get("population_terms", []))
                if "population_terms" in effective_fields
                else _role_texts(
                    role_evidence,
                    "population_evidence_spans",
                )
            ),
            "item": (
                list(effective_fields.get("item_terms", []))
                if "item_terms" in effective_fields
                else _role_texts(
                    role_evidence,
                    "item_evidence_spans",
                )
            ),
            "dimension": (
                list(effective_fields.get("dimension_terms", []))
                if "dimension_terms" in effective_fields
                else [
                text
                for text in (
                    _text(span.get("text"))
                    for span in observation.get("dimension_spans", [])
                    if isinstance(span, dict)
                )
                if text
                ]
            ),
            "semantic_status": _text(semantic_validation.get("status")),
            "binding_status": _text(binding_validation.get("claim_status")),
            "scope_status": _text(scope_validation.get("claim_status")),
            "action": (
                "PASS"
                if scope_validation.get("claim_status") == "PASS"
                else "BLOCKED"
            ),
        }
        row["prediction_run"] = str(run_dir)
        output.append(row)
    return output


def evaluate_run(
    gold_records: list[dict[str, Any]],
    run_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    predictions = apply_run_predictions(gold_records, run_dir)
    return predictions, {
        "source_gold": "CONFIRMED_HUMAN_ADJUDICATED",
        "prediction_run": str(run_dir),
        "evaluation": evaluate_records(predictions),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    predictions, report = evaluate_run(_load_jsonl(args.gold), args.run_dir)
    _write_jsonl(args.output, predictions)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "prediction_run": str(args.run_dir),
        "routing": report["evaluation"]["routing"],
        "complete_record": report["evaluation"]["complete_record"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
