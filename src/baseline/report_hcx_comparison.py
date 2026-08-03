"""Write an explicit HCX comparison table including precision, recall, and F1."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


DISPLAY_COLUMNS = [
    "model", "use_response_format", "prompt_version", "rows_ok", "rows_error",
    "claim_detection_precision", "claim_detection_recall", "claim_detection_f1",
    "claim_class_macro_precision", "claim_class_macro_recall", "claim_class_macro_f1",
    "source_scope_macro_precision", "source_scope_macro_recall", "source_scope_macro_f1",
    "rlt_recall", "rlt_score", "total_latency_ms", "mean_inference_latency_ms_ok",
    "prompt_tokens", "completion_tokens", "total_tokens", "mean_total_tokens_ok",
]


def write_report(summary: Path, output: Path) -> None:
    frame = pd.read_csv(summary, keep_default_na=False)
    columns = [column for column in DISPLAY_COLUMNS if column in frame.columns]
    frame = frame[columns].copy()
    frame.to_csv(output.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    headers = [column.replace("_", " ") for column in columns]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(row[column]) for column in columns) + " |")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_report(args.summary, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
