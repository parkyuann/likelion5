"""Compare regex and Korean-morphology BM25 retrieval on KOSIS claims.

The script samples claims deterministically with reservoir sampling, builds one
BM25 index at a time over the KOSIS catalog, and writes rankings and a summary.
Kiwi is imported only when the morphology experiment starts.

Example (run from the project root):

    python bm25_morph_experiment.py \
      --claims data/claims_v1.jsonl \
      --catalog data/kosis_catalog_v1.jsonl \
      --output-dir data/experiments/bm25_morph_300

Gold mappings are optional. Without ``--gold``, rankings and retrieval
diagnostics are produced but Recall@N is reported as unavailable.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import heapq
import json
import math
import random
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


TOKEN_RE = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9·_-]*|\d+(?:\.\d+)?")
DEFAULT_RECALL_AT = (5, 10, 20)
KEEP_POS_PREFIXES = ("NN", "NR", "NP", "VV", "VA", "XR", "MM", "SL", "SN")


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def regex_tokens(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(clean(text))]


class KiwiHybridTokenizer:
    """Keep exact surface tokens and useful Kiwi morphemes in two channels."""

    def __init__(self, user_dictionary: Path | None = None):
        try:
            from kiwipiepy import Kiwi
        except ImportError as exc:
            raise RuntimeError(
                "형태소 실험에는 kiwipiepy가 필요합니다: python -m pip install kiwipiepy"
            ) from exc
        # -1 lets Kiwi choose its worker pool and supports iterable batch input.
        self.kiwi = Kiwi(num_workers=-1)
        if user_dictionary:
            for line in user_dictionary.read_text(encoding="utf-8-sig").splitlines():
                word = line.strip()
                if word and not word.startswith("#"):
                    self.kiwi.add_user_word(word, "NNP")

    def __call__(self, text: str) -> list[str]:
        normalized = clean(text)
        surface = [f"s:{token}" for token in regex_tokens(normalized)]
        morphology = [
            f"m:{token.form.lower()}"
            for token in self.kiwi.tokenize(normalized)
            if token.tag.startswith(KEEP_POS_PREFIXES) and clean(token.form)
        ]
        return surface + morphology

    def tokenize_many(self, texts: Sequence[str]) -> list[list[str]]:
        """Analyze a batch so Kiwi can use its worker pool efficiently."""
        normalized = [clean(text) for text in texts]
        analyzed = self.kiwi.tokenize(normalized)
        return [
            [f"s:{token}" for token in regex_tokens(text)]
            + [
                f"m:{token.form.lower()}"
                for token in tokens
                if token.tag.startswith(KEEP_POS_PREFIXES) and clean(token.form)
            ]
            for text, tokens in zip(normalized, analyzed)
        ]


def unwrap_claim(payload: Mapping[str, Any]) -> dict[str, Any]:
    return dict(payload.get("claim", payload))


def reservoir_sample_claims(path: Path, sample_size: int, seed: int) -> list[dict[str, Any]]:
    if sample_size < 1:
        raise ValueError("sample_size must be >= 1")
    rng = random.Random(seed)
    sample: list[dict[str, Any]] = []
    seen = 0
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            claim = unwrap_claim(json.loads(line))
            seen += 1
            if len(sample) < sample_size:
                sample.append(claim)
            else:
                replacement = rng.randrange(seen)
                if replacement < sample_size:
                    sample[replacement] = claim
    if seen < sample_size:
        raise ValueError(f"requested {sample_size} claims, but input contains {seen}")
    return sorted(sample, key=lambda row: clean(row.get("claim_id")))


def claim_query_text(claim: Mapping[str, Any]) -> str:
    indicator = clean(claim.get("indicator_norm") or claim.get("indicator_raw"))
    population = clean(claim.get("population_norm") or claim.get("population_raw"))
    preferred = " ".join(value for value in (indicator, population) if value)
    return preferred or clean(claim.get("claim_text"))


def claim_org_ids(claim: Mapping[str, Any]) -> set[str]:
    return {
        clean(item.get("org_id"))
        for item in claim.get("attributions", [])
        if isinstance(item, Mapping) and clean(item.get("org_id"))
    }


def catalog_document(record: Mapping[str, Any]) -> str:
    item_text = clean(record.get("doc_item_index"))
    if item_text:
        return item_text
    # v1 document_text already contains tbl_name. Do not duplicate it here.
    return clean(record.get("document_text")) or clean(record.get("tbl_name"))


@dataclass(frozen=True)
class DocumentMeta:
    table_key: str
    org_id: str
    tbl_name: str


class BM25InvertedIndex:
    """Memory-conscious inverted Okapi BM25 index for short catalog documents."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents: list[DocumentMeta] = []
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.idf: dict[str, float] = {}
        self.avg_length = 0.0

    @classmethod
    def build(
        cls,
        catalog_path: Path,
        tokenizer: Callable[[str], list[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        catalog_limit: int | None = None,
        progress_every: int = 25_000,
    ) -> "BM25InvertedIndex":
        index = cls(k1=k1, b=b)
        started = time.perf_counter()
        def add_records(records: Sequence[Mapping[str, Any]]) -> None:
            texts = [catalog_document(record) for record in records]
            tokenize_many = getattr(tokenizer, "tokenize_many", None)
            token_rows = tokenize_many(texts) if tokenize_many else [tokenizer(text) for text in texts]
            for record, tokens in zip(records, token_rows):
                table_key = clean(record.get("table_key"))
                doc_id = len(index.documents)
                index.documents.append(DocumentMeta(
                    table_key=table_key,
                    org_id=clean(record.get("org_id")),
                    tbl_name=clean(record.get("tbl_name")),
                ))
                counts = Counter(tokens)
                index.lengths.append(sum(counts.values()))
                for term, frequency in counts.items():
                    index.postings[term].append((doc_id, frequency))

        batch: list[dict[str, Any]] = []
        with catalog_path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                table_key = clean(record.get("table_key"))
                if not table_key:
                    continue
                batch.append(record)
                reached_limit = catalog_limit is not None and len(index.documents) + len(batch) >= catalog_limit
                if len(batch) >= 2_048 or reached_limit:
                    add_records(batch)
                    batch.clear()
                if progress_every and len(index.documents) and len(index.documents) % progress_every == 0:
                    elapsed = time.perf_counter() - started
                    print(
                        f"indexed={len(index.documents):,} elapsed_sec={elapsed:.1f}",
                        file=sys.stderr,
                        flush=True,
                    )
                if reached_limit:
                    break
        if batch:
            add_records(batch)
        size = len(index.documents)
        index.avg_length = sum(index.lengths) / size if size else 0.0
        index.idf = {
            term: math.log(1.0 + (size - len(rows) + 0.5) / (len(rows) + 0.5))
            for term, rows in index.postings.items()
        }
        return index

    def search(
        self,
        query: str,
        tokenizer: Callable[[str], list[str]],
        limit: int,
        eligible_org_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        query_terms = list(dict.fromkeys(tokenizer(query)))
        scores: defaultdict[int, float] = defaultdict(float)
        for term in query_terms:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc_id, frequency in self.postings.get(term, []):
                meta = self.documents[doc_id]
                if eligible_org_ids and meta.org_id not in eligible_org_ids:
                    continue
                length = self.lengths[doc_id]
                norm = self.k1 * (
                    1.0 - self.b + self.b * length / self.avg_length
                ) if self.avg_length else self.k1
                scores[doc_id] += idf * frequency * (self.k1 + 1.0) / (frequency + norm)

        best = heapq.nsmallest(
            limit,
            scores.items(),
            key=lambda item: (-item[1], self.documents[item[0]].table_key),
        )
        hits = [
            {
                "rank": rank,
                "table_key": self.documents[doc_id].table_key,
                "org_id": self.documents[doc_id].org_id,
                "tbl_name": self.documents[doc_id].tbl_name,
                "score": round(float(score), 8),
            }
            for rank, (doc_id, score) in enumerate(best, start=1)
            if score > 0
        ]
        return hits, query_terms


def load_gold(path: Path | None) -> dict[str, set[str]]:
    if path is None:
        return {}
    gold: defaultdict[str, set[str]] = defaultdict(set)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows: Iterable[Mapping[str, Any]] = csv.DictReader(handle)
            for row in rows:
                claim_id = clean(row.get("claim_id") or row.get("eval_claim_id"))
                table_key = clean(row.get("table_key"))
                if not table_key:
                    org_id = clean(row.get("gold_org_id") or row.get("org_id"))
                    tbl_id = clean(row.get("gold_tbl_id") or row.get("tbl_id"))
                    table_key = f"{org_id}:{tbl_id}" if org_id and tbl_id else ""
                if claim_id and table_key:
                    gold[claim_id].add(table_key)
    else:
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                row = payload.get("mapping", payload)
                claim_id = clean(row.get("claim_id"))
                table_key = clean(row.get("table_key"))
                accepted = bool(row.get("is_gold")) or row.get("status") == "SELECTED"
                if claim_id and table_key and accepted:
                    gold[claim_id].add(table_key)
    return dict(gold)


def evaluate_recall(
        rankings: Mapping[str, Sequence[Mapping[str, Any]]],
        gold: Mapping[str, set[str]],
        at: Sequence[int],
) -> dict[str, Any]:
    evaluated = [claim_id for claim_id in rankings if gold.get(claim_id)]
    metrics: dict[str, Any] = {"gold_claims": len(evaluated)}
    if not evaluated:
        metrics["status"] = "unavailable: no sampled claims have gold table mappings"
        return metrics
    for n in at:
        hits = 0
        for claim_id in evaluated:
            retrieved = {row["table_key"] for row in rankings[claim_id][:n]}
            hits += bool(retrieved & gold[claim_id])
        metrics[f"recall@{n}"] = round(hits / len(evaluated), 6)
    return metrics


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_mode(
    mode: str,
    tokenizer: Callable[[str], list[str]],
    claims: Sequence[Mapping[str, Any]],
    catalog: Path,
    output_dir: Path,
    top_n: int,
    use_claim_org: bool,
    catalog_limit: int | None,
    gold: Mapping[str, set[str]],
    recall_at: Sequence[int],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    build_started = time.perf_counter()
    index = BM25InvertedIndex.build(
        catalog,
        tokenizer,
        catalog_limit=catalog_limit,
    )
    build_seconds = time.perf_counter() - build_started
    ranking_rows = []
    rankings: dict[str, list[dict[str, Any]]] = {}
    search_started = time.perf_counter()
    empty_results = 0
    for claim in claims:
        claim_id = clean(claim.get("claim_id"))
        query = claim_query_text(claim)
        org_ids = claim_org_ids(claim) if use_claim_org else set()
        hits, query_terms = index.search(
            query,
            tokenizer,
            top_n,
            eligible_org_ids=org_ids or None,
        )
        rankings[claim_id] = hits
        empty_results += not hits
        ranking_rows.append({
            "claim_id": claim_id,
            "claim_text": clean(claim.get("claim_text")),
            "query_text": query,
            "query_tokens": query_terms,
            "org_id_filter": sorted(org_ids) if org_ids else None,
            "tokenizer": mode,
            "candidates": hits,
        })
    search_seconds = time.perf_counter() - search_started
    write_jsonl(output_dir / f"rankings_{mode}.jsonl", ranking_rows)
    summary = {
        "mode": mode,
        "catalog_documents": len(index.documents),
        "vocabulary_size": len(index.postings),
        "average_document_tokens": round(index.avg_length, 4),
        "index_build_seconds": round(build_seconds, 3),
        "search_seconds": round(search_seconds, 3),
        "mean_search_ms": round(search_seconds * 1000 / len(claims), 3),
        "empty_result_claims": empty_results,
        "recall": evaluate_recall(rankings, gold, recall_at),
    }
    del index
    gc.collect()
    return summary, rankings


def compare_rankings(
    left: Mapping[str, Sequence[Mapping[str, Any]]],
    right: Mapping[str, Sequence[Mapping[str, Any]]],
    top_n: int,
) -> dict[str, Any]:
    claim_ids = sorted(set(left) & set(right))
    overlaps = []
    top1_changed = 0
    left_empty_recovered = 0
    for claim_id in claim_ids:
        left_keys = [row["table_key"] for row in left[claim_id][:top_n]]
        right_keys = [row["table_key"] for row in right[claim_id][:top_n]]
        left_set, right_set = set(left_keys), set(right_keys)
        union = left_set | right_set
        overlaps.append(len(left_set & right_set) / len(union) if union else 1.0)
        top1_changed += bool(left_keys and right_keys and left_keys[0] != right_keys[0])
        left_empty_recovered += bool(not left_keys and right_keys)
    return {
        "claims": len(claim_ids),
        f"mean_top_{top_n}_jaccard": round(sum(overlaps) / len(overlaps), 6) if overlaps else 0.0,
        "top1_changed_claims": top1_changed,
        "regex_empty_recovered_by_kiwi_hybrid": left_empty_recovered,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--recall-at", type=int, nargs="+", default=list(DEFAULT_RECALL_AT))
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--user-dictionary", type=Path)
    parser.add_argument("--use-claim-org", action="store_true")
    parser.add_argument("--catalog-limit", type=int, help="Smoke-test only; omit for full catalog")
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("regex", "kiwi_hybrid"),
        default=["regex", "kiwi_hybrid"],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if max(args.recall_at) > args.top_n:
        raise ValueError("top_n must be >= every recall-at value")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    claims = reservoir_sample_claims(args.claims, args.sample_size, args.seed)
    sample_digest = hashlib.sha256(
        "\n".join(clean(row.get("claim_id")) for row in claims).encode("utf-8")
    ).hexdigest()
    write_jsonl(args.output_dir / f"sample_claims_{len(claims)}.jsonl", claims)
    gold = load_gold(args.gold)

    summaries = []
    rankings_by_mode: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for mode in args.modes:
        tokenizer: Callable[[str], list[str]]
        if mode == "regex":
            tokenizer = regex_tokens
        else:
            tokenizer = KiwiHybridTokenizer(args.user_dictionary)
        mode_summary, mode_rankings = run_mode(
            mode=mode,
            tokenizer=tokenizer,
            claims=claims,
            catalog=args.catalog,
            output_dir=args.output_dir,
            top_n=args.top_n,
            use_claim_org=args.use_claim_org,
            catalog_limit=args.catalog_limit,
            gold=gold,
            recall_at=args.recall_at,
        )
        summaries.append(mode_summary)
        rankings_by_mode[mode] = mode_rankings
        del tokenizer
        gc.collect()

    comparison = None
    if "regex" in rankings_by_mode and "kiwi_hybrid" in rankings_by_mode:
        comparison = compare_rankings(
            rankings_by_mode["regex"], rankings_by_mode["kiwi_hybrid"], args.top_n
        )
    summary = {
        "experiment": "bm25-regex-vs-kiwi-hybrid",
        "claims": str(args.claims),
        "catalog": str(args.catalog),
        "sample_size": len(claims),
        "seed": args.seed,
        "sample_claim_ids_sha256": sample_digest,
        "top_n": args.top_n,
        "recall_at": args.recall_at,
        "gold_source": str(args.gold) if args.gold else None,
        "gold_mappings": sum(len(values) for values in gold.values()),
        "use_claim_org": args.use_claim_org,
        "catalog_limit": args.catalog_limit,
        "results": summaries,
        "comparison": comparison,
    }
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
