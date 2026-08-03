"""Freeze an article-level HCX holdout before model evaluation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from .article_claim_pipeline import build_span_candidates, sentence_offset_map


EXCLUDED_DEVELOPMENT_ARTICLE_IDS = {
    "11", "871", "1021", "1159", "1163", "1384", "1486",
    "1953", "2290", "2680",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def select_holdout_seeds(evaluation_paths: list[Path]) -> list[dict[str, str]]:
    """Select every exact KOSIS seed article not used by development runs."""
    selected: dict[str, dict[str, str]] = {}
    for evaluation_path in evaluation_paths:
        with evaluation_path.open(
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            for row in reader:
                source_scope = str(row.get("gold_source_scope") or "")
                article_idx = str(row.get("article_idx") or "")
                if (
                    not source_scope.startswith("KOSIS")
                    or article_idx in EXCLUDED_DEVELOPMENT_ARTICLE_IDS
                ):
                    continue
                selected.setdefault(article_idx, {
                    "article_idx": article_idx,
                    "title": str(row.get("기사제목") or ""),
                    "seed_claim_text": str(row.get("claim_text") or ""),
                    "evaluation_set": str(row.get("evaluation_set") or ""),
                    "selection_stratum": "EXACT_KOSIS_SEED",
                })
    return sorted(
        selected.values(),
        key=lambda row: int(row["article_idx"]),
    )


def build_holdout_fixture(
    *,
    news_csv: Path,
    evaluation_paths: list[Path],
    pipeline_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    seeds = select_holdout_seeds(evaluation_paths)
    with news_csv.open(encoding="utf-8-sig", newline="") as handle:
        news_rows = list(csv.DictReader(handle))

    input_rows: list[dict[str, Any]] = []
    sentence_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    for seed in seeds:
        article_idx = int(seed["article_idx"])
        news = news_rows[article_idx]
        if str(news.get("기사제목") or "") != seed["title"]:
            raise ValueError(
                f"article {article_idx}: evaluation/news title mismatch"
            )
        article_text = str(news.get("본문_정제") or "")
        if not article_text:
            raise ValueError(f"article {article_idx}: cleaned body missing")
        article_sha256 = hashlib.sha256(
            article_text.encode("utf-8")
        ).hexdigest()
        input_rows.append({
            **seed,
            "article_sha256": article_sha256,
            "date": str(news.get("작성일") or ""),
            "url": str(news.get("URL") or ""),
            "section": str(news.get("섹션") or ""),
            "article_text": article_text,
        })
        sentences = sentence_offset_map(article_text)
        candidates = build_span_candidates(article_text)
        values_by_sentence: dict[int, list[str]] = {}
        for candidate in candidates:
            if candidate.get("kind") == "value_unit":
                values_by_sentence.setdefault(
                    int(candidate["sentence_id"]),
                    [],
                ).append(str(candidate.get("text") or ""))
                candidate_rows.append({
                    "article_idx": str(article_idx),
                    "article_sha256": article_sha256,
                    **candidate,
                })
        for sentence in sentences:
            sentence_rows.append({
                "article_idx": str(article_idx),
                "article_sha256": article_sha256,
                "title": seed["title"],
                **sentence,
                "value_candidates": values_by_sentence.get(
                    int(sentence["sentence_id"]),
                    [],
                ),
            })

    output_root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_root / "input.jsonl", input_rows)
    _write_jsonl(output_root / "sentences.jsonl", sentence_rows)
    _write_jsonl(output_root / "value_candidates.jsonl", candidate_rows)
    manifest = {
        "status": "FROZEN_PRE_EVALUATION_HOLDOUT",
        "selection_rule": (
            "validation300/final500에서 gold_source_scope가 KOSIS로 시작하는 "
            "모든 고유 article 중 기존 개발 article을 제외"
        ),
        "selection_uses_seed_claim_only": True,
        "gold_scope": (
            "선정 후 기사 전체 문장을 독립 검토하며 seed claim 외 미검출 "
            "claim도 모두 기록"
        ),
        "news_csv": str(news_csv),
        "news_csv_sha256": _sha256(news_csv),
        "evaluation_sources": [
            {
                "path": str(path),
                "sha256": _sha256(path),
            }
            for path in evaluation_paths
        ],
        "pipeline_path": str(pipeline_path),
        "pipeline_sha256": _sha256(pipeline_path),
        "excluded_development_article_ids": sorted(
            EXCLUDED_DEVELOPMENT_ARTICLE_IDS,
            key=int,
        ),
        "selected_article_ids": [
            row["article_idx"] for row in input_rows
        ],
        "article_count": len(input_rows),
        "sentence_count": len(sentence_rows),
        "value_candidate_count": len(candidate_rows),
        "files": {
            "input": "input.jsonl",
            "sentences": "sentences.jsonl",
            "value_candidates": "value_candidates.jsonl",
        },
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--news-csv", type=Path, required=True)
    parser.add_argument(
        "--evaluation",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        build_holdout_fixture(
            news_csv=args.news_csv,
            evaluation_paths=args.evaluation,
            pipeline_path=args.pipeline,
            output_root=args.output_root,
        ),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
