"""Run v2 hybrid retrieval: doc_meta BM25 + Claim Dense + HyDE Dense.

Created: 2026-07-22
Catalog: data/kosis_catalog_v2.jsonl
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[4]
ARCHIVE_ROOT = PROJECT_ROOT / "archive" / "260722"
EXPERIMENT_CODE = ARCHIVE_ROOT / "code"
PROJECT_SRC = PROJECT_ROOT / "src"
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "kosis_catalog_v2.jsonl"
DEFAULT_DB = PROJECT_ROOT / "output" / "kosis_qdrant_v2"
DEFAULT_OUTPUT = ARCHIVE_ROOT / "outputs" / "hybrid_v2_20260722"
COLLECTION = "kosis_tables_v2"

sys.path.insert(0, str(PROJECT_SRC))
sys.path.insert(0, str(EXPERIMENT_CODE))

from bm25_b1_b4_experiment import KiwiRepresentations  # noqa: E402
from retrieval_backend import PathHit, validate_hits  # noqa: E402
from retrieval_fusion import reciprocal_rank_fusion  # noqa: E402
from search_hybrid_one import _embed, _generate_hyde  # noqa: E402


@dataclass(frozen=True)
class Document:
    table_key: str
    org_id: str
    tbl_name: str


class DocMetaBM25:
    """Okapi BM25 whose document is exactly Catalog v2 doc_meta_text."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents: list[Document] = []
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.idf: dict[str, float] = {}
        self.avg_length = 0.0

    @classmethod
    def build(cls, catalog: Path, kiwi: KiwiRepresentations) -> "DocMetaBM25":
        index = cls()
        rows = [json.loads(line) for line in catalog.open(encoding="utf-8-sig") if line.strip()]
        texts = [str(row.get("doc_meta_text") or "").strip() for row in rows]
        if any(not text for text in texts):
            raise ValueError("v2 catalog contains empty doc_meta_text")
        token_rows = kiwi.tokenize_many(texts)
        for row, tokens in zip(rows, token_rows):
            doc_id = len(index.documents)
            index.documents.append(Document(
                table_key=str(row["table_key"]),
                org_id=str(row.get("org_id") or ""),
                tbl_name=str(row.get("tbl_name") or ""),
            ))
            counts = Counter(tokens)
            index.lengths.append(sum(counts.values()))
            for term, frequency in counts.items():
                index.postings[term].append((doc_id, frequency))
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
        org_ids: set[str] | None = None,
    ) -> tuple[list[PathHit], list[str]]:
        terms = list(dict.fromkeys(tokenizer(query)))
        scores: defaultdict[int, float] = defaultdict(float)
        for term in terms:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc_id, frequency in self.postings[term]:
                document = self.documents[doc_id]
                if org_ids and document.org_id not in org_ids:
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
        return [
            PathHit(
                table_key=self.documents[doc_id].table_key,
                path="",
                rank=rank,
                raw_score=round(float(score), 8),
                tbl_name=self.documents[doc_id].tbl_name,
                payload={"org_id": self.documents[doc_id].org_id},
            )
            for rank, (doc_id, score) in enumerate(best, start=1)
            if score > 0
        ], terms


