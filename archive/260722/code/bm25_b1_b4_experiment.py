"""Run B1-B4 BM25 query-representation experiments on KOSIS catalog."""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bm25_morph_experiment import (  # noqa: E402
    BM25InvertedIndex,
    clean,
    evaluate_recall,
    load_gold,
    regex_tokens,
    reservoir_sample_claims,
    write_json,
    write_jsonl,
)


KEEP_ALL = ("NN", "NR", "NP", "VV", "VA", "VX", "XR", "MM", "SL", "SN")
NOMINAL = ("NN", "NR", "NP", "SL", "SN")
SUBJECT_PARTICLES = {"JKS"}
OBJECT_PARTICLES = {"JKO"}
TOPIC_FORMS = {"은", "는"}
LOCATION_FORMS = {"에", "에서"}
STOP_NOUNS = {"것", "수", "등", "중", "때", "정도", "경우", "하나"}


class KiwiRepresentations:
    def __init__(self):
        try:
            from kiwipiepy import Kiwi
        except ImportError as exc:
            raise RuntimeError("python -m pip install kiwipiepy") from exc
        self.kiwi = Kiwi(num_workers=-1)

    @staticmethod
    def _term(form: str) -> str:
        return f"m:{form.lower()}"

    @staticmethod
    def _is_nominal(token: Any) -> bool:
        return token.tag.startswith(NOMINAL) or token.tag in {"XSN", "MM"}

    def _nominal_chunk_before(self, tokens: Sequence[Any], particle_index: int) -> list[str]:
        selected: list[str] = []
        index = particle_index - 1
        while index >= 0 and self._is_nominal(tokens[index]):
            token = tokens[index]
            if token.tag.startswith(NOMINAL) and clean(token.form):
                selected.append(self._term(token.form))
            index -= 1
        selected.reverse()
        return selected

    def all_morphemes_from_tokens(self, tokens: Sequence[Any]) -> list[str]:
        return [
            self._term(token.form)
            for token in tokens
            if token.tag.startswith(KEEP_ALL) and clean(token.form)
        ]

    def all_morphemes(self, text: str) -> list[str]:
        return self.all_morphemes_from_tokens(self.kiwi.tokenize(clean(text)))

    def tokenize_many(self, texts: Sequence[str]) -> list[list[str]]:
        normalized = [clean(text) for text in texts]
        return [self.all_morphemes_from_tokens(tokens) for tokens in self.kiwi.tokenize(normalized)]

    def sov(self, text: str) -> list[str]:
        tokens = self.kiwi.tokenize(clean(text))
        selected: list[str] = []
        for index, token in enumerate(tokens):
            if token.tag in SUBJECT_PARTICLES | OBJECT_PARTICLES:
                selected.extend(self._nominal_chunk_before(tokens, index))
            if token.tag.startswith(("VV", "VX")):
                selected.append(self._term(token.form))
        return list(dict.fromkeys(selected))

    def expanded_core(self, text: str) -> list[str]:
        tokens = self.kiwi.tokenize(clean(text))
        selected = self.sov(text)
        for index, token in enumerate(tokens):
            if token.tag == "JX" and token.form in TOPIC_FORMS:
                selected.extend(self._nominal_chunk_before(tokens, index))
            if token.tag == "JKB" and token.form in LOCATION_FORMS:
                selected.extend(self._nominal_chunk_before(tokens, index))
            is_content_noun = token.tag.startswith("NN") or token.tag == "SL"
            if is_content_noun and len(clean(token.form)) >= 2 and token.form not in STOP_NOUNS:
                selected.append(self._term(token.form))
            if token.tag.startswith(("VV", "VA", "VX")):
                selected.append(self._term(token.form))
        return list(dict.fromkeys(selected))


def query_text(claim: Mapping[str, Any]) -> str:
    indicator = clean(claim.get("indicator_norm") or claim.get("indicator_raw"))
    population = clean(claim.get("population_norm") or claim.get("population_raw"))
    preferred = " ".join(value for value in (indicator, population) if value)
    return preferred or clean(claim.get("claim_text"))


