"""Audit r16i blocking behavior before the article-layer redesign."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _gold_attribution(
    scope_rows: list[dict[str, Any]],
    prediction_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    scope_by_claim = {
        (str(row.get("article_idx") or ""), row.get("claim_index")): (
            row.get("scope_validation") or row
        )
        for row in scope_rows
    }
    by_code: dict[str, Counter[str]] = defaultdict(Counter)
    joined_blocked_rows = 0
    missing_scope_rows = []
    for row in prediction_rows:
        prediction = row.get("prediction") or {}
        automatic = row.get("automatic") or {}
        if not prediction.get("detected") or automatic.get("action") != "BLOCKED":
            continue
        key = (str(row.get("article_idx") or ""), prediction.get("claim_index"))
        scope = scope_by_claim.get(key)
        if scope is None:
            missing_scope_rows.append(row.get("review_id"))
            continue
        joined_blocked_rows += 1
        target_span_id = row.get("candidate_span_id")
        codes = set(scope.get("errors") or [])
        for observation in scope.get("observations") or []:
            value_span = observation.get("value_span") or {}
            if (
                target_span_id
                and value_span.get("span_id")
                and value_span.get("span_id") != target_span_id
            ):
                continue
            codes.update(observation.get("errors") or [])
        eligibility = str((row.get("gold") or {}).get("eligibility") or "")
        for code in codes:
            counter = by_code[str(code)]
            counter["total_blocked"] += 1
            if eligibility == "KOSIS_CANDIDATE":
                counter["gold_kosis_lost"] += 1
            else:
                counter["justified_non_kosis_block"] += 1

    rows = []
    for code, counts in sorted(
        by_code.items(),
        key=lambda item: (
            -item[1]["gold_kosis_lost"],
            -item[1]["total_blocked"],
            item[0],
        ),
    ):
        total = counts["total_blocked"]
        rows.append({
            "block_code": code,
            "total_blocked": total,
            "gold_kosis_lost": counts["gold_kosis_lost"],
            "justified_non_kosis_block": counts[
                "justified_non_kosis_block"
            ],
            "loss_ratio": (
                counts["gold_kosis_lost"] / total if total else 0.0
            ),
        })
    return {
        "joined_blocked_rows": joined_blocked_rows,
        "missing_scope_review_ids": missing_scope_rows,
        "by_block_code": rows,
    }


def audit_r16i_run(
    run_root: Path,
    *,
    gold_predictions_path: Path | None = None,
    retry_run_roots: list[Path] | None = None,
) -> dict[str, Any]:
    scope_rows = _read_jsonl(run_root / "scope_validation.jsonl")
    semantic_rows = _read_jsonl(run_root / "semantic_validation.jsonl")
    claim_errors: Counter[str] = Counter()
    observation_errors: Counter[str] = Counter()
    blocked_rows = 0
    for row in scope_rows:
        scope = row.get("scope_validation") or row
        if scope.get("claim_status") != "PASS":
            blocked_rows += 1
        claim_errors.update(scope.get("errors") or [])
        for observation in scope.get("observations") or []:
            if observation.get("status") != "PASS":
                observation_errors.update(observation.get("errors") or [])

    ambiguous_blocks = []
    for row in semantic_rows:
        validation = row.get("semantic_validation") or {}
        if "CANDIDATE_CLASS_AMBIGUOUS" not in (validation.get("errors") or []):
            continue
        claim = row.get("semantic_claim_effective") or {}
        ambiguous_blocks.append({
            "article_idx": str(row.get("article_idx") or ""),
            "claim_index": row.get("claim_index"),
            "classification_reason": claim.get("classification_reason"),
            "candidate_coverage_source": claim.get("candidate_coverage_source"),
            "candidate_class_override": claim.get("candidate_class_override"),
            "target_value_span_ids": claim.get("target_value_span_ids") or [],
        })

    report = {
        "run_root": str(run_root),
        "scope": {
            "rows": len(scope_rows),
            "blocked_rows": blocked_rows,
            "passed_rows": len(scope_rows) - blocked_rows,
            "claim_errors": claim_errors.most_common(),
            "observation_errors": observation_errors.most_common(),
        },
        "ambiguous_hard_blocks": {
            "rows": len(ambiguous_blocks),
            "records": ambiguous_blocks,
            "model_only_rows": sum(
                row["candidate_coverage_source"] == "HCX"
                and not row["candidate_class_override"]
                for row in ambiguous_blocks
            ),
        },
    }
    if gold_predictions_path is not None:
        prediction_rows = _read_jsonl(gold_predictions_path)
        report["gold_attribution"] = {
            "gold_predictions_path": str(gold_predictions_path),
            **_gold_attribution(scope_rows, prediction_rows),
        }
    if retry_run_roots:
        retry_by_target: dict[tuple[str, str], dict[str, Any]] = {}
        retry_class_counts: Counter[str] = Counter()
        current_ambiguous_records = []
        for retry_root in retry_run_roots:
            for row in _read_jsonl(retry_root / "raw.jsonl"):
                article_idx = str(row.get("article_idx") or "")
                claims = (row.get("semantic_prediction") or {}).get(
                    "claims", []
                )
                for claim in claims:
                    candidate_class = str(
                        claim.get("candidate_class") or ""
                    )
                    retry_class_counts[candidate_class] += 1
                    if candidate_class == "AMBIGUOUS":
                        current_ambiguous_records.append({
                            "article_idx": article_idx,
                            "target_value_span_ids": (
                                claim.get("target_value_span_ids") or []
                            ),
                            "run_root": str(retry_root),
                        })
                    for span_id in claim.get("target_value_span_ids") or []:
                        retry_by_target[(article_idx, span_id)] = {
                            "article_idx": article_idx,
                            "target_value_span_id": span_id,
                            "candidate_class": claim.get("candidate_class"),
                            "candidate_coverage_source": claim.get(
                                "candidate_coverage_source"
                            ),
                            "run_root": str(retry_root),
                        }
        target_records = []
        for blocked in ambiguous_blocks:
            article_idx = blocked["article_idx"]
            for span_id in blocked["target_value_span_ids"]:
                current = retry_by_target.get((article_idx, span_id))
                target_records.append(
                    current or {
                        "article_idx": article_idx,
                        "target_value_span_id": span_id,
                        "candidate_class": None,
                        "candidate_coverage_source": None,
                        "run_root": None,
                    }
                )
        report["ambiguous_retry_audit"] = {
            "retry_run_roots": [str(path) for path in retry_run_roots],
            "targets": len(target_records),
            "resolved_non_ambiguous": sum(
                row["candidate_class"]
                and row["candidate_class"] != "AMBIGUOUS"
                for row in target_records
            ),
            "still_ambiguous": sum(
                row["candidate_class"] == "AMBIGUOUS"
                for row in target_records
            ),
            "missing_from_retry": sum(
                not row["candidate_class"] for row in target_records
            ),
            "current_candidate_class_counts": dict(retry_class_counts),
            "current_candidate_class_ambiguous": len(
                current_ambiguous_records
            ),
            "current_ambiguous_records": current_ambiguous_records,
            "records": target_records,
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--gold-predictions", type=Path)
    parser.add_argument("--retry-run-root", type=Path, action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_r16i_run(
        args.run_root,
        gold_predictions_path=args.gold_predictions,
        retry_run_roots=args.retry_run_root,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