def dense_search(client, query: str, vector_name: str, path: str, top_k: int, org_ids) -> list[PathHit]:
    query_filter = None
    if org_ids:
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        query_filter = Filter(must=[FieldCondition(key="org_id", match=MatchAny(any=org_ids))])
    response = client.query_points(
        collection_name=COLLECTION,
        query=_embed(query),
        using=vector_name,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    hits = []
    for point in response.points:
        payload = point.payload or {}
        table_key = str(payload.get("table_key") or "").strip()
        if table_key:
            hits.append(PathHit(
                table_key=table_key,
                path=path,
                rank=len(hits) + 1,
                raw_score=float(point.score),
                tbl_name=str(payload.get("tbl_name") or "").strip() or None,
                payload=payload,
            ))
    validate_hits(hits, top_k)
    return hits


def set_path(hits: list[PathHit], path: str) -> list[PathHit]:
    return [hit.with_path(path) for hit in hits]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim-text", required=True)
    parser.add_argument("--claim-id", default="v2-cli-single-claim")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--per-path-n", type=int, default=20)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--hcx-model", default="HCX-007")
    parser.add_argument("--org-id", action="append")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    claim_text = str(args.claim_text or "").strip()
    if not claim_text:
        raise ValueError("claim-text must not be empty")
    if min(args.per_path_n, args.top_n, args.rrf_k) < 1:
        raise ValueError("per-path-n, top-n and rrf-k must be >= 1")

    from qdrant_client import QdrantClient

    started = time.perf_counter()
    print("[1/5] Catalog v2 doc_meta_text BM25 index", file=sys.stderr, flush=True)
    kiwi = KiwiRepresentations()
    bm25 = DocMetaBM25.build(args.catalog, kiwi)
    org_ids = set(args.org_id) if args.org_id else None

    print("[2/5] B2/B4 BM25 search", file=sys.stderr, flush=True)
    b2_hits, b2_tokens = bm25.search(claim_text, kiwi.all_morphemes, args.per_path_n, org_ids)
    b4_hits, b4_tokens = bm25.search(claim_text, kiwi.expanded_core, args.per_path_n, org_ids)
    paths: dict[str, list[PathHit]] = {
        "b2_doc_meta_bm25": set_path(b2_hits, "b2_doc_meta_bm25"),
        "b4_doc_meta_bm25": set_path(b4_hits, "b4_doc_meta_bm25"),
    }

    client = QdrantClient(path=str(args.db_path))
    errors: dict[str, str] = {}
    predicted_tbl_nm: str | None = None
    try:
        qdrant_points = int(client.count(COLLECTION, exact=True).count)
        print("[3/5] Claim Dense on doc_meta_vector", file=sys.stderr, flush=True)
        try:
            paths["claim_dense"] = dense_search(
                client, claim_text, "doc_meta_vector", "claim_dense", args.per_path_n, args.org_id
            )
        except Exception as exc:
            errors["claim_dense"] = f"{type(exc).__name__}: {exc}"
            paths["claim_dense"] = []

        print("[4/5] HCX HyDE + tbl_name_vector", file=sys.stderr, flush=True)
        try:
            predicted_tbl_nm = _generate_hyde(claim_text, args.hcx_model)
            paths["hyde_dense"] = dense_search(
                client, predicted_tbl_nm, "tbl_name_vector", "hyde_dense", args.per_path_n, args.org_id
            )
        except Exception as exc:
            errors["hyde_dense"] = f"{type(exc).__name__}: {exc}"
            paths["hyde_dense"] = []
    finally:
        client.close()

    print("[5/5] RRF", file=sys.stderr, flush=True)
    fused = reciprocal_rank_fusion(paths, top_n=args.top_n, rrf_k=args.rrf_k)
    selected = [
        {
            "final_rank": row.rank,
            "table_key": row.table_key,
            "tbl_name": row.tbl_name,
            "fusion_score": row.fusion_score,
        }
        for row in fused
    ]
    elapsed = round(time.perf_counter() - started, 3)
    result = {
        "run_date": "2026-07-22",
        "catalog_version": "kosis-catalog-v2",
        "claim_id": args.claim_id,
        "claim_text": claim_text,
        "predicted_tbl_nm": predicted_tbl_nm,
        "selection_method": "RRF(b2_doc_meta_bm25,b4_doc_meta_bm25,claim_dense,hyde_dense)",
        "selected_tables": selected,
        "search_scope": {"bm25_catalog_documents": len(bm25.documents), "dense_qdrant_points": qdrant_points},
        "errors": errors,
        "elapsed_sec": elapsed,
    }
    debug = {
        **result,
        "query_tokens": {"b2": b2_tokens, "b4": b4_tokens},
        "path_results": {path: [asdict(hit) for hit in hits] for path, hits in paths.items()},
        "fused_candidates_with_path_ranks": [asdict(row) for row in fused],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "result_debug.json").write_text(
        json.dumps(debug, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
