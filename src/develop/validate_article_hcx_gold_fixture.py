"""Validate the source-grounded five-article HCX calibration gold fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .article_hcx_gold_fixture import load_jsonl, load_saved_articles, validate_gold_fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate article HCX calibration gold fixture")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--saved-run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_gold_fixture(load_jsonl(args.fixture), load_saved_articles(args.saved_run_root))
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if result["invalid_rows"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