def describe(values: Sequence[float]) -> dict[str, float]:
    return {
        "mean": round(statistics.mean(values), 4),
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def search_mode(
    mode: str,
    index: BM25InvertedIndex,
    tokenizer: Callable[[str], list[str]],
    claims: Sequence[Mapping[str, Any]],
    top_n: int,
    output_dir: Path,
    gold: Mapping[str, set[str]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    started = time.perf_counter()
    rows = []
    rankings: dict[str, list[dict[str, Any]]] = {}
    token_counts = []
    for claim in claims:
        claim_id = clean(claim.get("claim_id"))
        text = query_text(claim)
        hits, tokens = index.search(text, tokenizer, top_n)
        rankings[claim_id] = hits
        token_counts.append(len(tokens))
        rows.append({
            "claim_id": claim_id,
            "claim_text": clean(claim.get("claim_text")),
            "query_text": text,
            "query_tokens": tokens,
            "experiment": mode,
            "candidates": hits,
        })
    elapsed = time.perf_counter() - started
    write_jsonl(output_dir / f"rankings_{mode.lower()}.jsonl", rows)
    summary = {
        "mode": mode,
        "query_token_count": describe(token_counts),
        "empty_query_claims": sum(count == 0 for count in token_counts),
        "empty_result_claims": sum(not rankings[row["claim_id"]] for row in rows),
        "full_top_n_claims": sum(len(rankings[row["claim_id"]]) == top_n for row in rows),
        "search_seconds": round(elapsed, 3),
        "mean_search_ms": round(elapsed * 1000 / len(claims), 3),
        "recall": evaluate_recall(rankings, gold, (5, 10, 20)),
    }
    return summary, rankings


def pair_comparison(
    left: Mapping[str, Sequence[Mapping[str, Any]]],
    right: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    ids = sorted(set(left) & set(right))
    result: dict[str, Any] = {}
    for k in (1, 5, 10, 20):
        values = []
        any_overlap = 0
        exact_nonempty = 0
        both_empty = 0
        for claim_id in ids:
            a = {row["table_key"] for row in left[claim_id][:k]}
            b = {row["table_key"] for row in right[claim_id][:k]}
            union = a | b
            if union:
                values.append(len(a & b) / len(union))
            else:
                both_empty += 1
            any_overlap += bool(a & b)
            exact_nonempty += bool(union) and a == b
        result[f"top_{k}"] = {
            "mean_jaccard_nonempty_union": round(statistics.mean(values), 6) if values else None,
            "any_overlap_claims": any_overlap,
            "exact_nonempty_set_claims": exact_nonempty,
            "both_empty_claims": both_empty,
        }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--sample-size", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--catalog-limit", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    claims = reservoir_sample_claims(args.claims, args.sample_size, args.seed)
    write_jsonl(args.output_dir / f"sample_claims_{len(claims)}.jsonl", claims)
    gold = load_gold(args.gold)
    kiwi = KiwiRepresentations()

    index_started = time.perf_counter()
    regex_index = BM25InvertedIndex.build(
        args.catalog, regex_tokens, catalog_limit=args.catalog_limit
    )
    regex_index_seconds = time.perf_counter() - index_started
    b1, rank_b1 = search_mode(
        "B1", regex_index, regex_tokens, claims, args.top_n, args.output_dir, gold
    )
    del regex_index
    gc.collect()

    index_started = time.perf_counter()
    morph_index = BM25InvertedIndex.build(
        args.catalog, kiwi, catalog_limit=args.catalog_limit
    )
    morph_index_seconds = time.perf_counter() - index_started
    b2, rank_b2 = search_mode(
        "B2", morph_index, kiwi.all_morphemes, claims, args.top_n, args.output_dir, gold
    )
    b3, rank_b3 = search_mode(
        "B3", morph_index, kiwi.sov, claims, args.top_n, args.output_dir, gold
    )
    b4, rank_b4 = search_mode(
        "B4", morph_index, kiwi.expanded_core, claims, args.top_n, args.output_dir, gold
    )

    rankings = {"B1": rank_b1, "B2": rank_b2, "B3": rank_b3, "B4": rank_b4}
    pairs = {}
    names = list(rankings)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            pairs[f"{left}_vs_{right}"] = pair_comparison(rankings[left], rankings[right])

    summary = {
        "experiment": "bm25-b1-b4",
        "sample_size": len(claims),
        "seed": args.seed,
        "catalog_limit": args.catalog_limit,
        "catalog_documents": len(morph_index.documents),
        "top_n": args.top_n,
        "gold_mappings": sum(len(values) for values in gold.values()),
        "index_build_seconds": {
            "B1_regex": round(regex_index_seconds, 3),
            "B2_B4_shared_morph": round(morph_index_seconds, 3),
        },
        "results": [b1, b2, b3, b4],
        "pair_comparisons": pairs,
    }
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
