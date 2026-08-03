"""Build the routing-gold judgement list for a frozen split.

Routing gold is one binary-ish decision per value candidate — is this figure a
KOSIS-verifiable statistic — so the scaffold carries only what that decision
needs: the value, its sentence, and enough neighbouring sentences to see where
the source was attributed.  Korean articles name the source once and then omit
it, so a value's sentence alone is usually not enough to judge.

No model output is included.  The judgement becomes the gold that grades the
model, so seeing a prediction first would make the measurement circular.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from .l1_value_candidates import build_span_candidates, sentence_offset_map
except ImportError:  # pragma: no cover - direct script execution
    from l1_value_candidates import build_span_candidates, sentence_offset_map


CONTEXT_BEFORE = 2
CONTEXT_AFTER = 1
# Phrases that mark where a figure's source is stated.  Highlighted as a
# reading aid so the reviewer can find the attribution quickly; it is not a
# suggested answer and never pre-fills the decision.
ATTRIBUTION_RE = re.compile(
    r"(?:[가-힣A-Za-z0-9]+(?:에|은|는|이|가)?\s*)?"
    r"(?:에\s*따르면|가\s*발표한|이\s*발표한|가\s*집계한|조사한\s*결과|"
    r"발표했다|밝혔다|집계됐다|나타났다|조사에\s*따르면)"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_scaffold(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for article in articles:
        article_idx = str(article.get("article_idx"))
        body = str(article.get("article_text") or "")
        sentences = sentence_offset_map(body)
        by_id = {row["sentence_id"]: row["text"] for row in sentences}
        # ``build_span_candidates`` offsets are article-relative; the UI
        # highlights inside a single sentence, so they are rebased here.
        origins = {row["sentence_id"]: row["char_start"] for row in sentences}
        candidates = [
            candidate for candidate in build_span_candidates(body)
            if candidate.get("kind") == "value_unit"
        ]
        for candidate in candidates:
            sentence_id = candidate["sentence_id"]
            text = by_id.get(sentence_id, "")
            context = []
            for offset in range(-CONTEXT_BEFORE, CONTEXT_AFTER + 1):
                neighbour = sentence_id + offset
                if neighbour == sentence_id or neighbour not in by_id:
                    continue
                context.append({
                    "sentence_id": neighbour,
                    "text": by_id[neighbour],
                    "position": "before" if offset < 0 else "after",
                    "has_attribution": bool(
                        ATTRIBUTION_RE.search(by_id[neighbour])
                    ),
                })
            rows.append({
                "judgement_id": f"{article_idx}:{candidate['span_id']}",
                "article_idx": article_idx,
                "article_title": article.get("title"),
                "published_at": article.get("date"),
                "sentence_id": sentence_id,
                "value_span_id": candidate["span_id"],
                "value_text": candidate.get("text"),
                "value_char_start": candidate["char_start"] - origins[sentence_id],
                "value_char_end": candidate["char_end"] - origins[sentence_id],
                "sentence_text": text,
                "sentence_has_attribution": bool(ATTRIBUTION_RE.search(text)),
                "context": context,
                "article_sha256": _sha256(body),
                "judged_class": "",
                "judge_note": "",
                "review_status": "미검토",
            })
    return rows


def scaffold_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    return {
        "candidates": len(rows),
        "articles": len({row["article_idx"] for row in rows}),
        "per_article": dict(
            Counter(row["article_idx"] for row in rows)
        ),
        "sentences_with_attribution": sum(
            1 for row in rows if row["sentence_has_attribution"]
        ),
        "classes": ["KOSIS_CANDIDATE", "OUT_OF_SCOPE", "NOT_CLAIM"],
        "contains_model_output": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    articles = [
        json.loads(line)
        for line in args.articles.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = build_scaffold(articles)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary = scaffold_summary(rows)
    if args.summary:
        args.summary.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
