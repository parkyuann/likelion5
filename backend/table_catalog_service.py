"""Release-pinned KOSIS table search and metadata hydration service."""

from __future__ import annotations

import os
import time
import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from backend.errors import BackendError
from backend.metadata_repository import MetadataRepository, repository_from_env
from backend.query_encoder import BGEQueryEncoderClient
from backend.search_adapter import (
    ALLOWED_FIELDS,
    OpenSearchBM25Adapter,
    QdrantDenseAdapter,
    SEARCH_CHANNEL_TOP_K,
    normalize_query,
)


SEARCH_WINDOW_MAX = 1000
HYBRID_WINDOW_MAX = SEARCH_CHANNEL_TOP_K
HYBRID_DEADLINE_SECONDS = 8.0
_metadata_repository: MetadataRepository | Any | None = None
_bm25_adapter: OpenSearchBM25Adapter | Any | None = None
_dense_adapter: QdrantDenseAdapter | Any | None = None
_query_encoder: BGEQueryEncoderClient | Any | None = None


def configure_adapters(
    *,
    metadata: Any | None = None,
    bm25: Any | None = None,
    dense: Any | None = None,
    encoder: Any | None = None,
) -> None:
    """Inject adapters for tests or process-level wiring; no fallback is installed."""

    global _metadata_repository, _bm25_adapter, _dense_adapter, _query_encoder
    _metadata_repository = metadata
    _bm25_adapter = bm25
    _dense_adapter = dense
    _query_encoder = encoder


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


def _dense() -> Any:
    global _dense_adapter
    if _dense_adapter is None:
        _dense_adapter = QdrantDenseAdapter()
    return _dense_adapter


def _encoder() -> Any:
    global _query_encoder
    if _query_encoder is None:
        _query_encoder = BGEQueryEncoderClient()
    return _query_encoder


def _hybrid_rrf_k() -> int:
    """Fail closed when deployed fusion pins drift from the approved contract."""

    expected = {
        "KOSIS_HYBRID_PATH_TOP_K": str(SEARCH_CHANNEL_TOP_K),
        "KOSIS_HYBRID_FUSION_TOP_K": str(HYBRID_WINDOW_MAX),
        "KOSIS_HYBRID_RRF_K": "60",
    }
    if any(os.getenv(name, "").strip() != value for name, value in expected.items()):
        raise BackendError(
            "HYBRID_SEARCH_CONFIGURATION_PENDING",
            "hybrid retrieval pin 설정이 승인 계약과 일치하지 않습니다.",
            status_code=503,
        )
    return 60


def kosis_table_url(org_id: str, tbl_id: str) -> str:
    return f"https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"


def _window(page_limit: int, page_offset: int, *, maximum: int | None = None) -> tuple[int, int]:
    try:
        page_limit = int(page_limit)
        page_offset = int(page_offset)
    except (TypeError, ValueError) as exc:
        raise BackendError("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", status_code=422) from exc
    if not 1 <= page_limit <= 100 or page_offset < 0:
        raise BackendError("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", status_code=422)
    if maximum is not None and page_offset + page_limit > maximum:
        raise BackendError("SEARCH_WINDOW_EXCEEDED", f"검색 결과 범위는 {maximum}개 이내여야 합니다.", status_code=422)
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


def _future_result(future: Any, deadline: float) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BackendError("HYBRID_SEARCH_TIMEOUT", "hybrid 검색 시간이 초과되었습니다.", status_code=503)
    try:
        return future.result(timeout=remaining)
    except FutureTimeoutError as exc:
        raise BackendError("HYBRID_SEARCH_TIMEOUT", "hybrid 검색 시간이 초과되었습니다.", status_code=503) from exc
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError("HYBRID_SEARCH_UNAVAILABLE", "hybrid 검색 채널을 사용할 수 없습니다.", status_code=503) from exc


def _channel_record(candidate: dict[str, Any], *, source: str, rank: int) -> dict[str, Any]:
    evidence = candidate.get("evidence")
    if not isinstance(evidence, dict):
        raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 candidate evidence가 올바르지 않습니다.", status_code=503)
    table_key = str(candidate.get("table_key") or "").strip()
    release_id = str(candidate.get("release_id") or "").strip()
    if not table_key or not release_id:
        raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 candidate 식별자가 올바르지 않습니다.", status_code=503)
    score = candidate.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
        raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 candidate score가 올바르지 않습니다.", status_code=503)
    record_id = str(evidence.get("record_id") or "").strip()
    source_id = str(evidence.get("source_id") or "").strip()
    field = str(evidence.get("field") or "").strip()
    text_sha256 = str(evidence.get("text_sha256") or "").strip()
    index_or_collection = str(evidence.get("index") or evidence.get("collection") or "").strip()
    if not record_id or not source_id or not field or not text_sha256 or not index_or_collection:
        raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 candidate provenance가 부족합니다.", status_code=503)
    return {
        "source": source,
        "rank": rank,
        "raw_score": float(score),
        "record_id": record_id,
        "field": field,
        "source_id": source_id,
        "text_sha256": text_sha256,
        "index_or_collection": index_or_collection,
        "release_id": release_id,
    }


