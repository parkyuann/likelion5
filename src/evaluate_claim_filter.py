"""Evaluate claim-filter composition and deterministic KOSIS prefilter gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from retrieval_schema import compute_verifiability_prefilter

ROOT = Path(__file__).resolve().parent.parent


def bool_gold(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def evaluate(path: Path) -> dict:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = {"gold_is_claim", "gold_claim_class", "gold_source_scope",
                "gold_verifiability_prefilter"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    frame["gold_is_claim_bool"] = frame["gold_is_claim"].map(bool_gold)
    frame["computed_prefilter"] = frame.apply(
        lambda row: compute_verifiability_prefilter(
            row["gold_is_claim_bool"], row["gold_claim_class"], row["gold_source_scope"]
        )[0], axis=1,
    )
    frame["prefilter_consistent"] = (
        frame["computed_prefilter"] == frame["gold_verifiability_prefilter"]
    )

    claim_rows = frame[frame["gold_is_claim_bool"]].copy()
    kosis_attempt = claim_rows[claim_rows["computed_prefilter"] == "검증시도"]
    abstained = claim_rows[claim_rows["computed_prefilter"] != "검증시도"]
    return {
        "input": str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path),
        "rows": int(len(frame)),
        "articles": int(frame["article_idx"].nunique()) if "article_idx" in frame else None,
        "claim_rows": int(len(claim_rows)),
        "noise_rows": int(len(frame) - len(claim_rows)),
        "claim_rate": round(len(claim_rows) / max(len(frame), 1), 6),
        "class_counts": frame["gold_claim_class"].replace("", "UNKNOWN").value_counts().to_dict(),
        "source_scope_counts": frame["gold_source_scope"].replace("", "UNKNOWN").value_counts().to_dict(),
        "prefilter_counts": frame["computed_prefilter"].value_counts().to_dict(),
        "prefilter_consistency": round(float(frame["prefilter_consistent"].mean()), 6),
        "kosis_attempt_rows": int(len(kosis_attempt)),
        "abstained_claim_rows": int(len(abstained)),
        "abstention_rate_among_claims": round(len(abstained) / max(len(claim_rows), 1), 6),
        "attempt_class_counts": kosis_attempt["gold_claim_class"].value_counts().to_dict(),
        "attempt_source_scope_counts": kosis_attempt["gold_source_scope"].value_counts().to_dict(),
        "abstention_class_counts": abstained["gold_claim_class"].value_counts().to_dict(),
        "abstention_source_scope_counts": abstained["gold_source_scope"].value_counts().to_dict(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {path.stem: evaluate(path) for path in args.input}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
