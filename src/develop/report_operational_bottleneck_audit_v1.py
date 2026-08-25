"""Operational bottleneck audit, blind review scaffolds, and shadow comparison.

This module is deliberately artifact-only: it never calls a model or service.
Its JSON and Markdown reports are rendered from one canonical dictionary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


TAXONOMY_ORDER = ("ENVIRONMENT", "L2", "L5", "RETRIEVAL", "PROFILE", "BINDING", "CELL", "COMPARATOR", "ANSWER")
TAXONOMY_RANK = {value: index for index, value in enumerate(TAXONOMY_ORDER)}
# Public contract table: every raw terminal namespace emitted by operational
# v2 is assigned before the heuristic fallback below is considered.
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
    "NO_CANDIDATES", "SEARCH_UNAVAILABLE", "RETRIEVAL_UNAVAILABLE",
    "PROFILE_UNAVAILABLE", "PROFILE_REFRESH_NOT_FRESH", "PROFILE_INCOMPLETE",
    "NO_COMPATIBLE_SERIES", "QUERY_PLAN_INVENTORY_INVALID", "POPULATION_UNBOUND",
    "PERIOD_INVALID", "PERIOD_OUT_OF_RANGE", "PERIOD_FREQUENCY_MISMATCH",
    "PERIOD_UNKNOWN", "CLAIM_PROVENANCE_MISSING", "REGION_UNBOUND",
}
REGISTERED_STATUSES = set(RAW_STATUS_REASON_MAP) | {
    "HOLD", "L3_SCOPE_FAILED", "VERIFIED", "REFUTED", "PARTIAL", "UNVERIFIABLE",
    "ANSWER_READY", "SERVICE_READY", "SHADOW_READY",
}
TECHNICAL_ENUM = ("CORRECT_ABSTENTION", "RECOVERABLE_FROM_ARTICLE_EVIDENCE", "UNCERTAIN")
ANSWER_FIELDS = ("clarity", "reason_specificity", "evidence_traceability", "limitation_actionability")
BLIND_FIELDS = {"review_id", "article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels", *ANSWER_FIELDS, "harmful_overclaim", "overall_useful", "notes"}
FORBIDDEN_BLIND_TERMS = {"stage", "status", "reason", "rank", "score", "profile", "query", "table", "cell", "comparator", "run_id", "target_id", "article_idx"}
REVIEW_FORBIDDEN_KEYS = {"target_id", "query_plan", "answer_contract_valid", "citation_contract_valid", "verdict_contract_valid", "upstream_blocker_moved", "packet_sha256", "answer_sha256"}
REVIEW_INTERNAL_KEYS = {"stage", "status", "reason", "rank", "score", "profile", "table", "cell", "comparator"}


class AuditContractError(RuntimeError):
    pass


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditContractError(f"AUDIT_INPUT_INVALID:{path}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditContractError(f"AUDIT_INPUT_INVALID:{path}") from exc


def normalize_stage_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize replay/live ledger schemas without dropping raw evidence."""
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
    # Runner answer rows sometimes carry an article-level target or a stale
    # target alongside the terminal routing/L2 sentinel.  It is evidence, not
    # a real routed target; retain it in answer_raw but never partition it as
    # a target row.
    original_target_id = str(result.get("target_id") or "")
    if status in {"NO_ROUTED_TARGETS", "L2_UNAVAILABLE"}:
        result["article_sentinel_reason"] = status
        result["target_id"] = ""
    target_id = str(result.get("target_id") or original_target_id)
    if not result.get("article_idx") and target_id:
        parts = target_id.split(":")
        # Replay IDs are dev:<article_idx>:...; live IDs are
        # <article_idx>:... . Preserve the original ID in target_id.
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
        raise AuditContractError("AUDIT_CONTRACT_ERROR:ARTICLE_IDX_UNRECOVERABLE")
    return result


def _nested_status(value: Any) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        return "", ""
    status = value.get("outcome") or value.get("status") or value.get("verdict") or ""
    reason = value.get("hold_reason") or value.get("reason") or ""
    return str(status).upper(), str(reason).upper()


def _terminal_taxonomy(row: Mapping[str, Any]) -> str:
    """Classify the latest terminal evidence in one canonical ledger row.

    QUERY_READY/L2_READY are successful upstream transitions.  A later cell,
    comparator, or answer outcome in the same row therefore wins over the
    earlier transition rather than being masked as BINDING.
    """
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
            probe = {"stage": fallback, "status": status, "reason": reason, key: row.get(key)}
            return classify_taxonomy(probe)
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


def _raw(row: Mapping[str, Any]) -> tuple[str, str, str]:
    stage = str(row.get("stage") or row.get("layer") or "").upper()
    resolution = row.get("resolution")
    if isinstance(resolution, Mapping):
        status_value = resolution.get("outcome") or resolution.get("status")
        reason_value = resolution.get("hold_reason") or resolution.get("reason")
    else:
        status_value = row.get("status") or resolution or row.get("outcome")
        reason_value = row.get("reason") or row.get("hold_reason")
    status = str(status_value or "").upper()
    reason = str(reason_value or "").upper()
    return stage, status, reason


