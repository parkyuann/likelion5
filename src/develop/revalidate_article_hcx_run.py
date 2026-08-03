"""Reapply current deterministic validators to a saved article HCX run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .article_claim_pipeline import (
    pass_scope_bound_observations,
    validate_claim_observation_scope,
    validate_claim_skeleton,
    validate_span_binding,
)


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


def revalidate_saved_run(
    run_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    input_rows = _read_jsonl(run_root / "input.jsonl")
    raw_rows = _read_jsonl(run_root / "raw.jsonl")
    candidate_rows = _read_jsonl(run_root / "span_candidates.jsonl")
    binding_rows = _read_jsonl(run_root / "bindings.jsonl")
    articles = {
        str(row.get("article_idx") or ""): row
        for row in input_rows
    }
    candidates_by_key = {
        (str(row.get("article_idx") or ""), row.get("claim_index")): row
        for row in candidate_rows
    }
    bindings_by_key = {
        (str(row.get("article_idx") or ""), row.get("claim_index")): row
        for row in binding_rows
    }

    semantic_rows: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    scope_rows: list[dict[str, Any]] = []
    pass_rows: list[dict[str, Any]] = []
    counts = {
        "articles": 0,
        "claims": 0,
        "semantic_pass": 0,
        "semantic_blocked": 0,
        "binding_revalidated": 0,
        "binding_unavailable": 0,
        "pass_observations": 0,
    }
    for raw_row in raw_rows:
        article_idx = str(raw_row.get("article_idx") or "")
        article = articles.get(article_idx, {})
        article_text = str(article.get("article_text") or "")
        identity = {
            "article_idx": article_idx,
            "article_sha256": raw_row.get("article_sha256"),
        }
        claim_reports: list[dict[str, Any]] = []
        article_passed: list[dict[str, Any]] = []
        claims = raw_row.get("semantic_prediction", {}).get("claims", [])
        for claim_index, raw_claim in enumerate(claims):
            counts["claims"] += 1
            effective, semantic_validation = validate_claim_skeleton(
                article_text,
                raw_claim,
            )
            semantic_rows.append({
                **identity,
                "claim_index": claim_index,
                "semantic_claim_raw": raw_claim,
                "semantic_claim_effective": effective,
                "period_context_audit": {
                    "reason": "NOT_APPLIED_TO_SKELETON",
                    "raw_context_sentence_ids": raw_claim.get("context_sentence_ids", []),
                    "effective_context_sentence_ids": effective.get(
                        "context_sentence_ids", []
                    ),
                },
                "semantic_validation": semantic_validation,
            })
            key = (article_idx, claim_index)
            candidate_row = candidates_by_key.get(key)
            binding_row = bindings_by_key.get(key)
            if semantic_validation["status"] != "PASS":
                counts["semantic_blocked"] += 1
                scope_validation = {
                    "claim_status": "BLOCKED",
                    "errors": [
                        "SEMANTIC_VALIDATION_BLOCKED",
                        *semantic_validation["errors"],
                    ],
                    "observations": [],
                }
                validation = {
                    "claim_status": "CONFLICT",
                    "errors": scope_validation["errors"],
                    "observations": [],
                }
                binding = None
            elif not candidate_row or not binding_row:
                counts["semantic_pass"] += 1
                counts["binding_unavailable"] += 1
                scope_validation = {
                    "claim_status": "BLOCKED",
                    "errors": ["SAVED_BINDING_UNAVAILABLE"],
                    "observations": [],
                }
                validation = {
                    "claim_status": "CONFLICT",
                    "errors": ["SAVED_BINDING_UNAVAILABLE"],
                    "observations": [],
                }
                binding = None
            else:
                counts["semantic_pass"] += 1
                counts["binding_revalidated"] += 1
                binding = binding_row.get("binding", {})
                binding_candidates = candidate_row.get("binding_candidates", [])
                validation = validate_span_binding(
                    effective,
                    binding,
                    binding_candidates,
                    require_value_relation=True,
                    require_measurement_type=True,
                    require_semantic_evidence=True,
                    article_text=article_text,
                    reference_date=article.get("published_at"),
                )
                scope_validation = validate_claim_observation_scope(
                    article_text,
                    effective,
                    validation,
                )
                article_passed.extend(
                    pass_scope_bound_observations(
                        effective,
                        binding,
                        validation,
                        scope_validation,
                    )
                )
            scope_rows.append({
                **identity,
                "claim_index": claim_index,
                "scope_validation": scope_validation,
            })
            claim_reports.append({
                "claim_index": claim_index,
                "semantic_claim": effective,
                "semantic_claim_raw": raw_claim,
                "semantic_validation": semantic_validation,
                "binding": binding,
                "scope_validation": scope_validation,
                "validation": validation,
            })
        counts["articles"] += 1
        counts["pass_observations"] += len(article_passed)
        validation_rows.append({
            **identity,
            "validation": {
                "claims": claim_reports,
                "pass_observation_count": len(article_passed),
            },
        })
        pass_rows.extend({**identity, **row} for row in article_passed)

    output_root.mkdir(parents=True, exist_ok=True)
    for filename in ("input.jsonl", "raw.jsonl", "span_candidates.jsonl", "bindings.jsonl"):
        (output_root / filename).write_text(
            (run_root / filename).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    _write_jsonl(output_root / "semantic_validation.jsonl", semantic_rows)
    _write_jsonl(output_root / "scope_validation.jsonl", scope_rows)
    _write_jsonl(output_root / "validation.jsonl", validation_rows)
    _write_jsonl(output_root / "pass_observations.jsonl", pass_rows)
    manifest = {
        "source_run_root": str(run_root),
        "mode": "DETERMINISTIC_REVALIDATION",
        **counts,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        revalidate_saved_run(args.run_root, args.output_root),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
