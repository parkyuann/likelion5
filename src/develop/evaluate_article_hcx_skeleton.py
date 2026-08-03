"""Evaluate a skeleton-contract HCX run against the fixed 5-article gold."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .article_claim_pipeline import build_span_candidates
from .article_hcx_gold_fixture import load_jsonl


def _key(value: object) -> str:
    text = re.sub(r"\([^)]*\)", "", str(value or ""))
    text = re.sub(r"\s+", "", text)
    for suffix in (
        "전년동월비증감률", "전월비증감률", "전년동기대비증감률",
        "상승률", "하락률", "성장률", "증감률", "증가율", "감소율", "변화율",
    ):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _one_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(run_root: Path, fixture_rows: list[dict[str, Any]]) -> dict[str, Any]:
    raw_by_article: dict[str, list[dict[str, Any]]] = {}
    passed_by_article: dict[str, list[dict[str, Any]]] = {}
    value_candidates_by_article: dict[str, dict[str, dict[str, Any]]] = {}
    if (run_root / "raw.jsonl").exists():
        raw_rows = _one_jsonl(run_root / "raw.jsonl")
        passed_rows = _one_jsonl(run_root / "pass_observations.jsonl")
        input_rows = _one_jsonl(run_root / "input.jsonl")
        for raw_row in raw_rows:
            article_idx = str(raw_row["article_idx"])
            if article_idx in raw_by_article:
                raise ValueError(f"duplicate raw article row: {article_idx}")
            raw_by_article[article_idx] = raw_row.get("semantic_prediction", {}).get("claims", [])
        for passed_row in passed_rows:
            article_idx = str(passed_row.get("article_idx") or "")
            passed_by_article.setdefault(article_idx, []).append(passed_row)
        for input_row in input_rows:
            article_idx = str(input_row.get("article_idx") or "")
            value_candidates_by_article[article_idx] = {
                candidate["span_id"]: candidate
                for candidate in build_span_candidates(str(input_row.get("article_text") or ""))
                if candidate.get("kind") == "value_unit"
            }
    else:
        for directory in run_root.iterdir():
            if not directory.is_dir():
                continue
            raw_rows = _one_jsonl(directory / "raw.jsonl")
            if len(raw_rows) != 1:
                raise ValueError(f"expected one raw row: {directory}")
            article_idx = str(raw_rows[0]["article_idx"])
            raw_by_article[article_idx] = raw_rows[0].get("semantic_prediction", {}).get("claims", [])
            passed_by_article[article_idx] = _one_jsonl(directory / "pass_observations.jsonl")
            input_rows = _one_jsonl(directory / "input.jsonl")
            if len(input_rows) != 1:
                raise ValueError(f"expected one input row: {directory}")
            value_candidates_by_article[article_idx] = {
                candidate["span_id"]: candidate
                for candidate in build_span_candidates(str(input_rows[0].get("article_text") or ""))
                if candidate.get("kind") == "value_unit"
            }

    eligible = [row for row in fixture_rows if row.get("eligibility") == "KOSIS_CANDIDATE"]
    rows: list[dict[str, Any]] = []
    for gold in eligible:
        article_idx = str(gold["article_idx"])
        expected_key = _key(gold["indicator_norm"])
        skeletons = [
            claim for claim in raw_by_article.get(article_idx, [])
            if _key(claim.get("indicator_norm")) == expected_key
            and gold["value_sentence_id"] in claim.get("observation_sentence_ids", [])
        ]
        raw_target_candidate_match = any(
            candidate.get("text") == gold["value_text"]
            and candidate.get("sentence_id") == gold["value_sentence_id"]
            for claim in raw_by_article.get(article_idx, [])
            for span_id in claim.get("target_value_span_ids", [])
            for candidate in [value_candidates_by_article.get(article_idx, {}).get(str(span_id), {})]
        )
        source_passed = [
            item for item in passed_by_article.get(article_idx, [])
            if item.get("validation", {}).get("value_span", {}).get("text") == gold["value_text"]
            and item.get("validation", {}).get("value_span", {}).get("sentence_id") == gold["value_sentence_id"]
        ]
        passed = [
            item for item in passed_by_article.get(article_idx, [])
            if _key(item.get("semantic_claim", {}).get("indicator_norm")) == expected_key
            and item.get("validation", {}).get("value_span", {}).get("text") == gold["value_text"]
            and item.get("validation", {}).get("value_span", {}).get("sentence_id") == gold["value_sentence_id"]
        ]
        value_match = bool(passed)
        measurement_match = any(item.get("validation", {}).get("measurement_type") == gold["measurement_type"] for item in passed)
        source_measurement_match = any(
            item.get("validation", {}).get("measurement_type") == gold["measurement_type"]
            for item in source_passed
        )
        period = gold.get("period") or {}
        period_match = bool(passed) and (
            not period
            or any(
                (item.get("validation", {}).get("period_span") or {}).get("text") == period.get("text")
                for item in passed
            )
        )
        source_period_match = bool(source_passed) and (
            not period
            or any(
                (item.get("validation", {}).get("period_span") or {}).get("text")
                == period.get("text")
                for item in source_passed
            )
        )
        rows.append({
            "fixture_id": gold["fixture_id"], "article_idx": article_idx,
            "raw_target_candidate_match": raw_target_candidate_match,
            "skeleton_match": bool(skeletons), "source_value_match": bool(source_passed), "value_match": value_match,
            "measurement_type_match": measurement_match,
            "period_match": period_match,
            "source_measurement_type_match": source_measurement_match,
            "source_period_match": source_period_match,
        })
    totals = {
        field: sum(bool(row[field]) for row in rows)
        for field in (
            "skeleton_match",
            "raw_target_candidate_match",
            "source_value_match",
            "value_match",
            "measurement_type_match",
            "period_match",
            "source_measurement_type_match",
            "source_period_match",
        )
    }
    return {
        "eligible_gold_rows": len(rows),
        "metrics": {field: {"matched": count, "recall": count / len(rows) if rows else None} for field, count in totals.items()},
        "misses_by_article": dict(Counter(row["article_idx"] for row in rows if not row["value_match"])),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate HCX skeleton run against gold fixture")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.run_root, load_jsonl(args.fixture))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"], ensure_ascii=False))


if __name__ == "__main__":
    main()
