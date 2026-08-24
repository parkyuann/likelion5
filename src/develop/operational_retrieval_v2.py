"""Deterministic multi-query/multi-channel candidate generation for v2.

Retrieval outputs have candidate-membership authority only.  In particular,
ITEM and DIMENSION_AXIS hits are never emitted as DIMENSION value evidence or
as a completeness claim for the downstream late binder.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


CONTRACT_VERSION = "operational-retrieval-v2"
RRF_K = 60


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    role: str
    text: str
    channels: tuple[str, ...]
    fields_by_channel: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class RetrievalHit:
    query_id: str
    query_role: str
    channel: str
    table_key: str
    record_id: str
    field: str
    rank: int
    score: float | None
    release_sha256: str
    authority: str = "CANDIDATE_GENERATION_ONLY"


@dataclass(frozen=True)
class RrfCandidate:
    table_key: str
    rrf_score: float
    best_rank: int
    hits: tuple[RetrievalHit, ...]


@dataclass(frozen=True)
class RerankedCandidate:
    table_key: str
    rank: int
    raw_logit: float
    sigmoid_score: float
    rrf_score: float


class SearchChannel(Protocol):
    def __call__(self, query: QuerySpec, fields: Sequence[str], top_k: int) -> Iterable[Mapping[str, Any]]: ...


class Reranker(Protocol):
    def rerank(self, query: str, passages: Sequence[Mapping[str, str]]) -> Sequence[Mapping[str, Any]]: ...


def build_query_register(claim: Mapping[str, Any]) -> tuple[QuerySpec, ...]:
    """Build the approved three-role query register from explicit text only."""
    indicator = str(claim.get("indicator") or "").strip()
    item = str(claim.get("item") or indicator).strip()
    sentence = str(claim.get("sentence") or "").strip()
    # The sentence path contributes lexical context, never the asserted number.
    # Strip explicit numeric expressions before both retrieval and reranking.
    sentence = re.sub(r"[+-]?\d[\d,.]*(?:조|억|천만|백만|만|천)?(?:원|달러|명|가구|개|%p|%)?", " ", sentence)
    sentence = re.sub(r"\s+", " ", sentence).strip()
    queries: list[QuerySpec] = []
    corrective_only = claim.get("_corrective_only") is True
    if indicator and not corrective_only:
        queries.append(QuerySpec(
            "indicator", "indicator", indicator,
            ("official", "bm25", "dense"),
            {
                "official": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                "bm25": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                "dense": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
            },
        ))
    if item and not corrective_only:
        queries.append(QuerySpec(
            "item", "item", item,
            ("bm25", "dense"),
            {"bm25": ("ITEM",), "dense": ("ITEM",)},
        ))
    if sentence and not corrective_only:
        queries.append(QuerySpec(
            "sentence", "sentence", sentence,
            ("official", "dense"),
            {"official": ("TITLE", "CATEGORY"), "dense": ("TITLE", "CATEGORY")},
        ))
    # Opt-in article-body shadow callers may preserve source/report context as
    # separate paths.  Keeping each term separate avoids the FTS5 record-level
    # AND trap when an agency is stored in CATEGORY and a survey name in TITLE.
    source_terms = claim.get("source_terms")
    if not corrective_only and isinstance(source_terms, Sequence) and not isinstance(source_terms, (str, bytes)):
        seen_context: set[tuple[str, str]] = set()
        for index, value in enumerate(source_terms):
            if not isinstance(value, Mapping):
                continue
            role = re.sub(r"[^a-z0-9_]+", "_", str(value.get("role") or "context").casefold()).strip("_") or "context"
            text = re.sub(r"\s+", " ", str(value.get("text") or "")).strip()
            identity = (role, text)
            if not text or identity in seen_context:
                continue
            seen_context.add(identity)
            queries.append(QuerySpec(
                f"source_{role}_{index}", f"source_{role}", text,
                ("official", "bm25"),
                {
                    "official": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                    "bm25": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                },
            ))
    corrective_terms = claim.get("corrective_terms")
    if isinstance(corrective_terms, Sequence) and not isinstance(corrective_terms, (str, bytes)):
        seen_corrective: set[tuple[str, str]] = set()
        for index, value in enumerate(corrective_terms):
            if not isinstance(value, Mapping):
                continue
            case_id = re.sub(r"[^A-Za-z0-9_]+", "_", str(value.get("case_id") or "case")).strip("_")
            role = re.sub(r"[^a-z0-9_]+", "_", str(value.get("role") or "corrective").casefold()).strip("_")
            text = re.sub(r"\s+", " ", str(value.get("text") or "")).strip()
            identity = (role, text)
            if not text or identity in seen_corrective:
                continue
            seen_corrective.add(identity)
            queries.append(QuerySpec(
                f"corrective_{case_id}_{index}", role, text,
                ("official", "bm25", "dense"),
                {
                    "official": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                    "bm25": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                    "dense": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                },
            ))
    return tuple(queries)


def _normalize_path_hits(
    query: QuerySpec,
    channel: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    release_sha256: str,
    top_k: int,
) -> tuple[RetrievalHit, ...]:
    allowed = set(query.fields_by_channel[channel])
    normalized: list[RetrievalHit] = []
    for source_rank, row in enumerate(rows, 1):
        if source_rank > top_k:
            break
        table_key = str(row.get("table_key") or "").strip()
        record_id = str(row.get("record_id") or table_key).strip()
        field_name = str(row.get("field") or "TITLE").strip().upper()
        if not table_key or field_name not in allowed:
            continue
        score_value = row.get("score")
        normalized.append(RetrievalHit(
            query.query_id,
            query.role,
            channel,
            table_key,
            record_id,
            field_name,
            source_rank,
            None if score_value is None else float(score_value),
            release_sha256,
        ))
    # One child hit per (query, channel, table), with deterministic record tie-break.
    best: dict[str, RetrievalHit] = {}
    for hit in sorted(normalized, key=lambda row: (row.rank, row.record_id, row.table_key)):
        best.setdefault(hit.table_key, hit)
    return tuple(sorted(best.values(), key=lambda row: (row.rank, row.table_key, row.record_id)))


def retrieve_parallel(
    claim: Mapping[str, Any],
    channels: Mapping[str, SearchChannel],
    *,
    release_sha256_by_channel: Mapping[str, str],
    path_top_k: int = 20,
    union_top_k: int = 100,
) -> tuple[tuple[RrfCandidate, ...], dict[str, Any]]:
    """Execute all approved paths concurrently and flat-RRF their table hits."""
    register = build_query_register(claim)
    jobs = [(query, channel) for query in register for channel in query.channels]
    missing = sorted({channel for _, channel in jobs if channel not in channels})
    if missing:
        raise RuntimeError(f"SEARCH_CHANNEL_UNAVAILABLE:{','.join(missing)}")
    path_results: dict[tuple[str, str], tuple[RetrievalHit, ...]] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as executor:
        pending = {
            executor.submit(channels[channel], query, query.fields_by_channel[channel], path_top_k): (query, channel)
            for query, channel in jobs
        }
        for future in as_completed(pending):
            query, channel = pending[future]
            rows = future.result()
            path_results[(query.query_id, channel)] = _normalize_path_hits(
                query, channel, rows,
                release_sha256=str(release_sha256_by_channel.get(channel) or ""),
                top_k=path_top_k,
            )

    grouped: dict[str, list[RetrievalHit]] = {}
    for path in sorted(path_results):
        for hit in path_results[path]:
            grouped.setdefault(hit.table_key, []).append(hit)
    candidates = [
        RrfCandidate(
            table_key,
            sum(1.0 / (RRF_K + hit.rank) for hit in hits),
            min(hit.rank for hit in hits),
            tuple(sorted(hits, key=lambda hit: (hit.query_id, hit.channel, hit.rank, hit.record_id))),
        )
        for table_key, hits in grouped.items()
    ]
    ordered = tuple(sorted(candidates, key=lambda row: (-row.rrf_score, row.best_rank, row.table_key))[:union_top_k])
    audit = {
        "contract_version": CONTRACT_VERSION,
        "authority": "CANDIDATE_GENERATION_ONLY",
        "dimension_value_evidence": False,
        "dimension_completeness_evidence": False,
        "path_top_k": path_top_k,
        "union_top_k": union_top_k,
        "query_register_sha256": _sha([query.__dict__ for query in register]),
        "paths": {
            f"{query_id}:{channel}": len(path_results[(query_id, channel)])
            for query_id, channel in sorted(path_results)
        },
    }
    return ordered, audit


def build_candidate_passages(
    candidates: Sequence[RrfCandidate],
    catalog_records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    """Build reranker passages without DIMENSION values, article numbers or gold."""
    allowed = {"TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS", "DESCRIPTION"}
    by_table: dict[str, list[tuple[str, str, str]]] = {candidate.table_key: [] for candidate in candidates}
    for record in catalog_records:
        table_key = str(record.get("table_key") or "")
        field_name = str(record.get("field") or "").upper()
        text = str(record.get("text") or "").strip()
        if table_key in by_table and field_name in allowed and text:
            by_table[table_key].append((field_name, str(record.get("record_id") or ""), text))
    passages: list[dict[str, str]] = []
    for candidate in candidates:
        parts = [f"{field_name}: {text}" for field_name, _, text in sorted(by_table[candidate.table_key])]
        passages.append({"candidate_id": candidate.table_key, "text": "\n".join(parts)})
    return tuple(passages)


def rerank_top50(
    query: str,
    candidates: Sequence[RrfCandidate],
    passages: Sequence[Mapping[str, str]],
    reranker: Reranker,
    *,
    top_k: int = 50,
) -> tuple[RerankedCandidate, ...]:
    """Strictly rerank the RRF scope; missing/extra candidates fail closed."""
    if len(candidates) > 100:
        raise ValueError("reranker input exceeds RRF Top-100")
    expected = [candidate.table_key for candidate in candidates]
    passage_ids = [str(passage.get("candidate_id") or "") for passage in passages]
    if passage_ids != expected:
        raise ValueError("reranker passages must exactly follow candidate scope")
    raw = list(reranker.rerank(query, passages))
    returned = [str(row.get("candidate_id") or "") for row in raw]
    if len(returned) != len(expected) or set(returned) != set(expected):
        raise RuntimeError("RERANKER_INVALID_RESPONSE")
    rrf = {candidate.table_key: candidate.rrf_score for candidate in candidates}
    normalized = sorted(
        raw,
        key=lambda row: (-float(row["raw_logit"]), str(row["candidate_id"])),
    )
    return tuple(
        RerankedCandidate(
            str(row["candidate_id"]),
            rank,
            float(row["raw_logit"]),
            float(row["sigmoid_score"]),
            rrf[str(row["candidate_id"])],
        )
        for rank, row in enumerate(normalized[:top_k], 1)
    )


__all__ = [
    "CONTRACT_VERSION", "QuerySpec", "RetrievalHit", "RerankedCandidate", "RrfCandidate",
    "build_candidate_passages", "build_query_register", "rerank_top50", "retrieve_parallel",
]
