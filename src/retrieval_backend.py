"""Dense 검색 구현과 D 파트 사이의 입력·출력 계약."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class PathHit:
    table_key: str
    path: str
    rank: int
    raw_score: float
    tbl_name: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def with_path(self, path: str) -> "PathHit":
        return replace(self, path=path)


@runtime_checkable
class DenseSearchBackend(Protocol):
    """C 담당자가 Qdrant 구현으로 충족해야 하는 검색 계약."""

    def search(
        self,
        query_text: str,
        top_k: int,
        filters: Mapping[str, Any] | None = None,
    ) -> list[PathHit]:
        """Catalog와 같은 모델로 query_text를 임베딩해 Top-K를 반환한다."""
        ...


@runtime_checkable
class SparseSearchBackend(Protocol):
    """B2/B4 BM25 구현이 충족해야 하는 검색 계약."""

    def search(
        self,
        query_text: str,
        mode: str,
        top_k: int,
        filters: Mapping[str, Any] | None = None,
    ) -> list[PathHit]:
        ...


def validate_hits(hits: list[PathHit], top_k: int) -> None:
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    if len(hits) > top_k:
        raise ValueError(f"backend returned {len(hits)} hits for top_k={top_k}")
    for expected_rank, hit in enumerate(hits, start=1):
        if not hit.table_key:
            raise ValueError("table_key is required")
        if hit.rank != expected_rank:
            raise ValueError("hits must be ranked consecutively from 1")

