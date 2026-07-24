"""Create a fixed catalog-v3-relevant 500-claim sample for hybrid retrieval."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOKEN_RE = re.compile(r"[가-힣A-Za-z]{2,}|\d+(?:\.\d+)?")


def tokens(text: str) -> set[str]:
    return {value.lower() for value in TOKEN_RE.findall(text or "")}


def load_catalog(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8-sig") if line.strip()]


def load_claims(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for index, row in enumerate(reader):
            text = str(row.get("claim_text") or "").strip()
            if text:
                rows.append({
                    "claim_id": f"claimlist-{index:06d}",
                    "claim_text": text,
                    "source_row_number": index + 2,
                    "article_idx": str(row.get("article_idx") or ""),
                    "sentence_index": str(row.get("sentence_index") or ""),
                })
    return rows


def category_for(claim_text: str, catalog: list[dict]) -> tuple[str, str, float] | None:
    query = tokens(claim_text)
    best: tuple[str, str, float] | None = None
    for table in catalog:
        overlap = query & set(table["_tokens"])
        # A category match requires a non-numeric lexical overlap. Numeric-only
        # matches would admit arbitrary accident/event sentences.
        lexical = {term for term in overlap if not term.replace(".", "").isdigit()}
        if not lexical:
            continue
        paths = table.get("category_paths") or [[]]
        path = paths[0] if paths and isinstance(paths[0], list) else []
        category = " > ".join(str(value) for value in path) or "UNCATEGORIZED"
        score = float(len(lexical))
        candidate = (category, str(table.get("table_key") or ""), score)
        if best is None or candidate[2] > best[2] or (candidate[2] == best[2] and candidate[1] < best[1]):
            best = candidate
    return best


def stratified_sample(rows: list[dict], size: int, seed: int) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["matched_category_path"]].append(row)
    rng = random.Random(seed)
    for group in grouped.values():
        rng.shuffle(group)
    selected: list[dict] = []
    categories = sorted(grouped)
    while len(selected) < size:
        progressed = False
        for category in categories:
            if grouped[category] and len(selected) < size:
                selected.append(grouped[category].pop())
                progressed = True
        if not progressed:
            break
    if len(selected) < size:
        raise ValueError(f"only {len(selected)} catalog-relevant claims available; requested {size}")
    return sorted(selected, key=lambda row: row["claim_id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, default=ROOT / "data" / "claim_listform.csv")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "kosis_catalog_v3.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "hybrid_v3_500_20260723" / "sample_claims_500.jsonl")
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260723)
    args = parser.parse_args()

    catalog = load_catalog(args.catalog)
    for table in catalog:
        table["_tokens"] = tokens(str(table.get("doc_meta_text") or ""))
    candidates = []
    for row in load_claims(args.claims):
        match = category_for(row["claim_text"], catalog)
        if match is None:
            continue
        row["matched_category_path"], row["matched_table_key_seed"], row["category_match_score"] = match
        candidates.append(row)
    sample = stratified_sample(candidates, args.sample_size, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in sample:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    csv_path = args.output.with_suffix(".csv")
    csv_rows = [{"eval_claim_id": row["claim_id"], **row} for row in sample]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    print(json.dumps({"catalog_documents": len(catalog), "eligible_claims": len(candidates), "sample_size": len(sample), "output": str(args.output), "hcx_input": str(csv_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
