"""Release-pinned KOSIS table search and metadata hydration service."""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

from backend.errors import BackendError
from backend.metadata_repository import MetadataRepository, repository_from_env
from backend.search_adapter import OpenSearchBM25Adapter, normalize_query


SEARCH_WINDOW_MAX = 1000
_metadata_repository: MetadataRepository | Any | None = None
_bm25_adapter: OpenSearchBM25Adapter | Any | None = None


def configure_adapters(*, metadata: Any | None = None, bm25: Any | None = None) -> None:
    """Inject adapters for tests or process-level wiring; no fallback is installed."""

    global _metadata_repository, _bm25_adapter
    _metadata_repository = metadata
    _bm25_adapter = bm25


def _release_id() -> str:
    release = os.getenv("KOSIS_RELEASE_ID", "").strip()
    if not release:
        raise BackendError("KOSIS_RELEASE_CONFIGURATION_PENDING", "KOSIS release 설정이 없습니다.", status_code=503)
    return release


def _metadata() -> Any:
    global _metadata_repository
    if _metadata_repository is None:
        _metadata_repository = repository_from_env()
    return _metadata_repository


def _bm25() -> Any:
    global _bm25_adapter
    if _bm25_adapter is None:
        _bm25_adapter = OpenSearchBM25Adapter()
    return _bm25_adapter


def kosis_table_url(org_id: str, tbl_id: str) -> str:
    return f"https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"


def _window(page_limit: int, page_offset: int, *, bounded: bool) -> tuple[int, int]:
    try:
        page_limit = int(page_limit)
        page_offset = int(page_offset)
    except (TypeError, ValueError) as exc:
        raise BackendError("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", status_code=422) from exc
    if not 1 <= page_limit <= 100 or page_offset < 0:
        raise BackendError("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", status_code=422)
    if bounded and page_offset + page_limit > SEARCH_WINDOW_MAX:
        raise BackendError("SEARCH_WINDOW_EXCEEDED", "검색 결과 범위는 1,000개 이내여야 합니다.", status_code=422)
    return page_limit, page_offset


def _metadata_item(row: dict[str, Any], *, source: str, score: float | None, evidence: dict[str, Any]) -> dict[str, Any]:
    org_id = str(row.get("org_id") or "")
    tbl_id = str(row.get("tbl_id") or "")
    org_name = row.get("org_name_raw")
    tbl_name = row.get("title_raw")
    url = kosis_table_url(org_id, tbl_id) if org_id and tbl_id else ""
    metadata = dict(row)
    return {
        **metadata,
        "table_key": str(row["table_key"]),
        "release_id": str(row["snapshot_id"]),
        "org_name": org_name,
        "tbl_name": tbl_name,
        "source": source,
        "score": score,
        "metadata": metadata,
        "kosis_url": url,
        "evidence": evidence,
    }


def _organizations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    for item in items:
        metadata = item.get("metadata") or item
        org_id = str(metadata.get("org_id") or "")
        org_name = str(item.get("org_name") or metadata.get("org_name_raw") or org_id)
        if org_id or org_name:
            counts[(org_id, org_name)] += 1
    return [
        {"id": org_id, "name": org_name, "count": count}
        for (org_id, org_name), count in sorted(counts.items(), key=lambda pair: (pair[0][1], pair[0][0]))
    ]


def search_tables(query: str, *, limit: int = 20, offset: int = 0, organization: str = "") -> dict[str, Any]:
    """Browse metadata or hydrate release-pinned BM25 candidates in stable order."""

    normalized = normalize_query(query)
    page_limit, page_offset = _window(limit, offset, bounded=bool(normalized))
    release = _release_id()
    org = str(organization or "").strip()
    if not normalized:
        result = _metadata().browse_tables(release, limit=page_limit, offset=page_offset, organization=org)
        items = [
            _metadata_item(
                row,
                source="postgresql_metadata",
                score=0.0,
                evidence={"channel": "metadata_browse"},
            )
            for row in result["items"]
        ]
        return {
            "release_id": release,
            "items": items,
            "total": int(result["total"]),
            "total_relation": "eq",
            "limit": page_limit,
            "offset": page_offset,
            "organizations": result.get("organizations", []),
            "organizations_relation": "eq",
        }

    search_result = _bm25().search(normalized, limit=page_limit, offset=page_offset)
    window_candidates = list(search_result.get("window", search_result.get("candidates", [])))
    keys = [str(candidate.get("table_key") or "") for candidate in window_candidates]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 candidate table_key가 올바르지 않습니다.", status_code=503)
    rows = _metadata().hydrate_tables(release, keys)
    by_key = {str(row["table_key"]): row for row in rows}
    if set(by_key) != set(keys):
        raise BackendError("CROSS_STORE_RELEASE_MISMATCH", "검색 결과와 metadata release가 일치하지 않습니다.", status_code=503)
    candidates_by_key = {str(candidate["table_key"]): candidate for candidate in window_candidates}
    hydrated = [
        _metadata_item(
            by_key[key],
            source=str(candidates_by_key[key]["source"]),
            score=float(candidates_by_key[key]["score"]),
            evidence=dict(candidates_by_key[key].get("evidence") or {}),
        )
        for key in keys
    ]
    filtered = [
        item for item in hydrated
        if not org or str(item.get("org_id") or "") == org or str(item.get("org_name") or "") == org
    ]
    page = filtered[page_offset : page_offset + page_limit]
    relation = "gte" if search_result.get("total_relation") == "gte" else "eq"
    return {
        "release_id": release,
        "items": page,
        "total": len(filtered),
        "total_relation": relation,
        "limit": page_limit,
        "offset": page_offset,
        "organizations": _organizations(hydrated),
        "organizations_relation": relation,
    }


def get_table(table_key: str) -> dict[str, Any] | None:
    """Read one metadata row; no SQLite or legacy catalog fallback exists."""

    key = str(table_key or "").strip()
    if not key:
        return None
    row = _metadata().get_table(_release_id(), key)
    if row is None:
        return None
    return _metadata_item(row, source="postgresql_metadata", score=0.0, evidence={"channel": "metadata_lookup"})