def _fuse_channels(
    bm25_result: dict[str, Any],
    dense_result: dict[str, Any],
    *,
    release: str,
    encoder_evidence: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    rrf_k = _hybrid_rrf_k()
    by_key: dict[str, dict[str, Any]] = {}
    channel_results = (("bm25", bm25_result), ("dense", dense_result))
    for channel, result in channel_results:
        candidates = result.get("window", result.get("candidates", []))
        if not isinstance(candidates, list) or len(candidates) > SEARCH_CHANNEL_TOP_K:
            raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 channel window가 올바르지 않습니다.", status_code=503)
        seen: set[str] = set()
        for rank, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 candidate 형식이 올바르지 않습니다.", status_code=503)
            key = str(candidate.get("table_key") or "").strip()
            if key in seen:
                raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 channel table_key가 중복됩니다.", status_code=503)
            seen.add(key)
            if str(candidate.get("release_id") or "") != release:
                raise BackendError("CROSS_STORE_RELEASE_MISMATCH", "검색 candidate release가 일치하지 않습니다.", status_code=503)
            expected_source = "opensearch_bm25" if channel == "bm25" else "qdrant_dense"
            if str(candidate.get("source") or "") != expected_source:
                raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 channel source가 일치하지 않습니다.", status_code=503)
            record = _channel_record(candidate, source=channel, rank=rank)
            item = by_key.setdefault(
                key,
                {"table_key": key, "rrf_score": 0.0, "best_channel_rank": rank, "channels": []},
            )
            item["rrf_score"] += 1.0 / (rrf_k + rank)
            item["best_channel_rank"] = min(item["best_channel_rank"], rank)
            item["channels"].append(record)
    fused_items = sorted(
        by_key.values(),
        key=lambda item: (-float(item["rrf_score"]), int(item["best_channel_rank"]), item["table_key"]),
    )[:SEARCH_CHANNEL_TOP_K]
    fused: list[dict[str, Any]] = []
    for rank, item in enumerate(fused_items, start=1):
        channels = sorted(item["channels"], key=lambda channel: (channel["source"], channel["rank"]))
        fused.append(
            {
                "table_key": item["table_key"],
                "release_id": release,
                "source": "hybrid_rrf",
                "score": float(item["rrf_score"]),
                "evidence": {
                    "fusion": {
                        "contract": "hybrid-bm25-dense-rrf-v1",
                        "rank": rank,
                        "rrf_k": rrf_k,
                        "best_channel_rank": int(item["best_channel_rank"]),
                    },
                    "channels": channels,
                    "encoder": dict(encoder_evidence),
                },
            }
        )
    relations = [str(result.get("total_relation") or "eq") for _, result in channel_results]
    return fused, ("gte" if "gte" in relations else "eq")


def search_tables(query: str, *, limit: int = 20, offset: int = 0, organization: str = "") -> dict[str, Any]:
    """Browse metadata or hydrate release-pinned BM25+dense candidate results."""

    normalized = normalize_query(query)
    page_limit, page_offset = _window(limit, offset, maximum=HYBRID_WINDOW_MAX if normalized else None)
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

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kosis-hybrid")
    deadline = time.monotonic() + HYBRID_DEADLINE_SECONDS
    bm25_future = executor.submit(_bm25().search, normalized, limit=SEARCH_CHANNEL_TOP_K, offset=0)
    encoder_future = executor.submit(_encoder().encode, normalized)
    try:
        vector, encoder_evidence = _future_result(encoder_future, deadline)
        dense_future = executor.submit(
            _dense().search_grouped_by_table,
            vector,
            fields=sorted(ALLOWED_FIELDS),
            limit=SEARCH_CHANNEL_TOP_K,
        )
        bm25_result = _future_result(bm25_future, deadline)
        dense_result = _future_result(dense_future, deadline)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if not isinstance(encoder_evidence, dict):
        raise BackendError("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder evidence가 올바르지 않습니다.", status_code=503)
    fused_candidates, relation = _fuse_channels(
        bm25_result,
        dense_result,
        release=release,
        encoder_evidence=encoder_evidence,
    )
    window_candidates = fused_candidates
    keys = [str(candidate.get("table_key") or "") for candidate in window_candidates]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise BackendError("SEARCH_SOURCE_CONTRACT_MISMATCH", "검색 candidate table_key가 올바르지 않습니다.", status_code=503)
    rows = _metadata().hydrate_tables(release, keys) if keys else []
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
