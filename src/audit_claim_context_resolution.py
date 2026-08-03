"""문맥 resolver의 실제 기사 적용 범위와 모호성 분포를 별도 산출물로 측정한다."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .claim_extractor import extract_from_article
except ImportError:  # pragma: no cover - standalone CLI support
    from claim_extractor import extract_from_article


ROOT = Path(__file__).resolve().parent.parent


def summarize_context_rows(rows: list[dict[str, Any]], *, sample_limit: int = 5) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            context = json.loads(str(row.get("context_resolution_json") or "{}"))
        except json.JSONDecodeError:
            context = {"status": "CONTEXT_MISSING"}
        status = str(context.get("status") or "CONTEXT_MISSING")
        counts[status] += 1
        if len(samples.setdefault(status, [])) < sample_limit:
            samples[status].append({
                "article_idx": row.get("article_idx"), "sentence_index": row.get("sentence_index"),
                "claim_text": row.get("claim_text"), "resolved_terms": context.get("resolved_terms", []),
                "candidate_terms": context.get("candidate_terms", []),
            })
    total = len(rows)
    unresolved = counts["REFERENT_CANDIDATE"] + counts["REFERENT_AMBIGUOUS"] + counts["CONTEXT_MISSING"]
    return {
        "claim_rows": total,
        "status_counts": dict(sorted(counts.items())),
        "context_required_rows": total - counts["NOT_APPLICABLE"] - counts["EXPLICIT"],
        "unresolved_rows": unresolved,
        "unresolved_rate": round(unresolved / total, 4) if total else 0.0,
        "samples": samples,
    }


def audit_frame(frame: pd.DataFrame) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    required = {"기사제목", "작성일", "검색 구분 레이블", "본문_정제"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"input missing columns: {sorted(missing)}")
    for article_idx, row in frame.iterrows():
        text = row.get("본문_정제")
        if isinstance(text, str) and text.strip():
            rows.extend(extract_from_article(article_idx, row["기사제목"], row["작성일"], row["검색 구분 레이블"], text))
    return summarize_context_rows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="claim 문맥 보강 적용 분포 audit")
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "news_preprocessed.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-limit", type=int, default=5)
    args = parser.parse_args()
    result = audit_frame(pd.read_csv(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "samples"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
