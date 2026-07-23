"""경로별 중복 제거와 Reciprocal Rank Fusion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from retrieval_backend import PathHit


@dataclass
class FusedCandidate:
    table_key: str
    rank: int = 0
    fusion_score: float = 0.0
    tbl_name: str | None = None
    path_ranks: dict[str, int] = field(default_factory=dict)
    path_scores: dict[str, float] = field(default_factory=dict)


def _best_per_table(hits: Sequence[PathHit]) -> dict[str, PathHit]:
    best: dict[str, PathHit] = {}
    for hit in hits:
        current = best.get(hit.table_key)
        if current is None or hit.rank < current.rank:
            best[hit.table_key] = hit
    return best


def reciprocal_rank_fusion(
    path_results: Mapping[str, Sequence[PathHit]],
    *,
    top_n: int = 20,
    rrf_k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> list[FusedCandidate]:
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if rrf_k < 1:
        raise ValueError("rrf_k must be >= 1")
    weights = weights or {}
    candidates: dict[str, FusedCandidate] = {}
    for path, raw_hits in path_results.items():
        for hit in _best_per_table(raw_hits).values():
            candidate = candidates.setdefault(
                hit.table_key,
                FusedCandidate(table_key=hit.table_key, tbl_name=hit.tbl_name),
            )
            candidate.path_ranks[path] = hit.rank
            candidate.path_scores[path] = hit.raw_score
            candidate.fusion_score += weights.get(path, 1.0) / (rrf_k + hit.rank)
            if not candidate.tbl_name and hit.tbl_name:
                candidate.tbl_name = hit.tbl_name
    ordered = sorted(
        candidates.values(),
        key=lambda item: (-item.fusion_score, item.table_key),
    )[:top_n]
    for rank, candidate in enumerate(ordered, start=1):
        candidate.rank = rank
        candidate.fusion_score = round(candidate.fusion_score, 10)
    return ordered

