"""Select the validation model by RLT, then macro F1/recall as tie breakers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def select(path: Path) -> dict:
    frame = pd.read_csv(path, keep_default_na=False)
    required = {"model", "rlt_score", "claim_class_macro_f1", "source_scope_macro_f1",
                "claim_class_macro_recall", "source_scope_macro_recall"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"summary is missing columns: {missing}")
    for col in required - {"model"}:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(-1)
    frame["target_macro_f1"] = frame[["claim_class_macro_f1", "source_scope_macro_f1"]].mean(axis=1)
    frame["target_macro_recall"] = frame[["claim_class_macro_recall", "source_scope_macro_recall"]].mean(axis=1)
    winner = frame.sort_values(
        ["rlt_score", "target_macro_f1", "target_macro_recall"], ascending=False
    ).iloc[0]
    return {
        "model": winner["model"],
        "experiment_variant": winner.get("experiment_variant", ""),
        "prompt_version": winner.get("prompt_version", ""),
        "use_response_format": str(winner.get("use_response_format", "")),
        "experiment_id": winner.get("experiment_id", ""),
        "rlt_score": float(winner["rlt_score"]),
        "target_macro_f1": float(winner["target_macro_f1"]),
        "target_macro_recall": float(winner["target_macro_recall"]),
        "claim_class_macro_precision": float(winner.get("claim_class_macro_precision", -1)),
        "source_scope_macro_precision": float(winner.get("source_scope_macro_precision", -1)),
        "mean_inference_latency_ms_ok": float(winner.get("mean_inference_latency_ms_ok", -1)),
        "mean_total_tokens_ok": float(winner.get("mean_total_tokens_ok", -1)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = select(args.summary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
