"""Build deterministic table-mapping features from an existing article HCX run.

No network calls are made.  The command replays current validation rules over
saved semantic and binding outputs so feature records always reflect the active
scope gate.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .article_claim_pipeline import build_span_candidates, validate_claim_observation_scope, validate_span_binding
    from .table_mapping_features import FEATURE_BUILDER_VERSION, FEATURE_SCHEMA_VERSION, build_table_mapping_features
except ImportError:  # pragma: no cover
    from article_claim_pipeline import build_span_candidates, validate_claim_observation_scope, validate_span_binding
    from table_mapping_features import FEATURE_BUILDER_VERSION, FEATURE_SCHEMA_VERSION, build_table_mapping_features


def _read_one_jsonl(path: Path) -> dict[str, Any]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"expected one JSONL row: {path}")
    return json.loads(lines[0])


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _candidate_audit(*, article_idx: str, claim_index: int, candidates: list[dict[str, Any]], features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, list[str]] = {}
    for feature in features:
        for span_ids in feature["source_span_ids"].values():
            for span_id in span_ids:
                selected.setdefault(span_id, []).append(feature["observation_id"])
    return [{
        "source_article_id": article_idx,
        "claim_id": f"{article_idx}:{claim_index}",
        "span_id": candidate["span_id"],
        "kind": candidate["kind"],
        "text": candidate["text"],
        "sentence_id": candidate["sentence_id"],
        "selected_for_mapping": candidate["span_id"] in selected,
        "selected_by_observation_ids": selected.get(candidate["span_id"], []),
    } for candidate in candidates]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build table_mapping_features from a saved HCX calibration run")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--require-value-relation",
        action="store_true",
        help="Fail closed unless each binding asserts TARGET_MEASURE + SAME_METRIC relation evidence.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    features: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    candidate_audit: list[dict[str, Any]] = []
    claim_statuses: Counter[str] = Counter()
    binding_error_counts: Counter[str] = Counter()
    for directory in sorted(args.run_root.iterdir()):
        if not directory.is_dir() or not directory.name.startswith("기사_"):
            continue
        input_row = _read_one_jsonl(directory / "input.jsonl")
        validation_row = _read_one_jsonl(directory / "validation.jsonl")
        article_idx = str(input_row["article_idx"])
        for claim in validation_row["validation"].get("claims", []):
            semantic_claim = claim["semantic_claim"]
            sentence_ids = list(semantic_claim.get("context_sentence_ids", [])) + list(semantic_claim.get("observation_sentence_ids", []))
            candidates = build_span_candidates(input_row["article_text"], sentence_ids)
            binding = claim.get("binding")
            binding_validation = claim.get("validation")
            if isinstance(binding, dict) and isinstance(binding_validation, dict):
                binding_validation = validate_span_binding(
                    semantic_claim,
                    binding,
                    candidates,
                    require_value_relation=args.require_value_relation,
                )
                for observation in binding_validation.get("observations", []):
                    if isinstance(observation, dict):
                        binding_error_counts.update(
                            error for error in observation.get("errors", []) if isinstance(error, str)
                        )
                scope_validation = validate_claim_observation_scope(input_row["article_text"], semantic_claim, binding_validation)
            else:
                scope_validation = claim.get("scope_validation") or {"claim_status": "BLOCKED", "errors": ["BINDING_MISSING"], "observations": []}
            claim_features, block = build_table_mapping_features(
                article_idx=article_idx,
                article_sha256=validation_row["article_sha256"],
                article_text=input_row["article_text"],
                claim_index=claim["claim_index"],
                semantic_claim=semantic_claim,
                binding=binding,
                binding_validation=binding_validation,
                scope_validation=scope_validation,
            )
            claim_statuses[scope_validation["claim_status"]] += 1
            features.extend(claim_features)
            if block:
                blocks.append(block)
            candidate_audit.extend(_candidate_audit(
                article_idx=article_idx, claim_index=claim["claim_index"], candidates=candidates, features=claim_features,
            ))

    _write_jsonl(args.output_dir / "table_mapping_features.jsonl", features)
    _write_jsonl(args.output_dir / "table_mapping_feature_blocks.jsonl", blocks)
    _write_jsonl(args.output_dir / "candidate_span_audit.jsonl", candidate_audit)
    missing = {
        "indicator_terms": sum(not row["indicator_terms"] for row in features),
        "population_terms": sum(not row["population_terms"] for row in features),
        "item_constraint_terms": sum(not row["item_constraint_terms"] for row in features),
        "period": sum(not row["period"]["raw"] and not row.get("period_constraint_terms") for row in features),
        "comparison_constraint_terms": sum(not row.get("comparison_constraint_terms") for row in features),
        "dimension_constraints": sum(not row["dimension_constraints"] for row in features),
    }
    manifest = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "source_run_root": str(args.run_root),
        "require_value_relation": args.require_value_relation,
        "scope_claim_statuses": dict(claim_statuses),
        "binding_error_counts": dict(binding_error_counts),
        "retrieval_eligible_features": len(features),
        "blocked_claims": len(blocks),
        "missing_field_counts": missing,
        "candidate_span_audit": {
            "candidates": len(candidate_audit),
            "selected_for_mapping": sum(row["selected_for_mapping"] for row in candidate_audit),
            "unselected_candidates": sum(not row["selected_for_mapping"] for row in candidate_audit),
            "note": "Unselected candidates are an audit queue, not a gold-labelled span omission metric.",
        },
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
