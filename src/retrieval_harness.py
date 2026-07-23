"""Offline lexical retrieval harness for normalized KOSIS catalog records.

This is a candidate-generation baseline only.  It deliberately does not call
KOSIS or an embedding service, and it does not calculate Recall until gold
table IDs are adjudicated.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CLAIMS = ROOT / "data" / "retrieval_eval_claims_v0_codex.csv"
DEFAULT_CATALOG = ROOT / "data" / "kosis_catalog_v3.jsonl"
DEFAULT_OUTPUT = ROOT / "data" / "retrieval_candidates_v0_codex.jsonl"

TOKEN_RE = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9·_-]{1,}|\d+(?:\.\d+)?")


def tokens(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def load_catalog(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def score(query_tokens: list[str], record: dict, idf: dict[str, float]) -> float:
    document_tokens = tokens(record.get("document_text", ""))
    counts = Counter(document_tokens)
    overlap = set(query_tokens) & set(document_tokens)
    if not overlap:
        return 0.0
    value = sum(idf.get(token, 1.0) * (1.0 + math.log1p(counts[token])) for token in overlap)
    title = record.get("tbl_name", "")
    query_text = " ".join(query_tokens)
    if any(token in title.lower() for token in set(query_tokens) if len(token) >= 3):
        value += 0.5
    if query_text and query_text in record.get("document_text", "").lower():
        value += 1.0
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args()

    import pandas as pd

    claims = pd.read_csv(args.claims, keep_default_na=False)
    claims = claims[claims["gold_verifiability_prefilter"] == "검증시도"]
    catalog = load_catalog(args.catalog)

    by_org: defaultdict[str, list[dict]] = defaultdict(list)
    document_frequency: Counter[str] = Counter()
    for record in catalog:
        by_org[record.get("org_id", "")].append(record)
        document_frequency.update(set(tokens(record.get("document_text", ""))))
    total_docs = max(len(catalog), 1)
    idf = {
        token: math.log((total_docs + 1) / (frequency + 1)) + 1.0
        for token, frequency in document_frequency.items()
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for _, claim in claims.iterrows():
            query_tokens = tokens(str(claim.get("claim_text", "")))
            org_id = str(claim.get("gold_org_id", ""))
            pool = by_org.get(org_id) or catalog
            ranked = sorted(
                ((score(query_tokens, record, idf), record) for record in pool),
                key=lambda item: (-item[0], item[1].get("table_key", "")),
            )[: args.top_k]
            payload = {
                "eval_claim_id": claim.get("eval_claim_id"),
                "article_idx": int(claim.get("article_idx")),
                "claim_text": claim.get("claim_text"),
                "org_id_filter": org_id or None,
                "retrieval_stage": "lexical_org_filtered",
                "candidates": [
                    {
                        "rank": rank,
                        "table_key": record.get("table_key"),
                        "org_id": record.get("org_id"),
                        "tbl_id": record.get("tbl_id"),
                        "tbl_name": record.get("tbl_name"),
                        "stat_id": record.get("stat_id"),
                        "score": round(float(value), 6),
                        "category_paths": record.get("category_paths", []),
                    }
                    for rank, (value, record) in enumerate(ranked, start=1)
                ],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            written += 1

    print(json.dumps({"claims": written, "catalog": len(catalog), "top_k": args.top_k}, ensure_ascii=False))


if __name__ == "__main__":
    main()
