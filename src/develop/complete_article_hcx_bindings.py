"""Complete only bindings that became eligible after deterministic revalidation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .article_claim_pipeline import (
    build_semantic_evidence_candidates,
    build_span_candidates,
    call_hcx_span_binding,
    filter_span_candidates_for_target_selection,
    validate_claim_skeleton,
)
from ..hcx_claim_experiment import env_api_key


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
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


def complete_missing_bindings(
    run_root: Path,
    output_root: Path,
    *,
    api_key: str,
) -> dict[str, Any]:
    inputs = {
        str(row.get("article_idx") or ""): row
        for row in _load_jsonl(run_root / "input.jsonl")
    }
    existing_bindings = {
        (str(row.get("article_idx") or ""), int(row["claim_index"])): row
        for row in _load_jsonl(run_root / "bindings.jsonl")
    }
    raw_rows = _load_jsonl(run_root / "raw.jsonl")

    candidate_rows: list[dict[str, Any]] = []
    binding_rows: list[dict[str, Any]] = []
    counts = {
        "articles": len(raw_rows),
        "claims": 0,
        "semantic_pass": 0,
        "semantic_blocked": 0,
        "bindings_reused": 0,
        "bindings_created": 0,
    }
    for raw_row in raw_rows:
        article_idx = str(raw_row.get("article_idx") or "")
        article = inputs.get(article_idx)
        if article is None:
            raise ValueError(f"article {article_idx}: input is missing")
        article_text = str(article.get("article_text") or "")
        identity = {
            "article_idx": article_idx,
            "article_sha256": raw_row.get("article_sha256"),
        }
        for claim_index, raw_claim in enumerate(
            raw_row.get("semantic_prediction", {}).get("claims", [])
        ):
            counts["claims"] += 1
            effective, semantic_validation = validate_claim_skeleton(
                article_text,
                raw_claim,
            )
            if semantic_validation["status"] != "PASS":
                counts["semantic_blocked"] += 1
                continue
            counts["semantic_pass"] += 1
            sentence_ids = list(dict.fromkeys([
                *effective.get("context_sentence_ids", []),
                *effective.get("observation_sentence_ids", []),
            ]))
            candidates = [
                *build_span_candidates(article_text, sentence_ids),
                *[
                    candidate
                    for candidate in build_semantic_evidence_candidates(
                        article_text
                    )
                    if candidate.get("sentence_id") in set(sentence_ids)
                ],
            ]
            binding_candidates, candidate_filter = (
                filter_span_candidates_for_target_selection(
                    effective,
                    candidates,
                )
            )
            candidate_rows.append({
                **identity,
                "claim_index": claim_index,
                "candidates": candidates,
                "binding_candidates": binding_candidates,
                "candidate_filter": candidate_filter,
            })
            key = (article_idx, claim_index)
            existing = existing_bindings.get(key)
            if existing is not None:
                binding_rows.append(existing)
                counts["bindings_reused"] += 1
                continue
            binding, usage, latency_ms = call_hcx_span_binding(
                effective,
                binding_candidates,
                api_key=api_key,
            )
            binding_rows.append({
                **identity,
                "claim_index": claim_index,
                "binding": binding,
                "usage": usage,
                "latency_ms": latency_ms,
            })
            counts["bindings_created"] += 1

    output_root.mkdir(parents=True, exist_ok=True)
    for filename in ("input.jsonl", "raw.jsonl"):
        (output_root / filename).write_text(
            (run_root / filename).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    _write_jsonl(output_root / "span_candidates.jsonl", candidate_rows)
    _write_jsonl(output_root / "bindings.jsonl", binding_rows)
    manifest = {
        "source_run_root": str(run_root),
        "mode": "MISSING_BINDING_COMPLETION",
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
    api_key = env_api_key()
    if not api_key:
        raise RuntimeError("HCX API key is unavailable")
    print(json.dumps(
        complete_missing_bindings(
            args.run_root,
            args.output_root,
            api_key=api_key,
        ),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
