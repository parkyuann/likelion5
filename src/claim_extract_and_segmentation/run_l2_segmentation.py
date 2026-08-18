"""Run the L2 segmentation layer over the frozen 6-article gold set.

팀 인계: HCX L2 추론과 실행 manifest 생성을 담당하는 CLI 진입점이다. 새로운
L2 prediction 파일을 만들 때 사용한다.

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
    from .runtime import env_api_key
    from .l2_segmentation import call_hcx_l2_segmentation, call_hcx_l2_split
except ImportError:  # pragma: no cover - direct script execution
    from runtime import env_api_key
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
    generation_config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    call = (
        call_hcx_l2_split if contract == "split" else call_hcx_l2_segmentation
    )
    predictions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    usage_total = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    latency_total = 0.0
    article_runs: list[dict[str, Any]] = []
    for article in articles:
        article_idx = str(article.get("article_idx"))
        title = str(article.get("title") or article.get("기사제목") or "")
        body = str(article.get("article_text") or "")
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                call_kwargs = {"api_key": api_key, "model": model}
                if generation_config:
                    call_kwargs["generation_config"] = generation_config
                resolved, usage, latency_ms = call(title, body, **call_kwargs)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                last_error = exc
                time.sleep(pause_seconds * (attempt + 1))
                continue
            latency_total += latency_ms
            article_usage = {
                "prompt_tokens": int(usage.get("promptTokens") or 0),
                "completion_tokens": int(
                    usage.get("completionTokens") or 0
                ),
                "total_tokens": int(usage.get("totalTokens") or 0),
            }
            for key, value in article_usage.items():
                usage_total[key] += value
            article_runs.append({
                "article_idx": article_idx,
                "attempts": attempt + 1,
                "latency_ms": round(latency_ms, 1),
                **article_usage,
            })
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
            article_runs.append({
                "article_idx": article_idx,
                "attempts": retries + 1,
                "status": "CALL_FAILED",
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
        "prompt_tokens": usage_total["prompt_tokens"],
        "completion_tokens": usage_total["completion_tokens"],
        "total_tokens": usage_total["total_tokens"],
        "latency_ms_total": round(latency_total, 1),
        "article_runs": article_runs,
        "errors": errors,
        "generation_config": generation_config or {
            "temperature": 0.1,
            "top_p": 0.8,
            "seed": None,
            "max_completion_tokens": 4000,
        },
    }
    return predictions, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", default="HCX-007")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-completion-tokens", type=int, default=4000)
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
        generation_config={
            "temperature": args.temperature,
            "top_p": args.top_p,
            "seed": args.seed,
            "max_completion_tokens": args.max_completion_tokens,
        },
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
