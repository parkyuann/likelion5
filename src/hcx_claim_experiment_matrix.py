"""Run the HCX claim experiment for all comparison models."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS = ["HCX-003", "HCX-005", "HCX-007", "HCX-DASH-001", "HCX-DASH-002"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one HCX experiment per model.")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--config", type=Path, help="JSON experiment matrix configuration")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--prompt-version", default="claim-observation-v1")
    parser.add_argument("--input-cost-per-1m", type=float, default=0.0)
    parser.add_argument("--output-cost-per-1m", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = {}
    if args.config:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    common = config.get("common", {})
    config_input = config.get("input")
    input_path = args.input or ((ROOT / config_input) if config_input and not Path(config_input).is_absolute() else (Path(config_input) if config_input else None))
    if input_path is None:
        parser.error("--input 또는 config.input이 필요합니다")
    models = config.get("models", args.models)

    for model in models:
        overrides = config.get("model_overrides", {}).get(model, {})
        def value(name, default):
            return overrides.get(name, common.get(name, getattr(args, name.replace("-", "_"), default)))
        command = [
            sys.executable, str(ROOT / "src" / "hcx_claim_experiment.py"),
            "--input", str(input_path), "--model", model,
            "--temperature", str(value("temperature", 0.0)), "--top-p", str(value("top_p", 0.8)),
            "--prompt-version", str(value("prompt_version", "claim-observation-v1")),
            "--input-cost-per-1m", str(value("input_cost_per_1m", 0.0)),
            "--output-cost-per-1m", str(value("output_cost_per_1m", 0.0)),
        ]
        limit = value("limit", None)
        if limit is not None:
            command += ["--limit", str(limit)]
        if args.dry_run:
            command.append("--dry-run")
        print(f"[matrix] {model}", flush=True)
        subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
