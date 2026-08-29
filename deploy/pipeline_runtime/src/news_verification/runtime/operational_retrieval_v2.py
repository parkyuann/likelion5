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


CONTRACT_VERSION = "operational-retrieval-v5-item-official"
QUERY_REGISTER_VERSION = "six-path-v3-item-official-optin"
DISABLED_PATHS = (
    {"path": "sentence:official", "reason": "DISABLED_ZERO_YIELD_BASELINE"},
)
RRF_K = 60


def query_register_contract() -> dict[str, Any]:
    """Return the immutable shape of the active default register.

    Query text is request-specific, but the enabled path contract is runtime
    state.  Continuation artifacts attest both so a forged path identity cannot
    be accepted merely because its self-reported hash is well formed.
    """
    return {
        "version": QUERY_REGISTER_VERSION,
        "kind": "default",
        "enabled_paths": [
            "indicator:official", "indicator:bm25", "indicator:dense",
            "item:bm25", "item:dense", "sentence:dense",
        ],
        "optional_paths": [
            {
                "path": "item:item_official",
                "condition": "claim._include_item_official=true and item != indicator",
                "authority": "CANDIDATE_GENERATION_ONLY",
            },
        ],
        "disabled": list(DISABLED_PATHS),
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def query_register_identity_payload(
    register: Sequence["QuerySpec"],
    *,
    register_kind: str,
) -> dict[str, Any]:
    """Return the canonical payload whose digest binds a register to a run."""
    return {
        "version": QUERY_REGISTER_VERSION,
        "kind": str(register_kind),
        "enabled": [query.__dict__ for query in register],
        "disabled": list(DISABLED_PATHS),
    }


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


def _claim_text(value: Any) -> str:
    """Normalize scalar or list-shaped routed fields without inventing text."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(str(part).strip() for part in value if str(part).strip()).strip()
    return str(value or "").strip()


def build_query_register(claim: Mapping[str, Any]) -> tuple[QuerySpec, ...]:
    """Build only the approved default six-path register.

    Source/report context and corrective terms are deliberately excluded here.
    They have separate registers and can only affect a candidate set through an
    explicitly receipted union in the orchestrator.
    """
    indicator = _claim_text(claim.get("indicator"))
    explicit_item = _claim_text(claim.get("item"))
    item = explicit_item or indicator
    include_item_official = bool(
        claim.get("_include_item_official") is True
        and explicit_item
        and explicit_item != indicator
    )
    sentence = str(claim.get("sentence") or "").strip()
    # The sentence path contributes lexical context, never the asserted number.
    # Strip explicit numeric expressions before both retrieval and reranking.
    sentence = re.sub(r"[+-]?\d[\d,.]*(?:조|억|천만|백만|만|천)?(?:원|달러|명|가구|개|%p|%)?", " ", sentence)
    sentence = re.sub(r"\s+", " ", sentence).strip()
    queries: list[QuerySpec] = []
    if indicator:
        queries.append(QuerySpec(
            "indicator", "indicator", indicator,
            ("official", "bm25", "dense"),
            {
                "official": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                "bm25": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
                "dense": ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"),
            },
        ))
    if item:
        item_channels = ("bm25", "dense", "item_official") if include_item_official else ("bm25", "dense")
        item_fields = {"bm25": ("ITEM",), "dense": ("ITEM",)}
        if include_item_official:
            item_fields["item_official"] = ("ITEM",)
        queries.append(QuerySpec(
            "item", "item", item,
            item_channels,
            item_fields,
        ))
    if sentence:
        queries.append(QuerySpec(
            "sentence", "sentence", sentence,
            ("dense",),
            {"dense": ("TITLE", "CATEGORY")},
        ))
    return tuple(queries)


def build_context_query_register(claim: Mapping[str, Any]) -> tuple[QuerySpec, ...]:
    """Build source/report context paths; never include them in base six-path."""
    queries: list[QuerySpec] = []
    source_terms = claim.get("source_terms")
    if isinstance(source_terms, Sequence) and not isinstance(source_terms, (str, bytes)):
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
    return tuple(queries)


def build_corrective_query_register(claim: Mapping[str, Any]) -> tuple[QuerySpec, ...]:
    """Build the one bounded corrective register, isolated from default paths."""
    queries: list[QuerySpec] = []
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


_REGISTER_BUILDERS: Mapping[str, Callable[[Mapping[str, Any]], tuple[QuerySpec, ...]]] = {
    "default": build_query_register,
    "context": build_context_query_register,
    "corrective": build_corrective_query_register,
}


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
        row_release = row.get("release_sha256")
        if row_release not in (None, "") and str(row_release) != str(release_sha256):
            raise ValueError("RETRIEVAL_RELEASE_MISMATCH")
        if not table_key or field_name not in allowed:
            raise ValueError("RETRIEVAL_PATH_CONTRACT_INVALID")
        score_value = row.get("score")
        if score_value is not None:
            try:
                score_value = float(score_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("RETRIEVAL_PATH_CONTRACT_INVALID") from exc
        normalized.append(RetrievalHit(
            query.query_id,
            query.role,
            channel,
            table_key,
            record_id,
            field_name,
            source_rank,
            score_value,
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
    timeout_seconds: float | None = None,
    channel_allowlist: frozenset[str] | None = None,
    register_kind: str = "default",
) -> tuple[tuple[RrfCandidate, ...], dict[str, Any]]:
    """Execute one isolated register concurrently and flat-RRF its table hits."""
    builder = _REGISTER_BUILDERS.get(str(register_kind))
    if builder is None:
        raise ValueError("RETRIEVAL_REGISTER_KIND_INVALID")
    register = builder(claim)
    jobs = [
        (query, channel) for query in register for channel in query.channels
        if channel_allowlist is None or channel in channel_allowlist
    ]
    if not jobs:
        raise RuntimeError("SPECULATIVE_NATIVE_TIMEOUT_UNSUPPORTED")
    missing = sorted({channel for _, channel in jobs if channel not in channels})
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise RuntimeError("SPECULATIVE_NATIVE_TIMEOUT_UNSUPPORTED")
    path_results: dict[tuple[str, str], tuple[RetrievalHit, ...]] = {}
    path_status: dict[str, str] = {}
    path_errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as executor:
        pending = {}
        for query, channel in jobs:
            path_key = f"{query.query_id}:{channel}"
            if channel in missing:
                path_status[path_key] = "FAILED_TRANSPORT"
                path_errors[path_key] = "SEARCH_CHANNEL_UNAVAILABLE"
                continue
            callback = getattr(channels[channel], "speculative", None) if timeout_seconds is not None else channels[channel]
            if timeout_seconds is not None and not callable(callback):
                path_status[path_key] = "FAILED_TIMEOUT"
                path_errors[path_key] = "SPECULATIVE_NATIVE_TIMEOUT_UNSUPPORTED"
                continue
            pending[executor.submit(
                callback,
                query,
                query.fields_by_channel[channel],
                path_top_k,
                **({"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}),
            )] = (query, channel)
        for future in as_completed(pending):
            query, channel = pending[future]
            path_key = f"{query.query_id}:{channel}"
            try:
                rows = future.result()
                path_results[(query.query_id, channel)] = _normalize_path_hits(
                    query, channel, rows,
                    release_sha256=str(release_sha256_by_channel.get(channel) or ""),
                    top_k=path_top_k,
                )
            except TimeoutError:
                path_status[path_key] = "FAILED_TIMEOUT"
                path_errors[path_key] = "TIMEOUT"
            except (ValueError, TypeError, KeyError) as exc:
                path_status[path_key] = "FAILED_CONTRACT"
                path_errors[path_key] = str(exc.args[0]) if exc.args else "RETRIEVAL_PATH_CONTRACT_INVALID"
            except Exception as exc:  # noqa: BLE001 - path isolation boundary
                path_status[path_key] = "FAILED_TRANSPORT"
                path_errors[path_key] = type(exc).__name__
            else:
                path_status[path_key] = "OK" if path_results[(query.query_id, channel)] else "EMPTY"

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
    register_identity_payload = query_register_identity_payload(register, register_kind=register_kind)
    audit = {
        "contract_version": CONTRACT_VERSION,
        "query_register_version": QUERY_REGISTER_VERSION,
        "query_register_kind": register_kind,
        "disabled_paths": list(DISABLED_PATHS),
        "authority": "CANDIDATE_GENERATION_ONLY",
        "dimension_value_evidence": False,
        "dimension_completeness_evidence": False,
        "path_top_k": path_top_k,
        "union_top_k": union_top_k,
        "query_register_sha256": _sha(register_identity_payload),
        "query_register_contract_sha256": _sha({
            **query_register_contract(),
            "kind": register_kind,
        }) if register_kind == "default" else _sha({
            "version": QUERY_REGISTER_VERSION,
            "kind": register_kind,
            "enabled_paths": [f"{query.query_id}:{channel}" for query, channel in jobs],
            "disabled": list(DISABLED_PATHS),
        }),
        "query_register_payload": [query.__dict__ for query in register],
        "query_register_identity_payload": register_identity_payload,
        "paths": {
            f"{query.query_id}:{channel}": len(path_results.get((query.query_id, channel), ()))
            for query, channel in sorted(jobs, key=lambda item: (item[0].query_id, item[1]))
        },
        "path_status": {key: path_status[key] for key in sorted(path_status)},
        "path_errors": {key: path_errors[key] for key in sorted(path_errors)},
        "successful_path_count": sum(status in {"OK", "EMPTY"} for status in path_status.values()),
        "failed_path_count": sum(status.startswith("FAILED_") for status in path_status.values()),
        "all_paths_failed": bool(jobs) and not any(status in {"OK", "EMPTY"} for status in path_status.values()),
    }
    audit["retrieval_semantic_sha256"] = _sha({
        "contract_version": CONTRACT_VERSION,
        "query_register_kind": register_kind,
        "query_register_sha256": audit["query_register_sha256"],
        "release_sha256_by_channel": dict(sorted((str(k), str(v)) for k, v in release_sha256_by_channel.items())),
        "paths": audit["paths"],
        "path_status": audit["path_status"],
        "candidate_membership": [candidate.table_key for candidate in ordered],
    })
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
    # The attested Korean service accepts at most 50 candidates.  RRF still
    # computes the full Top-100 union; only its deterministic leading Top-50
    # enters the model, preserving the downstream resolver's Top-50 contract.
    scope = tuple(candidates[:top_k])
    expected = [candidate.table_key for candidate in scope]
    passage_ids = [str(passage.get("candidate_id") or "") for passage in passages[:top_k]]
    if passage_ids != expected:
        raise ValueError("reranker passages must exactly follow candidate scope")
    raw = list(reranker.rerank(query, passages[:top_k]))
    returned = [str(row.get("candidate_id") or "") for row in raw]
    if len(returned) != len(expected) or set(returned) != set(expected):
        raise RuntimeError("RERANKER_INVALID_RESPONSE")
    rrf = {candidate.table_key: candidate.rrf_score for candidate in scope}
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
    "CONTRACT_VERSION", "QUERY_REGISTER_VERSION", "DISABLED_PATHS", "query_register_contract", "QuerySpec", "RetrievalHit", "RerankedCandidate", "RrfCandidate",
    "build_candidate_passages", "build_query_register", "build_context_query_register",
    "build_corrective_query_register", "rerank_top50", "retrieve_parallel",
]
