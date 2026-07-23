"""Select the best validation model and run only that model on final500."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from select_hcx_model import select

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--selection-output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    winner = select(args.summary)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    variant_id = winner.get("experiment_variant")
    if not variant_id:
        matches = [
            item.get("id", item["name"])
            for item in config["models"]
            if item["name"] == winner["model"]
            and item.get("validation", {}).get("prompt_version") == winner.get("prompt_version")
        ]
        if len(matches) != 1:
            raise ValueError(f"could not uniquely resolve selected variant for {winner['model']}")
        variant_id = matches[0]
    winner["experiment_variant"] = variant_id
    args.selection_output.parent.mkdir(parents=True, exist_ok=True)
    args.selection_output.write_text(json.dumps(winner, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        sys.executable, str(ROOT / "src" / "run_hcx_experiment_matrix.py"),
        "--config", str(args.config), "--dataset", "final", "--variant", variant_id,
    ]
    if args.limit is not None:
        command += ["--limit", str(args.limit)]
    print(json.dumps({"selected_model": winner["model"], "command": command}, ensure_ascii=False))
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
