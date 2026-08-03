"""Run the L2 segmentation layer over the frozen 6-article gold set.

One HCX call per article, not per claim.  The r16i contract called the model
once per value candidate chunk; the layer contract asks for the article's
layout once, so the run is cheap enough to repeat when a layer changes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

try:
    from ..hcx_claim_experiment import env_api_key
    from .l2_segmentation import call_hcx_l2_segmentation, call_hcx_l2_split
except ImportError:  # pragma: no cover - direct script execution
    from hcx_claim_experiment import env_api_key
    from l2_segmentation import call_hcx_l2_segmentation, call_hcx_l2_split


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run(
    articles: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "HCX-007",
    retries: int = 2,
    pause_seconds: float = 1.0,
    contract: str = "single",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    call = (
        call_hcx_l2_split if contract == "split" else call_hcx_l2_segmentation
    )
    predictions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    usage_total = {"total_tokens": 0}
    latency_total = 0.0
    for article in articles:
        article_idx = str(article.get("article_idx"))
        title = str(article.get("title") or article.get("기사제목") or "")
        body = str(article.get("article_text") or "")
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resolved, usage, latency_ms = call(
                    title, body, api_key=api_key, model=model
                )
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                last_error = exc
                time.sleep(pause_seconds * (attempt + 1))
                continue
            latency_total += latency_ms
            usage_total["total_tokens"] += int(usage.get("totalTokens") or 0)
            for sentence in resolved["sentences"]:
                predictions.append({
                    "article_idx": article_idx,
                    **sentence,
                })
            if resolved["missing_sentence_ids"]:
                errors.append({
                    "article_idx": article_idx,
                    "kind": "MISSING_SENTENCES",
                    "sentence_ids": resolved["missing_sentence_ids"],
                })
            if resolved["unresolved_spans"]:
                errors.append({
                    "article_idx": article_idx,
                    "kind": "UNRESOLVED_SPANS",
                    "count": resolved["unresolved_spans"],
                })
            last_error = None
            break
        if last_error is not None:
            errors.append({
                "article_idx": article_idx,
                "kind": "CALL_FAILED",
                "detail": str(last_error)[:300],
            })
        time.sleep(pause_seconds)

    manifest = {
        "contract_version": (
            "l2_segmentation_split_v1"
            if contract == "split"
            else "l2_segmentation_v1"
        ),
        "model": model,
        "articles": len(articles),
        "sentences_predicted": len(predictions),
        "total_tokens": usage_total["total_tokens"],
        "latency_ms_total": round(latency_total, 1),
        "errors": errors,
    }
    return predictions, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", default="HCX-007")
    parser.add_argument(
        "--contract", choices=("single", "split"), default="single"
    )
    args = parser.parse_args()

    api_key = env_api_key()
    if not api_key:
        raise SystemExit("NCP_CLOVASTUDIO_API_KEY is not configured")

    predictions, manifest = run(
        _read_jsonl(args.articles),
        api_key=api_key,
        model=args.model,
        contract=args.contract,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in predictions
        ),
        encoding="utf-8",
    )
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
