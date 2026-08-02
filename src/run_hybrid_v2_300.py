"""Sample 300 claims and run Catalog v2 four-path hybrid retrieval.

Created: 2026-07-22
Batch execution date: 2026-07-23
Outputs one JSON object per claim in JSONL format and supports cache-based resume.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[4]
PROJECT_SRC = PROJECT_ROOT / "src"
ARCHIVE_ROOT = PROJECT_ROOT / "archive" / "260722"
DEFAULT_CLAIMS = PROJECT_ROOT / "data" / "claims_v1.jsonl"
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "kosis_catalog_v2.jsonl"
DEFAULT_DB = PROJECT_ROOT / "output" / "kosis_qdrant_v2"
DEFAULT_OUTPUT = ARCHIVE_ROOT / "outputs" / "hybrid_v2_300_20260722"
HYDE_SEED = ARCHIVE_ROOT / "outputs" / "hcx_hyde_100" / "hyde_predictions.jsonl"
EMBED_SEED = ARCHIVE_ROOT / "outputs" / "hybrid_100" / "query_embedding_cache.jsonl"
COLLECTION = "kosis_tables_v2"
PATHS = ("b2_doc_meta_bm25", "b4_doc_meta_bm25", "claim_dense", "hyde_dense")

sys.path.insert(0, str(PROJECT_SRC))

from kosis_v2_indexer import EmbedCache  # noqa: E402
from retrieval_backend import PathHit, validate_hits  # noqa: E402
from retrieval_fusion import reciprocal_rank_fusion  # noqa: E402
from retrieval_query_builder import build_claim_dense_query  # noqa: E402
from search_hybrid_one import _generate_hyde  # noqa: E402
from search_hybrid_v2 import DocMetaBM25, KiwiRepresentations, set_path  # noqa: E402


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def unwrap_claim(payload: dict[str, Any]) -> dict[str, Any]:
    return dict(payload.get("claim", payload))


def reservoir_sample(path: Path, size: int, seed: int) -> list[dict[str, Any]]:
    if size < 1:
        raise ValueError("sample-size must be >= 1")
    rng = random.Random(seed)
    sample: list[dict[str, Any]] = []
    seen = 0
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            claim = unwrap_claim(json.loads(line))
            claim_id = clean(claim.get("claim_id"))
            claim_text = clean(claim.get("claim_text"))
            if not claim_id or not claim_text:
                continue
            seen += 1
            if len(sample) < size:
                sample.append(claim)
            else:
                replacement = rng.randrange(seen)
                if replacement < size:
                    sample[replacement] = claim
    if len(sample) < size:
        raise ValueError(f"requested {size}, but only {seen} valid claims")
    return sorted(sample, key=lambda row: clean(row.get("claim_id")))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class HydePredictionCache:
    def __init__(self, output: Path, seed: Path | None = None):
        self.output = output
        self.rows = {str(row["claim_id"]): row for row in read_jsonl(output)}
        self.seed = {
            str(row["claim_id"]): row
            for row in read_jsonl(seed) if row.get("status") == "success"
        } if seed else {}
        self.hits = 0
        self.calls = 0

    def get_or_generate(self, claim_id: str, claim_text: str, model: str) -> str:
        current = self.rows.get(claim_id)
        if current and current.get("status") == "success":
            self.hits += 1
            return str(current["predicted_tbl_nm"])
        seeded = self.seed.get(claim_id)
        if seeded:
            self.hits += 1
            row = {
                "claim_id": claim_id,
                "claim_text": claim_text,
                "predicted_tbl_nm": seeded["predicted_tbl_nm"],
                "status": "success",
                "model": seeded.get("model", model),
                "cache_source": "hcx_hyde_100",
            }
            append_jsonl(self.output, row)
            self.rows[claim_id] = row
            return str(row["predicted_tbl_nm"])
        started = time.perf_counter()
        try:
            predicted = _generate_hyde(claim_text, model)
            self.calls += 1
            row = {
                "claim_id": claim_id,
                "claim_text": claim_text,
                "predicted_tbl_nm": predicted,
                "status": "success",
                "model": model,
                "cache_source": "api",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        except Exception as exc:
            self.calls += 1
            row = {
                "claim_id": claim_id,
                "claim_text": claim_text,
                "predicted_tbl_nm": None,
                "status": "error",
                "model": model,
                "cache_source": "api",
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        append_jsonl(self.output, row)
        self.rows[claim_id] = row
        if row["status"] != "success":
            raise RuntimeError(str(row["error"]))
        return str(row["predicted_tbl_nm"])


def dense_search(client, embed_cache: EmbedCache, query: str, vector_name: str, path: str, top_k: int) -> list[PathHit]:
    response = client.query_points(
        collection_name=COLLECTION,
        query=embed_cache.embed(query),
        using=vector_name,
        limit=top_k,
        with_payload=True,
    )
    hits = []
    for point in response.points:
        payload = point.payload or {}
        table_key = clean(payload.get("table_key"))
        if table_key:
            hits.append(PathHit(
                table_key=table_key,
                path=path,
                rank=len(hits) + 1,
                raw_score=float(point.score),
                tbl_name=clean(payload.get("tbl_name")) or None,
                payload=payload,
            ))
    validate_hits(hits, top_k)
    return hits


def describe(values: list[float]) -> dict[str, float]:
    return {
        "mean": round(statistics.mean(values), 6),
        "median": round(statistics.median(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-size", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--per-path-n", type=int, default=20)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--hcx-model", default="HCX-007")
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if min(args.sample_size, args.per_path_n, args.top_n, args.rrf_k) < 1:
        raise ValueError("numeric arguments must be >= 1")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    claims = reservoir_sample(args.claims, args.sample_size, args.seed)
    claim_ids = [clean(row.get("claim_id")) for row in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("sample contains duplicate claim_id")
    sample_path = args.output_dir / f"sample_claims_{len(claims)}.jsonl"
    write_jsonl(sample_path, claims)

    print("[prepare] build v2 doc_meta_text BM25", flush=True)
    kiwi = KiwiRepresentations()
    bm25 = DocMetaBM25.build(args.catalog, kiwi)
    if args.prepare_only:
        print(json.dumps({
            "run_date": "2026-07-23",
            "catalog_version": "kosis-catalog-v2",
            "sample_size": len(claims),
            "unique_claim_ids": len(set(claim_ids)),
            "bm25_documents": len(bm25.documents),
            "sample_path": str(sample_path.resolve()),
        }, ensure_ascii=False, indent=2))
        return

    from qdrant_client import QdrantClient

    final_path = args.output_dir / "hybrid_top20_300.jsonl"
    debug_path = args.output_dir / "path_debug_300.jsonl"
    error_path = args.output_dir / "errors.jsonl"
    error_path.touch(exist_ok=True)
    final_by_id = {str(row["claim_id"]): row for row in read_jsonl(final_path)}
    debug_by_id = {str(row["claim_id"]): row for row in read_jsonl(debug_path)}
    hyde_cache = HydePredictionCache(args.output_dir / "hyde_predictions_300.jsonl", HYDE_SEED)
    embed_cache = EmbedCache(args.output_dir / "query_embedding_cache.jsonl", EMBED_SEED)
    client = QdrantClient(path=str(args.db_path))
    started = time.perf_counter()
    resumed = 0
    try:
        qdrant_points = int(client.count(COLLECTION, exact=True).count)
        if qdrant_points != len(bm25.documents):
            raise ValueError(f"scope mismatch: BM25={len(bm25.documents)} Qdrant={qdrant_points}")
        for index, claim in enumerate(claims, start=1):
            claim_id = clean(claim.get("claim_id"))
            if claim_id in final_by_id and claim_id in debug_by_id:
                resumed += 1
                continue
            claim_text = clean(claim.get("claim_text"))
            dense_query = build_claim_dense_query(claim) or claim_text
            errors: dict[str, str] = {}
            b2_hits, b2_tokens = bm25.search(claim_text, kiwi.all_morphemes, args.per_path_n)
            b4_hits, b4_tokens = bm25.search(claim_text, kiwi.expanded_core, args.per_path_n)
            paths: dict[str, list[PathHit]] = {
                "b2_doc_meta_bm25": set_path(b2_hits, "b2_doc_meta_bm25"),
                "b4_doc_meta_bm25": set_path(b4_hits, "b4_doc_meta_bm25"),
            }
            try:
                paths["claim_dense"] = dense_search(
                    client, embed_cache, dense_query, "doc_meta_vector", "claim_dense", args.per_path_n
                )
            except Exception as exc:
                errors["claim_dense"] = f"{type(exc).__name__}: {exc}"
                paths["claim_dense"] = []
            predicted: str | None = None
            try:
                predicted = hyde_cache.get_or_generate(claim_id, claim_text, args.hcx_model)
                paths["hyde_dense"] = dense_search(
                    client, embed_cache, predicted, "tbl_name_vector", "hyde_dense", args.per_path_n
                )
            except Exception as exc:
                errors["hyde_dense"] = f"{type(exc).__name__}: {exc}"
                paths["hyde_dense"] = []

            fused = reciprocal_rank_fusion(paths, top_n=args.top_n, rrf_k=args.rrf_k)
            final_row = {
                "run_date": "2026-07-23",
                "catalog_version": "kosis-catalog-v2",
                "claim_id": claim_id,
                "claim_text": claim_text,
                "predicted_tbl_nm": predicted,
                "selection_method": "RRF(b2_doc_meta_bm25,b4_doc_meta_bm25,claim_dense,hyde_dense)",
                "selected_tables": [
                    {
                        "final_rank": row.rank,
                        "table_key": row.table_key,
                        "tbl_name": row.tbl_name,
                        "fusion_score": row.fusion_score,
                    }
                    for row in fused
                ],
                "errors": errors,
            }
            debug_row = {
                **final_row,
                "claim_dense_query": dense_query,
                "query_tokens": {"b2": b2_tokens, "b4": b4_tokens},
                "path_results": {path: [asdict(hit) for hit in hits] for path, hits in paths.items()},
                "fused_candidates_with_path_ranks": [asdict(row) for row in fused],
            }
            append_jsonl(final_path, final_row)
            append_jsonl(debug_path, debug_row)
            final_by_id[claim_id] = final_row
            debug_by_id[claim_id] = debug_row
            for path, message in errors.items():
                append_jsonl(error_path, {"claim_id": claim_id, "path": path, "error": message})
            if index % 10 == 0 or index == len(claims):
                print(
                    f"hybrid={index}/{len(claims)} hcx_calls={hyde_cache.calls} "
                    f"embed_calls={embed_cache.api_calls} errors={sum(bool(row.get('errors')) for row in final_by_id.values())}",
                    flush=True,
                )
    finally:
        client.close()

    final_rows = [final_by_id[claim_id] for claim_id in claim_ids]
    debug_rows = [debug_by_id[claim_id] for claim_id in claim_ids]
    # Rewrite in deterministic sample order and remove any duplicate resume rows.
    write_jsonl(final_path, final_rows)
    write_jsonl(debug_path, debug_rows)

    path_counts: dict[str, list[float]] = {path: [] for path in PATHS}
    pair_values: dict[str, list[float]] = {
        f"{left}__{right}": [] for left, right in itertools.combinations(PATHS, 2)
    }
    support_counts: Counter[int] = Counter()
    for row in debug_rows:
        path_results = row["path_results"]
        sets = {path: {hit["table_key"] for hit in path_results.get(path, [])} for path in PATHS}
        for path in PATHS:
            path_counts[path].append(float(len(sets[path])))
        for left, right in itertools.combinations(PATHS, 2):
            union = sets[left] | sets[right]
            pair_values[f"{left}__{right}"].append(len(sets[left] & sets[right]) / len(union) if union else 1.0)
        for candidate in row["fused_candidates_with_path_ranks"]:
            support_counts[len(candidate["path_ranks"])] += 1

    elapsed = time.perf_counter() - started
    summary = {
        "run_date": "2026-07-23",
        "catalog_version": "kosis-catalog-v2",
        "sample_source": str(args.claims.resolve()),
        "sample_size": len(claims),
        "sample_seed": args.seed,
        "catalog_documents": len(bm25.documents),
        "qdrant_points": qdrant_points,
        "per_path_n": args.per_path_n,
        "top_n": args.top_n,
        "rrf_k": args.rrf_k,
        "resumed_claims": resumed,
        "hcx_cache_hits": hyde_cache.hits,
        "hcx_api_calls": hyde_cache.calls,
        "embedding_cache_hits": embed_cache.hits,
        "embedding_api_calls": embed_cache.api_calls,
        "claims_with_errors": sum(bool(row.get("errors")) for row in final_rows),
        "path_candidate_counts": {path: describe(values) for path, values in path_counts.items()},
        "pairwise_mean_jaccard": {name: round(statistics.mean(values), 6) for name, values in pair_values.items()},
        "final_support_path_count": {str(key): value for key, value in sorted(support_counts.items())},
        "elapsed_sec": round(elapsed, 3),
        "mean_ms_per_claim": round(elapsed * 1000 / len(claims), 3),
        "recall": None,
        "recall_reason": "gold table_key mappings were not provided",
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
