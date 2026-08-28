"""Pure terminal partitioning used by runtime barriers and audit reports.

The implementation is kept free of report rendering and file I/O so the
operational runtime does not depend on an evaluator or audit report module.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


TAXONOMY_ORDER = ("ENVIRONMENT", "L2", "L5", "RETRIEVAL", "PROFILE", "BINDING", "CELL", "COMPARATOR", "ANSWER")
RAW_STATUS_REASON_MAP = {
    "L2_UNAVAILABLE": "L2", "L2_READY": "L2", "L5_OUT_OF_SCOPE": "L5",
    "NO_ROUTED_TARGETS": "L5", "RETRIEVAL_UNAVAILABLE": "RETRIEVAL", "NO_CANDIDATES": "RETRIEVAL",
    "RERANKER_UNAVAILABLE": "RETRIEVAL", "PROFILE_UNAVAILABLE": "PROFILE", "PROFILE_REFRESH_NOT_FRESH": "PROFILE",
    "NO_COMPATIBLE_SERIES": "BINDING", "HOLD": "BINDING", "QUERY_PLAN_INVENTORY_INVALID": "BINDING",
    "QUERY_READY": "BINDING", "CELL_RESOLVED": "CELL", "NO_CELL": "CELL", "MULTIPLE_CELLS": "CELL",
    "CELL_QUERY_MISMATCH": "CELL", "COMPARATOR_UNVERIFIABLE": "COMPARATOR", "ANSWER_INVALID": "ANSWER",
    "ANSWER_UNSEALED_NUMBER": "ANSWER", "ANSWER_VERDICT_DRIFT": "ANSWER",
}
REGISTERED_HOLD_REASONS = {
    "NO_CANDIDATES", "SEARCH_UNAVAILABLE", "RETRIEVAL_UNAVAILABLE", "PROFILE_UNAVAILABLE",
    "PROFILE_REFRESH_NOT_FRESH", "PROFILE_INCOMPLETE", "NO_COMPATIBLE_SERIES",
    "QUERY_PLAN_INVENTORY_INVALID", "POPULATION_UNBOUND", "PERIOD_INVALID", "PERIOD_OUT_OF_RANGE",
    "PERIOD_FREQUENCY_MISMATCH", "PERIOD_UNKNOWN", "CLAIM_PROVENANCE_MISSING", "REGION_UNBOUND",
}
REGISTERED_STATUSES = set(RAW_STATUS_REASON_MAP) | {
    "HOLD", "L3_SCOPE_FAILED", "VERIFIED", "REFUTED", "PARTIAL", "UNVERIFIABLE",
    "ANSWER_READY", "SERVICE_READY", "SHADOW_READY",
}


class TerminalPartitionError(RuntimeError):
    """Raised for malformed terminal ledger input."""


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def normalize_stage_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if "resolution" in row and isinstance(row["resolution"], Mapping):
        result["resolution_raw"] = dict(row["resolution"])
        resolution = row["resolution"]
        result.setdefault("status", resolution.get("outcome"))
        result.setdefault("reason", resolution.get("hold_reason"))
    elif "resolution" in row and row.get("resolution") is not None:
        result.setdefault("status", row.get("resolution"))
    if "answer" in row and isinstance(row["answer"], Mapping):
        result["answer_raw"] = dict(row["answer"])
        answer = row["answer"]
        result.setdefault("target_id", answer.get("target_id"))
        answer_resolution = answer.get("resolution")
        if isinstance(answer_resolution, Mapping):
            result["answer_resolution_raw"] = dict(answer_resolution)
            answer_status = answer_resolution.get("outcome") or answer_resolution.get("status")
            if str(answer_status or "").upper() in {"NO_ROUTED_TARGETS", "L2_UNAVAILABLE"} or not result.get("status"):
                result["status"] = answer_status
            result.setdefault("reason", answer_resolution.get("hold_reason") or answer_resolution.get("reason"))
    status = str(result.get("status") or "").upper()
    original_target_id = str(result.get("target_id") or "")
    if status in {"NO_ROUTED_TARGETS", "L2_UNAVAILABLE"}:
        result["article_sentinel_reason"] = status
        result["target_id"] = ""
    target_id = str(result.get("target_id") or original_target_id)
    if not result.get("article_idx") and target_id:
        parts = target_id.split(":")
        if len(parts) >= 3 and parts[0] == "dev":
            result["article_idx"] = parts[1]
        elif parts and parts[0] == "article" and len(parts) > 1:
            result["article_idx"] = parts[1]
        elif parts:
            result["article_idx"] = parts[0]
    if not result.get("article_idx"):
        provenance = ((row.get("claim_core") or {}).get("atoms") or {})
        for atom in provenance.values():
            candidate = ((atom or {}).get("provenance") or {}).get("article_idx")
            if candidate is not None:
                result["article_idx"] = str(candidate)
                break
    if not result.get("article_idx"):
        raise TerminalPartitionError("AUDIT_CONTRACT_ERROR:ARTICLE_IDX_UNRECOVERABLE")
    return result


def _nested_status(value: Any) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        return "", ""
    status = value.get("outcome") or value.get("status") or value.get("verdict") or ""
    reason = value.get("hold_reason") or value.get("reason") or ""
    return str(status).upper(), str(reason).upper()


def _raw(row: Mapping[str, Any]) -> tuple[str, str, str]:
    stage = str(row.get("stage") or row.get("layer") or "").upper()
    resolution = row.get("resolution")
    if isinstance(resolution, Mapping):
        status_value = resolution.get("outcome") or resolution.get("status")
        reason_value = resolution.get("hold_reason") or resolution.get("reason")
    else:
        status_value = row.get("status") or resolution or row.get("outcome")
        reason_value = row.get("reason") or row.get("hold_reason")
    return stage, str(status_value or "").upper(), str(reason_value or "").upper()


def classify_taxonomy(row: Mapping[str, Any]) -> str:
    stage, status, reason = _raw(row)
    if status == "HOLD" and reason not in REGISTERED_HOLD_REASONS:
        raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:UNREGISTERED_REASON:{stage}:{status}:{reason}")
    if status and status not in REGISTERED_STATUSES:
        raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:UNREGISTERED_STATUS:{stage}:{status}:{reason}")
    if status == "HOLD" and reason in {"NO_CANDIDATES", "SEARCH_UNAVAILABLE", "RETRIEVAL_UNAVAILABLE"}:
        return "RETRIEVAL"
    if status == "HOLD" and reason.startswith("PROFILE"):
        return "PROFILE"
    if status in RAW_STATUS_REASON_MAP:
        return RAW_STATUS_REASON_MAP[status]
    if status == "HOLD":
        return "BINDING"
    text = " ".join((stage, status, reason))
    if any(token in text for token in ("PREFLIGHT", "ENVIRONMENT", "CONFIG_DRIFT", "SERVICE_UNAVAILABLE", "ENV_BLOCK")):
        return "ENVIRONMENT"
    if stage == "L2" or status.startswith("L2_") or "L2_" in text or "UNRESOLVED_SPAN" in text:
        return "L2"
    if stage in {"L3", "L4", "L5"} or status.startswith("L5_") or "OUT_OF_SCOPE" in text or "NOT_CLAIM" in text:
        return "L5"
    if status.startswith(("RETRIEVAL", "RERANKER")) or status in {"NO_CANDIDATES", "SEARCH_UNAVAILABLE"} or "RETRIEVAL" in text:
        return "RETRIEVAL"
    if status.startswith("PROFILE") or "PROFILE_" in text:
        return "PROFILE"
    if status in {"HOLD", "NO_COMPATIBLE_SERIES", "QUERY_PLAN_INVENTORY_INVALID", "BINDING_UNAVAILABLE"} or "BINDING" in text or "QUERY_READY" in text:
        return "BINDING"
    if status.startswith("CELL") or status in {"NO_CELL", "MULTIPLE_CELLS"} or "CELL" in text:
        return "CELL"
    if status.startswith("COMPARATOR") or (stage == "COMPARATOR" and status in {"VERIFIED", "REFUTED", "PARTIAL", "UNVERIFIABLE"}) or status in {"VERIFIED", "REFUTED", "PARTIAL", "UNVERIFIABLE"} and ("comparison" in row or "comparator" in row):
        return "COMPARATOR"
    if status.startswith("ANSWER") or "answer" in row:
        return "ANSWER"
    raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:UNREGISTERED_STATUS:{stage}:{status}:{reason}")


def _terminal_taxonomy(row: Mapping[str, Any]) -> str:
    upstream_stage, upstream_status, upstream_reason = _raw(row)
    if upstream_status == "HOLD":
        return classify_taxonomy(row)
    cache_status, _ = _nested_status(row.get("cell_cache_match"))
    if cache_status in {"NO_CELL", "CELL_UNAVAILABLE"}:
        return "CELL"
    final_answer = row.get("final_answer")
    if isinstance(final_answer, Mapping):
        final_comparison = final_answer.get("comparison")
        final_verdict = str(final_answer.get("verdict") or "").upper()
        if isinstance(final_comparison, Mapping) and final_comparison.get("match") is not None:
            return "COMPARATOR"
        if final_verdict in {"REFUTED", "VERIFIED", "PARTIAL"}:
            return "COMPARATOR"
    for key, fallback in (("comparison", "COMPARATOR"), ("comparator", "COMPARATOR"), ("final_answer", "ANSWER"), ("answer", "ANSWER"), ("cell", "CELL")):
        status, reason = _nested_status(row.get(key))
        if status and status not in {"QUERY_READY", "L2_READY", "READY", "RESOLVED"}:
            return classify_taxonomy({"stage": fallback, "status": status, "reason": reason, key: row.get(key)})
        if status in {"CELL_RESOLVED", "NO_CELL", "MULTIPLE_CELLS", "CELL_QUERY_MISMATCH"}:
            return "CELL"
    if isinstance(final_answer, Mapping):
        comparison = final_answer.get("comparison")
        verdict = str(final_answer.get("verdict") or "").upper()
        if isinstance(comparison, Mapping) and comparison.get("match") is not None:
            return "COMPARATOR"
        if verdict in {"REFUTED", "VERIFIED", "PARTIAL"}:
            return "COMPARATOR"
        if verdict in {"UNVERIFIABLE", "INVALID"}:
            return "ANSWER"
    return classify_taxonomy({**dict(row), "stage": upstream_stage, "status": upstream_status, "reason": upstream_reason})


def _article_map(articles: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for article in articles:
        article_id = str(article.get("article_idx") or article.get("article_id") or "")
        if not article_id or article_id in result:
            raise TerminalPartitionError("AUDIT_CONTRACT_ERROR:ARTICLE_ID_DUPLICATE_OR_MISSING")
        result[article_id] = article
    return result


def build_terminal_partition(
    articles: Iterable[Mapping[str, Any]],
    stage_rows: Iterable[Mapping[str, Any]],
    *,
    run_id: str = "run",
    preflight_error: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    amap = _article_map(articles)
    if preflight_error:
        raw = {"status": "ENVIRONMENT", "reason": str(preflight_error)}
        universe = [{"terminal_id": f"env:{run_id}", "article_idx": None, "sentinel": True, "taxonomy": "ENVIRONMENT", "raw": raw, "terminal_row_sha256": _sha(raw)}]
        return universe, {"run_id": run_id, "articles": 0, "terminal_rows": 1, "sentinels": 1, "target_rows": 0, "preflight_error": str(preflight_error)}
    rows = list(stage_rows)
    if not rows:
        raise TerminalPartitionError("AUDIT_CONTRACT_ERROR:ZERO_LEDGER_ROWS")
    rows_by_article: dict[str, list[Mapping[str, Any]]] = {key: [] for key in amap}
    for raw_row in rows:
        row = normalize_stage_row(raw_row)
        article_id = str(row.get("article_idx") or row.get("article_id") or "")
        if article_id not in amap:
            raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:ORPHAN_ARTICLE:{article_id}")
        rows_by_article[article_id].append(row)
    universe: list[dict[str, Any]] = []
    for article_id in sorted(amap):
        article_rows = rows_by_article[article_id]
        l2_failed = any(str(row.get("article_sentinel_reason") or _raw(row)[1]).startswith("L2_UNAVAILABLE") for row in article_rows)
        no_routed = any(str(row.get("article_sentinel_reason") or _raw(row)[1]) == "NO_ROUTED_TARGETS" for row in article_rows)
        targets = [row for row in article_rows if str(row.get("target_id") or "")]
        if not article_rows:
            raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:ARTICLE_LEDGER_MISSING:{article_id}")
        explicit_sentinels = [row for row in article_rows if row.get("article_sentinel_reason")]
        if explicit_sentinels and targets:
            raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:SENTINEL_TARGET_CONTRADICTION:{article_id}")
        if l2_failed and targets:
            raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:L2_TARGET_CONTRADICTION:{article_id}")
        if l2_failed:
            evidence = next(row for row in article_rows if str(row.get("article_sentinel_reason") or _raw(row)[1]).startswith("L2_UNAVAILABLE"))
            universe.append({"terminal_id": f"article:{article_id}:l2", "article_idx": article_id, "sentinel": True, "taxonomy": "L2", "raw": dict(evidence), "terminal_row_sha256": _sha(evidence)})
        elif no_routed:
            evidence = next((row for row in article_rows if str(row.get("article_sentinel_reason") or _raw(row)[1]) == "NO_ROUTED_TARGETS"), {"status": "NO_ROUTED_TARGETS", "article_idx": article_id})
            universe.append({"terminal_id": f"article:{article_id}:no_target", "article_idx": article_id, "sentinel": True, "taxonomy": "L5", "raw": dict(evidence), "terminal_row_sha256": _sha(evidence)})
        elif not targets:
            raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:TARGET_OR_SENTINEL_MISSING:{article_id}")
        else:
            grouped: dict[str, list[Mapping[str, Any]]] = {}
            for row in targets:
                grouped.setdefault(str(row["target_id"]), []).append(row)
            for target_id, target_rows in grouped.items():
                if len(target_rows) > 1:
                    terminal_rows = [row for row in target_rows if _raw(row)[1] not in {"QUERY_READY", "L2_READY", "READY"} or any(_nested_status(row.get(key))[0] in {"CELL_RESOLVED", "NO_CELL", "MULTIPLE_CELLS", "CELL_QUERY_MISMATCH", "REFUTED", "VERIFIED", "PARTIAL", "ANSWER_INVALID", "UNVERIFIABLE"} for key in ("cell", "comparison", "comparator", "answer", "final_answer"))]
                    if len(terminal_rows) > 1:
                        raise TerminalPartitionError(f"AUDIT_CONTRACT_ERROR:DUPLICATE_TARGET:{target_id}")
                chosen = None
                for row in target_rows:
                    raw_status = _raw(row)[1]
                    nested_terminal = any(_nested_status(row.get(key))[0] in {"CELL_RESOLVED", "NO_CELL", "MULTIPLE_CELLS", "CELL_QUERY_MISMATCH", "COMPARATOR_UNVERIFIABLE", "ANSWER_INVALID", "ANSWER_UNSEALED_NUMBER", "ANSWER_VERDICT_DRIFT"} for key in ("cell", "comparator", "comparison", "answer"))
                    if raw_status not in {"QUERY_READY", "L2_READY", "READY"} or nested_terminal:
                        chosen = row
                        break
                chosen = chosen or target_rows[-1]
                universe.append({"terminal_id": target_id, "article_idx": article_id, "sentinel": False, "target_id": target_id, "taxonomy": _terminal_taxonomy(chosen), "raw": dict(chosen), "terminal_row_sha256": _sha(chosen)})
    return universe, {"run_id": run_id, "articles": len(amap), "terminal_rows": len(universe), "sentinels": sum(bool(row["sentinel"]) for row in universe), "target_rows": sum(not row["sentinel"] for row in universe)}


__all__ = ["TerminalPartitionError", "build_terminal_partition", "classify_taxonomy", "normalize_stage_row"]
