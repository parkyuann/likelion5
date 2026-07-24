"""Merge HCX Structured Output with the fixed hybrid-v3 sample as canonical claims."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8-sig") if line.strip()]


def canonical_row(sample: dict, result: dict) -> dict:
    prediction = result.get("prediction") if isinstance(result.get("prediction"), dict) else {}
    observations = prediction.get("observations") if isinstance(prediction.get("observations"), list) else []
    return {
        "claim_id": sample["claim_id"],
        "claim_text": sample["claim_text"],
        "source_row_number": sample.get("source_row_number"),
        "article_idx": sample.get("article_idx"),
        "sentence_index": sample.get("sentence_index"),
        "sample_category_path": sample.get("matched_category_path"),
        "sample_seed_table_key": sample.get("matched_table_key_seed"),
        "is_claim": bool(prediction.get("is_claim")),
        "claim_class": prediction.get("claim_class"),
        "source_scope": prediction.get("source_scope"),
        "indicator_raw": prediction.get("indicator_raw"),
        "population_raw": prediction.get("population"),
        "observations": observations,
        "source_org_raw": prediction.get("source_org_raw"),
        "source_role": prediction.get("source_role"),
        "verifiability_prefilter": prediction.get("verifiability_prefilter"),
        "hcx_status": result.get("status"),
        "hcx_latency_ms": result.get("latency_ms"),
        "hcx_total_tokens": result.get("total_tokens"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=ROOT / "output" / "hybrid_v3_500_20260723" / "sample_claims_500.jsonl")
    parser.add_argument("--hcx-cache", type=Path, required=True, help="hcx_claim_experiment.py cache.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "hybrid_v3_500_20260723" / "canonical_claims_500.jsonl")
    args = parser.parse_args()
    sample = read_jsonl(args.sample)
    results = read_jsonl(args.hcx_cache)
    # The general evaluation runner keys cache rows by eval_claim_id. Hybrid
    # samples instead use claim_id, so older runner output has blank IDs. A
    # cache is appended in input order; only use that fallback when its length
    # exactly matches the fixed sample to prevent a silent misalignment.
    cache_ids = [str(row.get("eval_claim_id") or "") for row in results]
    if all(not value for value in cache_ids):
        if len(results) != len(sample):
            raise ValueError("blank cache IDs require cache rows to equal sample rows")
        matched = results
    else:
        by_id = {value: row for value, row in zip(cache_ids, results) if value}
        matched = [by_id.get(str(row.get("claim_id") or ""), {}) for row in sample]
    rows = [canonical_row(sample_row, result) for sample_row, result in zip(sample, matched)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"sample_rows": len(sample), "hcx_matched": sum(row["hcx_status"] == "ok" for row in rows), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
