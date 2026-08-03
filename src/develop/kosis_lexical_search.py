"""Lexical Top-K candidate generation over the KOSIS v4 registry.

Its job is to make gold adjudication possible, not to be the retriever.  A
person cannot pick the correct table out of 265,096 by hand, so P0's human
gold is unreachable without some candidate generator; this is the cheapest one
that is fully deterministic and has no model in it.  Because it is lexical
only, whatever a later dense retriever scores can be compared against it as a
floor rather than to nothing.

Korean table names are not whitespace-separable in a useful way
(``행정구역별주민등록세대수``), so scoring uses character bigrams weighted by
inverse document frequency.  A rare bigram like ``부양`` carries the match; a
common one like ``현황`` barely moves it.

No performance claim is made here.  CLAUDE.md P0: without adjudicated gold, a
candidate agreement rate is not recall.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

INDEX_VERSION = "kosis-lexical-bigram-v1"
# The table name is what a person recognises a statistic by; the category path
# only disambiguates between similarly named tables, so it scores lower.
NAME_WEIGHT = 1.0
CATEGORY_WEIGHT = 0.35
# An indicator that appears verbatim in a table name is a different kind of
# evidence from accumulated bigram overlap, and without it long names outscore
# the exact table on sheer bigram count.
SUBSTRING_BONUS = 2.0


def bigrams(text: object) -> list[str]:
    """Character bigrams, ignoring whitespace and punctuation."""
    compact = "".join(
        char for char in str(text or "")
        if char.isalnum()
    )
    if len(compact) < 2:
        return [compact] if compact else []
    return [compact[i:i + 2] for i in range(len(compact) - 1)]


def iter_registry(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def build_index(registry_path: Path, out_path: Path) -> dict[str, Any]:
    """Reduce the registry to the fields search needs, one row per table."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for row in iter_registry(registry_path):
            table_key = str(row.get("table_key") or "")
            name = str(row.get("tbl_name") or "")
            if not table_key or not name:
                continue
            categories = [
                part
                for path in (row.get("category_paths") or [])
                for part in (path if isinstance(path, list) else [path])
            ]
            handle.write(json.dumps({
                "table_key": table_key,
                "org_id": row.get("org_id"),
                "tbl_id": row.get("tbl_id"),
                "tbl_name": name,
                "category_paths": categories,
                "profile_present": bool(row.get("profile_present")),
            }, ensure_ascii=False) + "\n")
            written += 1
    manifest = {
        "index_version": INDEX_VERSION,
        "source": str(registry_path),
        "tables": written,
    }
    return manifest


class LexicalIndex:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        document_frequency: Counter = Counter()
        weights: list[dict[str, float]] = []
        for row in rows:
            grams: dict[str, float] = defaultdict(float)
            for gram in bigrams(row.get("tbl_name")):
                grams[gram] += NAME_WEIGHT
            for category in row.get("category_paths") or []:
                for gram in bigrams(category):
                    grams[gram] += CATEGORY_WEIGHT
            weights.append(grams)
            document_frequency.update(grams.keys())
        total = max(len(rows), 1)
        self.idf = {
            gram: math.log(1 + total / (1 + count))
            for gram, count in document_frequency.items()
        }
        for position, grams in enumerate(weights):
            for gram, weight in grams.items():
                self.postings[gram].append((position, weight))

    @classmethod
    def load(cls, index_path: Path) -> "LexicalIndex":
        return cls([
            json.loads(line)
            for line in index_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ])

    def search(
        self,
        query: str,
        *,
        k: int = 10,
        extra_terms: Iterable[str] = (),
        collapse_duplicate_names: bool = True,
    ) -> list[dict[str, Any]]:
        query_text = str(query or "").strip()
        grams = Counter(bigrams(query_text))
        for term in extra_terms:
            grams.update(bigrams(term))
        if not grams:
            return []
        scores: dict[int, float] = defaultdict(float)
        for gram, count in grams.items():
            idf = self.idf.get(gram)
            if idf is None:
                continue
            for position, weight in self.postings.get(gram, ()):  # noqa: B007
                scores[position] += idf * weight * count
        # Normalising by query length keeps scores comparable between a short
        # indicator and a long one, which matters because the adjudication
        # tool shows a single cut-off across rows.
        norm = math.sqrt(sum(grams.values())) or 1.0
        compact_query = "".join(c for c in query_text if c.isalnum())
        # Rescoring more than k is what makes the substring bonus able to
        # promote, and the duplicate collapse below needs headroom too.
        ranked = sorted(scores.items(), key=lambda item: -item[1])[: k * 40]
        results = []
        for position, score in ranked:
            row = self.rows[position]
            final = score / norm
            if compact_query and compact_query in "".join(
                c for c in row["tbl_name"] if c.isalnum()
            ):
                final += SUBSTRING_BONUS
            results.append({**row, "score": final})
        results.sort(key=lambda item: -item["score"])
        if collapse_duplicate_names:
            # The registry carries the same statistic under one name many times
            # (per publishing body, per vintage).  Left alone they fill the
            # list: in the 2026-08-03 adjudication the ten candidates held only
            # 7.2 distinct names on average and as few as 3, so the person was
            # choosing from a third of the options the tool claimed to offer.
            seen: dict[str, dict[str, Any]] = {}
            for row in results:
                name = row["tbl_name"]
                if name in seen:
                    seen[name]["duplicate_tables"] += 1
                    continue
                seen[name] = {**row, "duplicate_tables": 1}
            results = list(seen.values())
        return results[:k]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--query")
    parser.add_argument("-k", type=int, default=10)
    args = parser.parse_args()

    if args.build:
        if not args.registry:
            parser.error("--build requires --registry")
        manifest = build_index(args.registry, args.index)
        if args.manifest:
            args.manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        print(json.dumps(manifest, ensure_ascii=False, indent=1))
        return

    index = LexicalIndex.load(args.index)
    for hit in index.search(args.query or "", k=args.k):
        print(f"{hit['score']:8.3f}  {hit['table_key']:22} {hit['tbl_name']}")


if __name__ == "__main__":
    main()
