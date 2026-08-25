"""Gold-blind article-date provenance for R4-C1 period anchoring."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTICLE_SOURCE = ROOT / "data/raw/AI_기반_뉴스_사실검증_시스템_프로젝트_데이터.csv"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def with_article_date_context(
    routed_rows: Sequence[Mapping[str, Any]], article_source_path: str | Path | None
) -> list[dict[str, Any]]:
    """Join raw article dates only when the routed sentence is reproducible."""

    if article_source_path is None:
        return [dict(row) for row in routed_rows]
    source = Path(article_source_path)
    with source.open(encoding="utf-8-sig", newline="") as handle:
        articles = list(csv.DictReader(handle))
    source_sha = _sha256_file(source)
    enriched: list[dict[str, Any]] = []
    for routed in routed_rows:
        article_idx = str(routed.get("article_idx") or "")
        if not article_idx.isdigit() or int(article_idx) >= len(articles):
            raise ValueError(f"raw article join failed: article_idx={article_idx!r}")
        source_row = articles[int(article_idx)]
        sentence = str(routed.get("sentence_text") or "")
        article_text = str(source_row.get("기사 본문 전체") or "")
        article_date = str(source_row.get("작성일") or "").strip()
        row = dict(routed)
        if sentence and sentence in article_text and article_date:
            row["article_date"] = article_date
            row["article_date_provenance"] = {
                "source_path": str(source.resolve()),
                "source_sha256": source_sha,
                "row_index": int(article_idx),
                "date_field": "작성일",
                "article_text_sha256": hashlib.sha256(article_text.encode("utf-8")).hexdigest(),
            }
        enriched.append(row)
    return enriched


__all__ = ["DEFAULT_ARTICLE_SOURCE", "with_article_date_context"]
