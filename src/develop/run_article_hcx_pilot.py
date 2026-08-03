"""기사 단위 HCX 구조화·원문 검증 파일럿 실행기."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .article_claim_pipeline import apply_article_relative_period_context, build_semantic_evidence_candidates, build_span_candidates, call_hcx_claim_skeleton, call_hcx_semantic_article, call_hcx_span_binding, filter_span_candidates_for_measurement_type, filter_span_candidates_for_target_selection, pass_scope_bound_observations, validate_claim_observation_scope, validate_claim_skeleton, validate_semantic_claim, validate_span_binding
    from ..hcx_claim_experiment import env_api_key
except ImportError:  # pragma: no cover
    from article_claim_pipeline import apply_article_relative_period_context, build_semantic_evidence_candidates, build_span_candidates, call_hcx_claim_skeleton, call_hcx_semantic_article, call_hcx_span_binding, filter_span_candidates_for_measurement_type, filter_span_candidates_for_target_selection, pass_scope_bound_observations, validate_claim_observation_scope, validate_claim_skeleton, validate_semantic_claim, validate_span_binding
    from hcx_claim_experiment import env_api_key


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def select_articles(claims: list[dict[str, Any]], articles: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    selected = []
    for article_idx in sorted({str(claim.get("article_idx") or "") for claim in claims if str(claim.get("article_idx") or "").isdigit()}):
        row = articles.iloc[int(article_idx)]
        text = str(row.get("본문_정제") or "").strip()
        if not text:
            continue
        article = {
            "article_idx": article_idx,
            "title": str(row.get("기사제목") or ""),
            "article_text": text,
        }
        published_at = str(row.iloc[1] or "").strip() if len(row) > 1 else ""
        if published_at:
            article["published_at"] = published_at
        selected.append(article)
        if len(selected) >= limit:
            break
    return selected


def select_saved_articles(run_root: Path, limit: int) -> list[dict[str, Any]]:
    """Reuse exact audited article inputs when the original CSV is unavailable.

    The saved ``input.jsonl`` record is an immutable copy of the title and
    whole article text used by an earlier calibration.  This avoids silently
    reconstructing or changing text before an approved HCX rerun.
    """
    selected: list[dict[str, Any]] = []
    flat_input = run_root / "input.jsonl"
    paths = [flat_input] if flat_input.is_file() else sorted(
        run_root.glob("*/input.jsonl"),
        key=lambda item: item.parent.name,
    )
    for path in paths:
        rows = read_jsonl(path)
        if path != flat_input and len(rows) != 1:
            raise ValueError(f"expected exactly one article input: {path}")
        for row in rows:
            article_idx = str(row.get("article_idx") or "")
            title = row.get("title")
            article_text = row.get("article_text")
            if not article_idx.isdigit() or not isinstance(title, str) or not isinstance(article_text, str) or not article_text.strip():
                raise ValueError(f"invalid saved article input: {path}")
            selected_article = {
                "article_idx": article_idx,
                "title": title,
                "article_text": article_text,
            }
            published_at = row.get("published_at")
            if isinstance(published_at, str) and published_at.strip():
                selected_article["published_at"] = published_at.strip()
            selected.append(selected_article)
            if len(selected) >= limit:
                return selected
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="기사 단위 HCX-007 구조화 파일럿")
    parser.add_argument("--claims", type=Path)
    parser.add_argument("--articles", type=Path)
    parser.add_argument(
        "--saved-run-root",
        type=Path,
        help="Reuse audited */input.jsonl article records instead of the original claims/CSV inputs.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--article-idx", help="Run one selected article in an isolated, resumable calibration directory.")
    parser.add_argument("--contract", choices=("full", "skeleton"), default="full")
    args = parser.parse_args()
    api_key = env_api_key()
    if not api_key:
        raise RuntimeError("HCX API key is unavailable")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.saved_run_root:
        if args.claims or args.articles:
            raise ValueError("--saved-run-root cannot be combined with --claims or --articles")
        chosen = select_saved_articles(args.saved_run_root, args.limit)
        input_source = {"kind": "saved_run_root", "path": str(args.saved_run_root)}
    else:
        if not args.claims or not args.articles:
            raise ValueError("provide both --claims and --articles, or provide --saved-run-root")
        chosen = select_articles(read_jsonl(args.claims), pd.read_csv(args.articles, dtype=str, keep_default_na=False), args.limit)
        input_source = {"kind": "claims_and_articles", "claims": str(args.claims), "articles": str(args.articles)}
    if args.article_idx is not None:
        chosen = [article for article in chosen if article["article_idx"] == str(args.article_idx)]
        if not chosen:
            raise ValueError(f"article_idx {args.article_idx} is not present in the selected claims input")
    paths = {
        "input": args.output_dir / "input.jsonl", "raw": args.output_dir / "raw.jsonl",
        "candidates": args.output_dir / "span_candidates.jsonl", "semantic_validation": args.output_dir / "semantic_validation.jsonl", "bindings": args.output_dir / "bindings.jsonl", "scope_validation": args.output_dir / "scope_validation.jsonl",
        "validation": args.output_dir / "validation.jsonl", "pass": args.output_dir / "pass_observations.jsonl",
        "errors": args.output_dir / "errors.jsonl", "manifest": args.output_dir / "manifest.json",
    }
    for key in ("input", "raw", "candidates", "semantic_validation", "bindings", "scope_validation", "validation", "pass", "errors"):
        paths[key].write_text("", encoding="utf-8")
    counts = {"ok": 0, "error": 0, "semantic_claims": 0, "semantic_pass": 0, "semantic_blocked": 0,
              "pass_observations": 0, "binding_calls": 0, "binding_skipped_no_value_candidate": 0,
              "binding_skipped_semantic_validation": 0, "scope_claim_pass": 0, "scope_claim_blocked": 0,
              "scope_pass_observations": 0, "candidate_class_kosis": 0,
              "candidate_class_out_of_scope": 0, "candidate_class_not_claim": 0,
              "candidate_class_ambiguous": 0}
    for article in chosen:
        paths["input"].open("a", encoding="utf-8").write(json.dumps(article, ensure_ascii=False) + "\n")
        try:
            semantic_call = call_hcx_claim_skeleton if args.contract == "skeleton" else call_hcx_semantic_article
            prediction, usage, latency_ms = semantic_call(article["title"], article["article_text"], api_key=api_key)
            identity = {"article_idx": article["article_idx"], "article_sha256": hashlib.sha256(article["article_text"].encode()).hexdigest()}
            paths["raw"].open("a", encoding="utf-8").write(json.dumps({**identity, "semantic_prediction": prediction, "usage": usage, "latency_ms": latency_ms}, ensure_ascii=False) + "\n")
            claim_reports, passed = [], []
            for claim_index, semantic_claim in enumerate(prediction.get("claims", [])):
                if args.contract == "skeleton":
                    effective_semantic_claim, semantic_validation = validate_claim_skeleton(article["article_text"], semantic_claim)
                    period_context_audit = {"reason": "NOT_APPLIED_TO_SKELETON", "raw_context_sentence_ids": semantic_claim.get("context_sentence_ids", []), "effective_context_sentence_ids": effective_semantic_claim.get("context_sentence_ids", [])}
                else:
                    effective_semantic_claim, period_context_audit = apply_article_relative_period_context(article["article_text"], semantic_claim)
                    semantic_validation = validate_semantic_claim(article["article_text"], effective_semantic_claim, require_constraints=True)
                paths["semantic_validation"].open("a", encoding="utf-8").write(json.dumps(
                    {**identity, "claim_index": claim_index, "semantic_claim_raw": semantic_claim,
                     "semantic_claim_effective": effective_semantic_claim, "period_context_audit": period_context_audit,
                     "semantic_validation": semantic_validation}, ensure_ascii=False) + "\n")
                counts["semantic_claims"] += 1
                candidate_class = str(
                    effective_semantic_claim.get("candidate_class") or ""
                )
                class_count_key = {
                    "KOSIS_CANDIDATE": "candidate_class_kosis",
                    "OUT_OF_SCOPE": "candidate_class_out_of_scope",
                    "NOT_CLAIM": "candidate_class_not_claim",
                    "AMBIGUOUS": "candidate_class_ambiguous",
                }.get(candidate_class)
                if class_count_key:
                    counts[class_count_key] += 1
                if semantic_validation["status"] != "PASS":
                    counts["semantic_blocked"] += 1
                    counts["binding_skipped_semantic_validation"] += 1
                    counts["scope_claim_blocked"] += 1
                    blocked_scope = {"claim_status": "BLOCKED", "errors": ["SEMANTIC_VALIDATION_BLOCKED", *semantic_validation["errors"]], "observations": []}
                    paths["scope_validation"].open("a", encoding="utf-8").write(json.dumps(
                        {**identity, "claim_index": claim_index, "scope_validation": blocked_scope}, ensure_ascii=False) + "\n")
                    claim_reports.append({"claim_index": claim_index, "semantic_claim": effective_semantic_claim, "semantic_claim_raw": semantic_claim,
                                          "period_context_audit": period_context_audit,
                                          "semantic_validation": semantic_validation, "binding": None, "scope_validation": blocked_scope,
                                          "validation": {"claim_status": "CONFLICT",
                                                         "errors": ["SEMANTIC_VALIDATION_BLOCKED", *semantic_validation["errors"]],
                                                         "observations": []}})
                    continue
                counts["semantic_pass"] += 1
                sentence_ids = list(effective_semantic_claim.get("context_sentence_ids", [])) + list(effective_semantic_claim.get("observation_sentence_ids", []))
                candidates = [
                    *build_span_candidates(article["article_text"], sentence_ids),
                    *[
                        candidate for candidate in build_semantic_evidence_candidates(article["article_text"])
                        if candidate.get("sentence_id") in set(sentence_ids)
                    ],
                ]
                if args.contract == "skeleton":
                    binding_candidates, candidate_filter = filter_span_candidates_for_target_selection(
                        effective_semantic_claim, candidates,
                    )
                else:
                    binding_candidates, candidate_filter = filter_span_candidates_for_measurement_type(effective_semantic_claim, candidates)
                paths["candidates"].open("a", encoding="utf-8").write(json.dumps(
                    {**identity, "claim_index": claim_index, "candidates": candidates,
                     "binding_candidates": binding_candidates, "candidate_filter": candidate_filter}, ensure_ascii=False) + "\n")
                if not any(candidate["kind"] == "value_unit" for candidate in binding_candidates):
                    binding = None
                    validation = {"claim_status": "CONFLICT", "errors": ["NO_MEASUREMENT_TYPE_COMPATIBLE_VALUE_CANDIDATE"], "observations": []}
                    scope_validation = {"claim_status": "BLOCKED", "errors": ["NO_MEASUREMENT_TYPE_COMPATIBLE_VALUE_CANDIDATE"], "observations": []}
                    counts["binding_skipped_no_value_candidate"] += 1
                    counts["scope_claim_blocked"] += 1
                else:
                    binding, binding_usage, binding_latency_ms = call_hcx_span_binding(effective_semantic_claim, binding_candidates, api_key=api_key)
                    validation = validate_span_binding(
                        effective_semantic_claim,
                        binding,
                        binding_candidates,
                        require_value_relation=True,
                        require_measurement_type=True,
                        require_semantic_evidence=args.contract == "skeleton",
                        article_text=article["article_text"],
                        reference_date=article.get("published_at"),
                    )
                    scope_validation = validate_claim_observation_scope(article["article_text"], effective_semantic_claim, validation)
                    paths["bindings"].open("a", encoding="utf-8").write(json.dumps({**identity, "claim_index": claim_index, "binding": binding, "usage": binding_usage, "latency_ms": binding_latency_ms}, ensure_ascii=False) + "\n")
                    scope_passed = pass_scope_bound_observations(effective_semantic_claim, binding, validation, scope_validation)
                    passed.extend(scope_passed)
                    counts["scope_claim_pass" if scope_validation["claim_status"] == "PASS" else "scope_claim_blocked"] += 1
                    counts["scope_pass_observations"] += len(scope_passed)
                    counts["binding_calls"] += 1
                paths["scope_validation"].open("a", encoding="utf-8").write(json.dumps(
                    {**identity, "claim_index": claim_index, "scope_validation": scope_validation}, ensure_ascii=False) + "\n")
                claim_reports.append({"claim_index": claim_index, "semantic_claim": effective_semantic_claim, "semantic_claim_raw": semantic_claim,
                                      "period_context_audit": period_context_audit,
                                      "semantic_validation": semantic_validation, "binding": binding, "scope_validation": scope_validation,
                                      "validation": validation})
            validation = {"claims": claim_reports, "pass_observation_count": len(passed)}
            paths["validation"].open("a", encoding="utf-8").write(json.dumps({**identity, "validation": validation}, ensure_ascii=False) + "\n")
            with paths["pass"].open("a", encoding="utf-8") as handle:
                for item in passed:
                    handle.write(json.dumps({**identity, **item}, ensure_ascii=False) + "\n")
            counts["ok"] += 1
            counts["pass_observations"] += len(passed)
        except Exception as error:  # keep a partial pilot auditable and resumable by retained inputs
            paths["errors"].open("a", encoding="utf-8").write(json.dumps({"article_idx": article["article_idx"], "error": type(error).__name__, "message": str(error)}, ensure_ascii=False) + "\n")
            counts["error"] += 1
    manifest = {"selected_articles": len(chosen), **counts, "model": "HCX-007", "contract": args.contract, "validation_policy": "PASS observation only", "input_source": input_source, "paths": {key: str(path) for key, path in paths.items()}}
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
