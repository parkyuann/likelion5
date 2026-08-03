"""규칙 resolver가 확정하지 못한 claim을 HCX/사람 문맥 판정 fixture로 만든다."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .claim_context_resolver import CONTEXT_WINDOW_SENTENCES
    from .claim_extractor import extract_from_article, iter_sentence_spans
except ImportError:  # pragma: no cover - standalone CLI support
    from claim_context_resolver import CONTEXT_WINDOW_SENTENCES
    from claim_extractor import extract_from_article, iter_sentence_spans


ROOT = Path(__file__).resolve().parent.parent
FIXTURE_STATUSES = {"REFERENT_CANDIDATE", "REFERENT_AMBIGUOUS", "CONTEXT_MISSING"}


def clean_context_window(sentences: list[tuple[int, int, int, str]], sentence_index: int) -> list[dict[str, Any]]:
    start = max(0, sentence_index - CONTEXT_WINDOW_SENTENCES)
    return [
        {"sentence_index": index, "text": text}
        for index, _, _, text in sentences
        if start <= index <= sentence_index
    ]


def fixture_row(article_idx: int, title: str, row: dict[str, Any], sentences: list[tuple[int, int, int, str]]) -> dict[str, str]:
    resolution = json.loads(str(row["context_resolution_json"]))
    sentence_index = int(row["sentence_index"])
    fingerprint = f"{article_idx}|{sentence_index}|{row['claim_text']}"
    return {
        "context_eval_id": f"context_{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]}",
        "article_idx": str(article_idx), "sentence_index": str(sentence_index), "article_title": str(title),
        "claim_text": str(row["claim_text"]), "rule_context_status": str(resolution["status"]),
        "candidate_terms_json": json.dumps(resolution.get("candidate_terms", []), ensure_ascii=False),
        "evidence_json": json.dumps(resolution.get("evidence", []), ensure_ascii=False),
        "context_window_json": json.dumps(clean_context_window(sentences, sentence_index), ensure_ascii=False),
        # HCX 또는 사람 검토자가 채우는 결정 필드. 둘을 분리해 원본 규칙 결과를 덮어쓰지 않는다.
        "adjudication_status": "", "selected_referent": "", "evidence_sentence_index": "",
        "adjudication_source": "", "adjudication_notes": "", "review_status": "pending",
    }


def build_fixture(frame: pd.DataFrame) -> list[dict[str, str]]:
    required = {"기사제목", "작성일", "검색 구분 레이블", "본문_정제"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"input missing columns: {sorted(missing)}")
    rows: list[dict[str, str]] = []
    for article_idx, article in frame.iterrows():
        text = article.get("본문_정제")
        if not isinstance(text, str) or not text.strip():
            continue
        sentences = list(iter_sentence_spans(text))
        extracted = extract_from_article(article_idx, article["기사제목"], article["작성일"], article["검색 구분 레이블"], text)
        for row in extracted:
            resolution = json.loads(str(row["context_resolution_json"]))
            if resolution.get("status") in FIXTURE_STATUSES:
                rows.append(fixture_row(article_idx, article["기사제목"], row, sentences))
    return sorted(rows, key=lambda row: (int(row["article_idx"]), int(row["sentence_index"]), row["claim_text"]))


def write_fixture(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="HCX/사람 문맥 referent 판정 fixture 생성")
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "news_preprocessed.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    rows = build_fixture(pd.read_csv(args.input))
    write_fixture(args.output, rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["rule_context_status"]] = counts.get(row["rule_context_status"], 0) + 1
    manifest = {"rows": len(rows), "rule_context_status_counts": counts, "input": str(args.input), "output": str(args.output),
                "requires_evidence_for_resolution": True, "not_a_retrieval_metric": True}
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
