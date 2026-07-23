"""Run configured HCX model experiments and select the best validation model."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def resolve(path: str | None) -> Path | None:
    if not path:
        return None
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def run_one(config: dict, model: dict, dataset: str, limit: int | None) -> None:
    common = dict(config.get("common", {}))
    overrides = dict(model.get(dataset, {}))
    settings = {**common, **overrides}
    input_path = resolve(config[f"input_{dataset}"])
    variant_id = model.get("id", model["name"])
    output_root = resolve(config["output_root"]) / dataset / variant_id
    summary = resolve(config["summary_file"])
    metrics_log = resolve(config["metrics_log"])
    command = [sys.executable, str(ROOT / "src" / "hcx_claim_experiment.py"),
               "--input", str(input_path), "--output-dir", str(output_root),
               "--summary-file", str(summary), "--metrics-log", str(metrics_log),
               "--model", model["name"], "--api-version", str(settings.get("api_version", "auto")),
               "--experiment-variant", variant_id,
               "--temperature", str(settings.get("temperature", 0.1)),
               "--top-p", str(settings.get("top_p", 0.8)),
               "--max-tokens", str(settings.get("max_tokens", 1200)),
               "--prompt-version", str(settings.get("prompt_version", "claim-observation-v1")),
               "--latency-ref-ms", str(settings.get("rlt_latency_ref_ms", 6112.722)),
               "--tokens-ref", str(settings.get("rlt_tokens_ref", 412.746)),
               "--rlt-recall-weight", str(settings.get("rlt_recall_weight", 0.6)),
               "--rlt-latency-weight", str(settings.get("rlt_latency_weight", 0.2)),
               "--rlt-tokens-weight", str(settings.get("rlt_tokens_weight", 0.2))]
    if settings.get("system_prompt_file"):
        command += ["--system-prompt-file", str(resolve(settings["system_prompt_file"]))]
    if settings.get("use_response_format"):
        command.append("--use-response-format")
    if settings.get("skip_tokenizer", False):
        command.append("--skip-tokenizer")
    if limit is not None:
        command += ["--limit", str(limit)]
    print(f"[HCX] dataset={dataset} variant={variant_id} model={model['name']} settings={json.dumps(settings, ensure_ascii=False)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dataset", choices=["validation", "final", "both"], default="validation")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model", action="append", help="Run only this model; repeat for multiple models.")
    parser.add_argument("--variant", action="append", help="Run only this configured variant ID; repeat for multiple variants.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    datasets = ["validation", "final"] if args.dataset == "both" else [args.dataset]
    selected_models = [m for m in config["models"] if (not args.model or m["name"] in args.model)
                       and (not args.variant or m.get("id", m["name"]) in args.variant)]
    if not selected_models:
        raise ValueError(f"no configured model matched: {args.model}")
    for dataset in datasets:
        for model in selected_models:
            run_one(config, model, dataset, args.limit)


if __name__ == "__main__":
    main()