def classify_taxonomy(row: Mapping[str, Any]) -> str:
    """Exhaustive raw status/reason classification; unknown is a hard error."""
    stage, status, reason = _raw(row)
    if status == "HOLD" and reason not in REGISTERED_HOLD_REASONS:
        raise AuditContractError(f"AUDIT_CONTRACT_ERROR:UNREGISTERED_REASON:{stage}:{status}:{reason}")
    if status and status not in REGISTERED_STATUSES:
        raise AuditContractError(f"AUDIT_CONTRACT_ERROR:UNREGISTERED_STATUS:{stage}:{status}:{reason}")
    if status == "HOLD" and reason in {"NO_CANDIDATES", "SEARCH_UNAVAILABLE", "RETRIEVAL_UNAVAILABLE"}:
        return "RETRIEVAL"
    if status == "HOLD" and reason.startswith("PROFILE"):
        return "PROFILE"
    if status in RAW_STATUS_REASON_MAP:
        return RAW_STATUS_REASON_MAP[status]
    if status == "HOLD":
        # Late-binding HOLD is a binding terminal, never an answer failure.
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
    raise AuditContractError(f"AUDIT_CONTRACT_ERROR:UNREGISTERED_STATUS:{stage}:{status}:{reason}")


def _article_map(articles: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result = {}
    for article in articles:
        article_id = str(article.get("article_idx") or article.get("article_id") or "")
        if not article_id or article_id in result:
            raise AuditContractError("AUDIT_CONTRACT_ERROR:ARTICLE_ID_DUPLICATE_OR_MISSING")
        result[article_id] = article
    return result


def _answer_view(answer: Mapping[str, Any], article: Mapping[str, Any] | None) -> dict[str, Any]:
    claim = answer.get("claim") if isinstance(answer.get("claim"), Mapping) else {}
    return {
        "article_title": str(article.get("title") or article.get("article_title") or "") if article else "",
        "claim_headline": str(answer.get("headline") or claim.get("indicator") or ""),
        "claim_text": str(answer.get("claim_text") or answer.get("sentence") or claim.get("sentence") or ""),
        "explanation": str(answer.get("explanation") or ""),
        "limitation": str(answer.get("limitation") or answer.get("next_action") or answer.get("reason_detail") or ""),
        "citation_labels": list(answer.get("citation_labels") or answer.get("citation_ids") or []),
    }


def _sealed_answer_contract(answer: Mapping[str, Any] | None) -> dict[str, bool]:
    answer = answer or {}
    verdict = str(answer.get("verdict") or "").upper()
    citations = answer.get("citation_ids") or answer.get("citation_labels")
    return {"answer_contract_valid": answer.get("answer_contract") == "operational-answer-v2", "citation_contract_valid": isinstance(citations, list) and bool(citations), "verdict_contract_valid": verdict in {"VERIFIED", "REFUTED", "PARTIAL", "UNVERIFIABLE"}}


def build_terminal_partition(articles: Iterable[Mapping[str, Any]], stage_rows: Iterable[Mapping[str, Any]], *, run_id: str = "run", preflight_error: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    amap = _article_map(articles)
    if preflight_error:
        universe = [{"terminal_id": f"env:{run_id}", "article_idx": None, "sentinel": True, "taxonomy": "ENVIRONMENT", "raw": {"status": "ENVIRONMENT", "reason": str(preflight_error)}, "terminal_row_sha256": _sha({"status": "ENVIRONMENT", "reason": str(preflight_error)})}]
        return universe, {"run_id": run_id, "articles": 0, "terminal_rows": 1, "sentinels": 1, "target_rows": 0, "preflight_error": str(preflight_error)}
    stage_rows = list(stage_rows)
    if not stage_rows:
        raise AuditContractError("AUDIT_CONTRACT_ERROR:ZERO_LEDGER_ROWS")
    rows_by_article: dict[str, list[Mapping[str, Any]]] = {key: [] for key in amap}
    for raw_row in stage_rows:
        row = normalize_stage_row(raw_row)
        article_id = str(row.get("article_idx") or row.get("article_id") or "")
        if article_id not in amap:
            raise AuditContractError(f"AUDIT_CONTRACT_ERROR:ORPHAN_ARTICLE:{article_id}")
        rows_by_article[article_id].append(row)
    universe: list[dict[str, Any]] = []
    for article_id, article in sorted(amap.items()):
        rows = rows_by_article[article_id]
        l2_failed = any(str(row.get("article_sentinel_reason") or _raw(row)[1]).startswith("L2_UNAVAILABLE") for row in rows)
        no_routed = any(str(row.get("article_sentinel_reason") or _raw(row)[1]) == "NO_ROUTED_TARGETS" for row in rows)
        targets = [row for row in rows if str(row.get("target_id") or "")]
        if not rows:
            raise AuditContractError(f"AUDIT_CONTRACT_ERROR:ARTICLE_LEDGER_MISSING:{article_id}")
        explicit_sentinels = [row for row in rows if row.get("article_sentinel_reason")]
        if explicit_sentinels and targets:
            raise AuditContractError(f"AUDIT_CONTRACT_ERROR:SENTINEL_TARGET_CONTRADICTION:{article_id}")
        if l2_failed and targets:
            raise AuditContractError(f"AUDIT_CONTRACT_ERROR:L2_TARGET_CONTRADICTION:{article_id}")
        if l2_failed:
            terminal_id = f"article:{article_id}:l2"
            evidence = next((row for row in rows if str(row.get("article_sentinel_reason") or _raw(row)[1]).startswith("L2_UNAVAILABLE")), rows[0] if rows else {"status": "L2_UNAVAILABLE"})
            universe.append({"terminal_id": terminal_id, "article_idx": article_id, "sentinel": True, "taxonomy": "L2", "raw": dict(evidence), "terminal_row_sha256": _sha(evidence)})
        elif no_routed:
            terminal_id = f"article:{article_id}:no_target"
            evidence = next((row for row in rows if str(row.get("article_sentinel_reason") or _raw(row)[1]) == "NO_ROUTED_TARGETS"), {"status": "NO_ROUTED_TARGETS", "article_idx": article_id})
            universe.append({"terminal_id": terminal_id, "article_idx": article_id, "sentinel": True, "taxonomy": "L5", "raw": dict(evidence), "terminal_row_sha256": _sha(evidence)})
        elif not targets:
            raise AuditContractError(f"AUDIT_CONTRACT_ERROR:TARGET_OR_SENTINEL_MISSING:{article_id}")
        else:
            grouped: dict[str, list[Mapping[str, Any]]] = {}
            for row in targets:
                grouped.setdefault(str(row["target_id"]), []).append(row)
            for target_id, target_rows in grouped.items():
                if len(target_rows) > 1:
                    terminal_rows = [row for row in target_rows if _raw(row)[1] not in {"QUERY_READY", "L2_READY", "READY"} or any(_nested_status(row.get(key))[0] in {"CELL_RESOLVED", "NO_CELL", "MULTIPLE_CELLS", "CELL_QUERY_MISMATCH", "REFUTED", "VERIFIED", "PARTIAL", "ANSWER_INVALID", "UNVERIFIABLE"} for key in ("cell", "comparison", "comparator", "answer", "final_answer"))]
                    if len(terminal_rows) > 1:
                        raise AuditContractError(f"AUDIT_CONTRACT_ERROR:DUPLICATE_TARGET:{target_id}")
                chosen = None
                for row in target_rows:
                    raw_status = _raw(row)[1]
                    # QUERY_READY/L2_READY are successful transitions, not a
                    # terminal blocker.  A later nested cell/comparator/
                    # answer outcome remains eligible in this same row.
                    nested_terminal = any(_nested_status(row.get(key))[0] in {
                        "CELL_RESOLVED", "NO_CELL", "MULTIPLE_CELLS", "CELL_QUERY_MISMATCH",
                        "COMPARATOR_UNVERIFIABLE", "ANSWER_INVALID", "ANSWER_UNSEALED_NUMBER", "ANSWER_VERDICT_DRIFT",
                    } for key in ("cell", "comparator", "comparison", "answer"))
                    if raw_status not in {"QUERY_READY", "L2_READY", "READY"} or nested_terminal:
                        chosen = row
                        break
                chosen = chosen or target_rows[-1]
                universe.append({"terminal_id": target_id, "article_idx": article_id, "sentinel": False, "target_id": target_id, "taxonomy": _terminal_taxonomy(chosen), "raw": dict(chosen), "terminal_row_sha256": _sha(chosen)})
    return universe, {"run_id": run_id, "articles": len(amap), "terminal_rows": len(universe), "sentinels": sum(bool(row["sentinel"]) for row in universe), "target_rows": sum(not row["sentinel"] for row in universe)}


def _opaque_id(used: set[str], seed: Any | None = None) -> str:
    """Stable opaque identifier derived from sealed inputs, never semantics."""
    seed = seed if seed is not None else len(used)
    value = "r_" + _sha(seed)[:24]
    suffix = 0
    while value in used:
        suffix += 1
        value = "r_" + _sha((seed, suffix))[:24]
    used.add(value)
    return value


def build_review_scaffolds(universe: Iterable[Mapping[str, Any]], answers: Iterable[Mapping[str, Any]], articles: Iterable[Mapping[str, Any]], *, run_id: str, run_role: str = "baseline") -> dict[str, Any]:
    amap = _article_map(articles)
    used: set[str] = set()
    technical: list[dict[str, Any]] = []
    answer_review: list[dict[str, Any]] = []
    bindings: dict[str, dict[str, Any]] = {}
    answer_map = {str(row.get("target_id") or row.get("article_idx") or ""): row for row in answers if row.get("target_id") or row.get("article_idx")}
    for item in universe:
        review_id = _opaque_id(used, (run_id, run_role, item["terminal_id"], item["terminal_row_sha256"]))
        raw = dict(item.get("raw") or {})
        technical.append({"review_id": review_id, "terminal_id": item["terminal_id"], "article_idx": item["article_idx"], "taxonomy": item["taxonomy"], "evidence": {"evidence_sha256": _sha(raw)}, "terminal_row_sha256": item["terminal_row_sha256"], "judgment": None, "notes": ""})
        answer = answer_map.get(str(item.get("target_id") or ""))
        bindings[review_id] = {"run_id": run_id, "run_role": run_role, "canonical_id": item["terminal_id"], "baseline_terminal_row_sha256": item["terminal_row_sha256"], "packet_sha256": _sha(_answer_view(answer or {}, amap.get(str(item["article_idx"])) )) if answer else None, "answer_sha256": _sha(answer) if answer else None, "sealed_contract_validity": _sealed_answer_contract(answer)}
        if answer is not None and not item.get("sentinel"):
            packet = _answer_view(answer, amap.get(str(item["article_idx"])))
            answer_review.append({"review_id": review_id, **packet, **{field: None for field in ANSWER_FIELDS}, "harmful_overclaim": None, "overall_useful": None, "notes": ""})
    return {"answer_review": answer_review, "technical_review": technical, "review_bindings": bindings}


def validate_review_rows(rows: Iterable[Mapping[str, Any]], expected: Iterable[Mapping[str, Any]], *, technical: bool = False, paired: bool = False) -> dict[str, Any]:
    expected_rows = list(expected)
    expected_ids = {str(row.get("review_id") or "") for row in expected_rows}
    expected_by_id = {str(row.get("review_id") or ""): row for row in expected_rows}
    got: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if technical:
            allowed_keys = {"review_id", "terminal_id", "article_idx", "taxonomy", "evidence", "terminal_row_sha256", "judgment", "notes"}
        elif paired:
            allowed_keys = {"review_id", "pair_id", "article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels", *ANSWER_FIELDS, "harmful_overclaim", "overall_useful", "notes"}
        else:
            allowed_keys = {"review_id", "article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels", *ANSWER_FIELDS, "harmful_overclaim", "overall_useful", "notes"}
        if set(row) != allowed_keys:
            missing = sorted(allowed_keys - set(row))
            extra = sorted(set(row) - allowed_keys)
            detail = f"MISSING:{missing[0]}" if missing else f"EXTRA:{extra[0]}"
            raise AuditContractError(f"REVIEW_INCOMPLETE:SCHEMA:{detail}")
        def reject_forbidden(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    if str(key) in REVIEW_FORBIDDEN_KEYS or str(key) in REVIEW_INTERNAL_KEYS:
                        raise AuditContractError(f"REVIEW_INCOMPLETE:FORBIDDEN_FIELD:{key}")
                    reject_forbidden(nested)
            elif isinstance(value, list):
                for nested in value:
                    reject_forbidden(nested)
        reject_forbidden(row)
        def text_field(name: str) -> None:
            value = row.get(name)
            if not isinstance(value, str):
                raise AuditContractError(f"REVIEW_INCOMPLETE:TYPE:{name}")
        if technical:
            required_text = ("review_id", "terminal_id", "article_idx", "taxonomy", "terminal_row_sha256", "notes")
        else:
            required_text = ("review_id", "article_title", "claim_headline", "claim_text", "explanation", "limitation", "notes")
            if paired:
                required_text = (*required_text, "pair_id")
        for field in required_text:
            text_field(field)
        if technical:
            evidence = row.get("evidence")
            if not isinstance(evidence, Mapping) or set(evidence) != {"evidence_sha256"} or not isinstance(evidence.get("evidence_sha256"), str):
                raise AuditContractError("REVIEW_INCOMPLETE:TECHNICAL_EVIDENCE_SCHEMA")
        else:
            citations = row.get("citation_labels")
            if not isinstance(citations, list) or not all(isinstance(item, str) for item in citations):
                raise AuditContractError("REVIEW_INCOMPLETE:TYPE:citation_labels")
            if any(isinstance(row.get(field), (Mapping, list)) for field in ("article_title", "claim_headline", "claim_text", "explanation", "limitation", "notes")):
                raise AuditContractError("REVIEW_INCOMPLETE:NESTED_STRUCTURE")
        review_id = str(row.get("review_id") or "")
        if not review_id or review_id in got:
            raise AuditContractError("REVIEW_INCOMPLETE:DUPLICATE_OR_MISSING_REVIEW_ID")
        scaffold = expected_by_id.get(review_id)
        if scaffold is None:
            raise AuditContractError("REVIEW_INCOMPLETE:UNIVERSE_MISMATCH")
        if technical:
            identity_fields = ("terminal_id", "article_idx", "taxonomy", "terminal_row_sha256", "evidence")
        else:
            identity_fields = ("article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels")
        if any(row.get(field) != scaffold.get(field) for field in identity_fields):
            raise AuditContractError("REVIEW_INCOMPLETE:SCAFFOLD_BINDING")
        got[review_id] = row
        if technical:
            if str(row.get("judgment") or "") not in TECHNICAL_ENUM:
                raise AuditContractError("REVIEW_INCOMPLETE:TECHNICAL_ENUM")
        else:
            for field in ANSWER_FIELDS:
                value = row.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 2:
                    raise AuditContractError(f"REVIEW_INCOMPLETE:SCORE:{field}")
            if not isinstance(row.get("harmful_overclaim"), bool) or not isinstance(row.get("overall_useful"), bool):
                raise AuditContractError("REVIEW_INCOMPLETE:ANSWER_ENUM")
    if set(got) != expected_ids:
        raise AuditContractError("REVIEW_INCOMPLETE:UNIVERSE_MISMATCH")
    return {"complete": True, "count": len(got), "rows": got}


def validate_bound_reviews(rows: Iterable[Mapping[str, Any]], expected: Iterable[Mapping[str, Any]], bindings: Mapping[str, Mapping[str, Any]], *, technical: bool = False) -> dict[str, Any]:
    """Validate completion plus the sealed packet/terminal SHA bindings."""
    rows = list(rows)
    result = validate_review_rows(rows, expected, technical=technical)
    for row in rows:
        review_id = str(row.get("review_id") or "")
        binding = bindings.get(review_id)
        if binding is None:
            raise AuditContractError("REVIEW_INCOMPLETE:STALE_BINDING")
        if technical:
            if str(row.get("terminal_row_sha256") or "") != str(binding.get("baseline_terminal_row_sha256") or ""):
                raise AuditContractError("REVIEW_INCOMPLETE:TERMINAL_SHA")
        else:
            packet = {key: row.get(key) for key in ("article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels")}
            if binding.get("packet_sha256") and _sha(packet) != str(binding["packet_sha256"]):
                raise AuditContractError("REVIEW_INCOMPLETE:PACKET_SHA")
    return result


def choose_bottleneck(universe: Iterable[Mapping[str, Any]], technical_review: Iterable[Mapping[str, Any]], answer_review: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    technical = list(technical_review)
    expected = list(universe)
    if len(technical) != len(expected) or any(str(row.get("judgment") or "") not in TECHNICAL_ENUM for row in technical):
        raise AuditContractError("REVIEW_INCOMPLETE")
    by_id = {str(row.get("review_id")): row for row in technical}
    by_terminal = {str(row.get("terminal_id")): row for row in technical}
    # review rows are bound to canonical IDs by the caller; this function
    # accepts either ``canonical_id`` or ``terminal_id`` for fixture ergonomics.
    counts: dict[str, set[str]] = {stage: set() for stage in TAXONOMY_ORDER}
    articles: dict[str, set[str]] = {stage: set() for stage in TAXONOMY_ORDER}
    for item in expected:
        review = by_id.get(str(item.get("review_id"))) or by_terminal.get(str(item.get("terminal_id")))
        if review and review.get("judgment") == "RECOVERABLE_FROM_ARTICLE_EVIDENCE" and not item.get("sentinel"):
            stage = str(item.get("taxonomy"))
            if stage in counts:
                counts[stage].add(str(item.get("target_id") or item.get("terminal_id")))
                articles[stage].add(str(item.get("article_idx")))
    eligible = [(stage, len(ids), len(articles[stage])) for stage, ids in counts.items() if len(ids) >= 3 and len(articles[stage]) >= 2]
    if eligible:
        stage, count, article_count = sorted(eligible, key=lambda row: (-row[1], TAXONOMY_RANK[row[0]]))[0]
        return {"status": "ELIGIBLE", "selected_stage": stage, "recovered_count": count, "article_count": article_count, "counts": {key: len(value) for key, value in counts.items()}}
    answers = list(answer_review)
    bad = [row for row in answers if row.get("overall_useful") is False]
    if len(bad) >= 3:
        return {"status": "ELIGIBLE", "selected_stage": "ANSWER", "recovered_count": len(bad), "article_count": len({str(row.get("article_idx") or "") for row in bad})}
    return {"status": "NO_ELIGIBLE_BOTTLENECK", "selected_stage": None, "recovered_count": 0, "article_count": 0}


def compare_baseline_candidate(stage: str, baseline: Iterable[Mapping[str, Any]], candidate: Iterable[Mapping[str, Any]], *, baseline_review: Mapping[str, Mapping[str, Any]] | None = None, candidate_review: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    b = {str(row.get("target_id") or row.get("terminal_id")): row for row in baseline if not row.get("sentinel")}
    c = {str(row.get("target_id") or row.get("terminal_id")): row for row in candidate if not row.get("sentinel")}
    if stage == "ANSWER":
        eligible = sorted(key for key, review in (baseline_review or {}).items() if review.get("overall_useful") is False and key in b)
    else:
        eligible = sorted(
            key for key, row in b.items()
            if str(row.get("taxonomy")) == stage
            and (
                baseline_review is None
                or baseline_review.get(key, {}).get("complete") is True
                or baseline_review.get(key, {}).get("judgment") in TECHNICAL_ENUM
            )
        )
    recovered: list[str] = []
    def has_blocking_evidence(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                status = str(nested.get("outcome") or nested.get("status") or nested.get("verdict") or "").upper() if isinstance(nested, Mapping) else ""
                if status == "HOLD" or status.endswith(("_UNAVAILABLE", "_FAILED", "_UNVERIFIABLE")) or status in {"UNAVAILABLE", "UNVERIFIABLE", "NO_ROUTED_TARGETS", "NO_CANDIDATES", "NO_CELL"}:
                    return True
                if key in {"raw", "resolution", "cell_cache_match", "final_answer", "comparison", "comparator", "answer", "cell"} and has_blocking_evidence(nested):
                    return True
        return False
    for target_id in eligible:
        if target_id not in c:
            continue
        candidate_row = c[target_id]
        if stage == "ANSWER":
            br = (baseline_review or {}).get(target_id, {})
            cr = (candidate_review or {}).get(target_id, {})
            sealed = cr.get("_sealed_contract_validity") or {}
            if (
                cr.get("overall_useful") is True
                and cr.get("harmful_overclaim") is False
                and all(cr.get(field, -1) >= br.get(field, 99) for field in ANSWER_FIELDS)
                and sealed.get("answer_contract_valid") is True
                and sealed.get("citation_contract_valid") is True
                and sealed.get("verdict_contract_valid") is True
                and cr.get("upstream_blocker_moved", False) is False
            ):
                recovered.append(target_id)
            continue
        if TAXONOMY_RANK.get(str(candidate_row.get("taxonomy")), 999) <= TAXONOMY_RANK.get(stage, 999):
            continue
        resolution_status, _ = _nested_status(candidate_row.get("resolution"))
        terminal_status = str(candidate_row.get("terminal_status") or candidate_row.get("status") or "").upper()
        def _non_success(status: str) -> bool:
            return bool(status) and status not in {"READY", "RESOLVED", "QUERY_READY", "L2_READY", "PROFILE_READY", "CELL_RESOLVED"} and (status == "HOLD" or status.endswith(("_UNAVAILABLE", "_FAILED", "_UNVERIFIABLE")) or status in {"UNAVAILABLE", "UNVERIFIABLE", "NO_ROUTED_TARGETS"})
        later_blocker = _non_success(resolution_status) or _non_success(terminal_status) or has_blocking_evidence(candidate_row)
        if str(candidate_row.get("taxonomy")) in {"ENVIRONMENT", stage} or later_blocker or candidate_row.get("terminal") or candidate_row.get("unsafe_ready") or candidate_row.get("harmful_overclaim"):
            continue
        recovered.append(target_id)
    denominator = len(eligible)
    return {"stage": stage, "baseline_eligible_count": denominator, "baseline_eligible_target_ids": eligible, "recovered_count": len(recovered), "recovered_target_ids": recovered, "improvement_claim": denominator >= 3 and len(recovered) * 2 >= denominator, "comparison_status": "COMPLETE" if denominator and all(key in c for key in eligible) else "INCOMPLETE"}


def build_answer_pair_scaffold(
    baseline_rows: Iterable[Mapping[str, Any]],
    candidate_rows: Iterable[Mapping[str, Any]],
    baseline_bindings: Mapping[str, Mapping[str, Any]],
    candidate_bindings: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create role-blind paired answer packets; role lives only in sealed pairs."""
    baseline_by_target = {str(value.get("canonical_id")): key for key, value in baseline_bindings.items()}
    candidate_by_target = {str(value.get("canonical_id")): key for key, value in candidate_bindings.items()}
    b_rows = {str((baseline_bindings.get(str(row.get("review_id") or "")) or {}).get("canonical_id") or row.get("target_id") or row.get("canonical_id")): row for row in baseline_rows}
    c_rows = {str((candidate_bindings.get(str(row.get("review_id") or "")) or {}).get("canonical_id") or row.get("target_id") or row.get("canonical_id")): row for row in candidate_rows}
    visible: list[dict[str, Any]] = []
    sealed: dict[str, Any] = {}
    for target_id in sorted(set(b_rows) & set(c_rows)):
        left, right = b_rows[target_id], c_rows[target_id]
        baseline_review_id = baseline_by_target.get(target_id)
        candidate_review_id = candidate_by_target.get(target_id)
        pair_seed = (target_id, _sha(left), _sha(right))
        pair_id = _opaque_id(set(sealed), pair_seed)
        packets = [
            {key: left.get(key) for key in ("article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels")},
            {key: right.get(key) for key in ("article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels")},
        ]
        for packet_index, packet in enumerate(packets):
            visible_id = _opaque_id(set(sealed), (pair_seed, packet_index))
            visible.append({"review_id": visible_id, "pair_id": pair_id, **packet, **{field: None for field in ANSWER_FIELDS}, "harmful_overclaim": None, "overall_useful": None, "notes": ""})
            pair = sealed.setdefault(pair_id, {"target_id": target_id, "baseline_review_id": baseline_review_id, "candidate_review_id": candidate_review_id, "baseline_answer_sha256": (baseline_bindings.get(baseline_review_id or "") or {}).get("answer_sha256"), "candidate_answer_sha256": (candidate_bindings.get(candidate_review_id or "") or {}).get("answer_sha256"), "packet_shas": [], "visible_review_ids": []})
            pair["packet_shas"].append(_sha(packet))
            pair["visible_review_ids"].append(visible_id)
    for pair in sealed.values():
        pair["baseline_visible_review_id"], pair["candidate_visible_review_id"] = pair["visible_review_ids"]
    return visible, sealed


def audit_run(articles: list[dict[str, Any]], run_root: Path, *, run_id: str = "baseline") -> dict[str, Any]:
    ledger_path = run_root / "stage_ledger.jsonl"
    answers_path = run_root / "final_answers.jsonl"
    if not ledger_path.is_file():
        raise AuditContractError("AUDIT_INPUT_INVALID:stage_ledger.jsonl")
    universe, partition = build_terminal_partition(articles, _read_jsonl(ledger_path), run_id=run_id)
    answers = _read_jsonl(answers_path) if answers_path.is_file() else []
    scaffolds = build_review_scaffolds(universe, answers, articles, run_id=run_id)
    return {"contract": "operational-bottleneck-audit-v1", "partition": partition, "terminal_universe": universe, **scaffolds, "selection": {"status": "REVIEW_PENDING"}, "run_id": run_id}


def _load_review_input(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read one mixed review file or a directory containing both universes."""
    if path.is_dir():
        technical_path = path / "technical_review_working.jsonl"
        answer_path = path / "answer_review.jsonl"
        technical = _read_jsonl(technical_path) if technical_path.is_file() else []
        answers = _read_jsonl(answer_path) if answer_path.is_file() else []
        return technical, answers
    if path.suffix.lower() == ".json":
        payload = _read_json(path)
        if isinstance(payload, Mapping):
            return list(payload.get("technical_review") or payload.get("technical") or []), list(payload.get("answer_review") or payload.get("answers") or [])
    rows = _read_jsonl(path)
    technical = [row for row in rows if "judgment" in row or row.get("review_type") == "technical"]
    answers = [row for row in rows if row not in technical]
    return technical, answers


def _load_pair_input(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        pair_path = path / "answer_pair_review.jsonl"
        return _read_jsonl(pair_path) if pair_path.is_file() else []
    return []


def _load_pair_bindings(path: Path) -> dict[str, Any]:
    if path.is_dir() and (path / "answer_pair_bindings.json").is_file():
        value = _read_json(path / "answer_pair_bindings.json")
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _pair_reviews_by_target(rows: Iterable[Mapping[str, Any]], sealed: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id = {str(row.get("review_id") or ""): dict(row) for row in rows}
    baseline: dict[str, dict[str, Any]] = {}
    candidate: dict[str, dict[str, Any]] = {}
    for pair in sealed.values():
        target = str(pair.get("target_id") or "")
        b = by_id.get(str(pair.get("baseline_visible_review_id") or ""))
        c = by_id.get(str(pair.get("candidate_visible_review_id") or ""))
        if target and b is not None and c is not None:
            baseline[target], candidate[target] = b, c
    return baseline, candidate


def validate_pair_reviews(rows: Iterable[Mapping[str, Any]], expected: Iterable[Mapping[str, Any]], sealed: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    result = validate_review_rows(rows, expected, technical=False, paired=True)
    by_visible = {str(row.get("review_id") or ""): row for row in rows}
    visible_binding: dict[str, tuple[Mapping[str, Any], int]] = {}
    for pair_id, binding in sealed.items():
        for index, visible_id in enumerate(binding.get("visible_review_ids") or []):
            visible_binding[str(visible_id)] = (binding, index)
    if set(by_visible) != set(visible_binding):
        raise AuditContractError("REVIEW_INCOMPLETE:PAIR_UNIVERSE_MISMATCH")
    for visible_id, row in by_visible.items():
        binding, index = visible_binding[visible_id]
        packet = {key: row.get(key) for key in ("article_title", "claim_headline", "claim_text", "explanation", "limitation", "citation_labels")}
        packet_shas = binding.get("packet_shas") or []
        if index >= len(packet_shas) or _sha(packet) != str(packet_shas[index]):
            raise AuditContractError("REVIEW_INCOMPLETE:PAIR_PACKET_SHA")
    return result


def validate_persisted_pair_bindings(persisted: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    if not persisted:
        raise AuditContractError("REVIEW_INCOMPLETE:PAIR_BINDINGS_MISSING")
    if set(persisted) != set(current):
        raise AuditContractError("REVIEW_INCOMPLETE:PAIR_BINDINGS_STALE")
    for pair_id in current:
        old, fresh = persisted[pair_id], current[pair_id]
        for field in ("target_id", "baseline_review_id", "candidate_review_id", "baseline_answer_sha256", "candidate_answer_sha256", "packet_shas", "visible_review_ids"):
            if old.get(field) != fresh.get(field):
                raise AuditContractError(f"REVIEW_INCOMPLETE:PAIR_BINDING_SHA:{field}")


def _review_by_target(rows: Iterable[Mapping[str, Any]], bindings: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        binding = bindings.get(str(row.get("review_id") or ""))
        if binding and binding.get("canonical_id"):
            mapped = dict(row)
            mapped["_sealed_contract_validity"] = dict(binding.get("sealed_contract_validity") or {})
            result[str(binding["canonical_id"])] = mapped
    return result


def render_markdown(report: Mapping[str, Any]) -> str:
    partition = report.get("partition") or {}
    selection = report.get("selection") or {}
    lines = ["# Operational Bottleneck Audit v1", "", f"- articles: {partition.get('articles', 0)}", f"- terminal rows: {partition.get('terminal_rows', 0)}", f"- sentinels: {partition.get('sentinels', 0)}", f"- selection: {selection.get('selected_stage') or selection.get('status') or 'PENDING'}", ""]
    return "\n".join(lines)


def write_audit_outputs(report: Mapping[str, Any], output: str | Path, *, secret_values: Iterable[str] = ()) -> dict[str, Any]:
    root = Path(output)
    if root.exists():
        raise FileExistsError(f"refusing to overwrite audit output: {root}")
    root.mkdir(parents=True)
    def atomic(name: str, data: str) -> None:
        path = root / name
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(data, encoding="utf-8")
        temporary.replace(path)
    atomic("bottleneck_audit.json", json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n")
    atomic("bottleneck_audit.md", render_markdown(report))
    atomic("answer_review.jsonl", "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in report.get("answer_review", [])))
    atomic("answer_pair_review.jsonl", "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in report.get("answer_pair_scaffold", [])))
    atomic("answer_pair_bindings.json", json.dumps(report.get("answer_pair_bindings", {}), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    atomic("technical_review_working.jsonl", "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in report.get("technical_review", [])))
    from src.develop.audit_secret_scan_v1 import FinalTreeSecretScanner
    return FinalTreeSecretScanner(root, secrets=secret_values).finalize()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--compare-run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    articles = _read_jsonl(args.articles)
    report = audit_run(articles, args.run, run_id=args.run.name)
    pair_input = _load_pair_input(args.review) if args.review else []
    if args.review:
        technical_rows, answer_rows = _load_review_input(args.review)
        if not technical_rows or (not answer_rows and not pair_input):
            raise AuditContractError("REVIEW_INCOMPLETE:BOTH_UNIVERSES_REQUIRED")
        validate_bound_reviews(technical_rows, report["technical_review"], report["review_bindings"], technical=True)
        if answer_rows and not pair_input:
            validate_bound_reviews(answer_rows, report["answer_review"], report["review_bindings"], technical=False)
        report["technical_review"] = technical_rows
        report["answer_review"] = answer_rows
        report["selection"] = choose_bottleneck(report["terminal_universe"], report["technical_review"], report["answer_review"])
    if args.compare_run:
        candidate = audit_run(articles, args.compare_run, run_id=args.compare_run.name)
        candidate_review_path = args.compare_run
        if not args.review:
            raise AuditContractError("REVIEW_INCOMPLETE:COMPARE_REQUIRES_REVIEW")
        candidate_technical, candidate_answers = _load_review_input(candidate_review_path)
        if not candidate_technical or not candidate_answers:
            raise AuditContractError("REVIEW_INCOMPLETE:CANDIDATE_BOTH_UNIVERSES_REQUIRED")
        validate_bound_reviews(candidate_technical, candidate["technical_review"], candidate["review_bindings"], technical=True)
        validate_bound_reviews(candidate_answers, candidate["answer_review"], candidate["review_bindings"], technical=False)
        selected = str(report.get("selection", {}).get("selected_stage") or "")
        if selected not in TAXONOMY_ORDER:
            raise AuditContractError("NO_ELIGIBLE_BOTTLENECK")
        baseline_review = _review_by_target(report["answer_review"] if selected == "ANSWER" else report["technical_review"], report["review_bindings"])
        candidate_review = _review_by_target(candidate_answers if selected == "ANSWER" else candidate_technical, candidate["review_bindings"])
        if selected == "ANSWER":
            visible, sealed = build_answer_pair_scaffold(report["answer_review"], candidate_answers, report["review_bindings"], candidate["review_bindings"])
            report["answer_pair_scaffold"] = visible
            report["answer_pair_bindings"] = sealed
            if not pair_input:
                report["comparison"] = {"stage": "ANSWER", "comparison_status": "PAIRED_REVIEW_PENDING", "baseline_eligible_count": 0, "recovered_count": 0}
            else:
                persisted_bindings = _load_pair_bindings(args.review)
                validate_persisted_pair_bindings(persisted_bindings, sealed)
                validate_pair_reviews(pair_input, visible, sealed)
                baseline_review, candidate_review = _pair_reviews_by_target(pair_input, sealed)
                for target, mapped in candidate_review.items():
                    binding = next((value for value in candidate["review_bindings"].values() if value.get("canonical_id") == target), {})
                    mapped["_sealed_contract_validity"] = dict(binding.get("sealed_contract_validity") or {})
                report["comparison"] = compare_baseline_candidate(selected, report["terminal_universe"], candidate["terminal_universe"], baseline_review=baseline_review, candidate_review=candidate_review)
        else:
            report["comparison"] = compare_baseline_candidate(selected, report["terminal_universe"], candidate["terminal_universe"], baseline_review=baseline_review, candidate_review=candidate_review)
    allowed_secret_names = ("KOSIS_API_KEY", "NCP_CLOVASTUDIO_API_KEY", "NCP_API_KEY")
    completion = write_audit_outputs(report, args.output, secret_values=[os.environ[name] for name in allowed_secret_names if os.environ.get(name)])
    # The receipt is a sibling of the scanned tree.  Return its actual hash in
    # the outer command response; never rewrite the scanned report to embed it.
    print(json.dumps({"status": "COMPLETE", "output": str(args.output), "completion": completion}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ANSWER_FIELDS", "AuditContractError", "BLIND_FIELDS", "RAW_STATUS_REASON_MAP", "TAXONOMY_ORDER", "audit_run", "build_answer_pair_scaffold", "build_review_scaffolds", "build_terminal_partition", "choose_bottleneck", "classify_taxonomy", "compare_baseline_candidate", "main", "normalize_stage_row", "render_markdown", "validate_bound_reviews", "validate_pair_reviews", "validate_persisted_pair_bindings", "validate_review_rows", "write_audit_outputs"]
