"""Operational v2 orchestration with strict preflight and no silent fallback.

The replay vertical slice is executable today.  Live execution is enabled only
after the v6 dense return, pinned Korean reranker service, and required API
credentials all pass preflight.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date as calendar_date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
import uuid
from typing import Any, Callable, Mapping, Sequence
from urllib import request

from backend.errors import BackendError
from src.news_verification.runtime.bge_reranker_v2_service import MODEL_REVISION, SERVICE_CONTRACT
from src.news_verification.runtime.bge_m3_ko_query_encoder_service import (
    HttpQueryEncoderClient,
    MODEL_REVISION as ENCODER_MODEL_REVISION,
    SERVICE_CONTRACT as ENCODER_SERVICE_CONTRACT,
)
from src.news_verification.runtime.canonical_quantity import QuantityNormalizationError, compare_canonical, normalize_quantity
from src.news_verification.runtime.operational_answer_v2 import Hcx007AnswerClient, build_evidence_packet, generate_guarded_answer
from src.news_verification.runtime.same_series_evidence_v1 import (
    FEATURE_GATE_ENV,
    SameSeriesEvidenceError,
    indicator_family_key,
    select_primary_target,
    synthesize_same_series_evidence,
)
from src.develop.annual_requery_shadow_v1 import AnnualRequeryError, verify_annual_requery
from src.news_verification.runtime.operational_gpu_receipts_v2 import GpuReceiptError, validate_gpu_receipts
from src.news_verification.runtime.operational_live_adapters_v2 import (
    CountingAdapter,
    CountingAnswerer,
    CountingEncoder,
    CountingReranker,
    FailClosedCellFetcher,
    LiveAdapterError,
    OperationalProfileProvider,
    V6CatalogPassageStore,
    load_live_articles,
    safe_adapter_failure,
    sha256_file,
    write_live_outputs,
)
from src.news_verification.runtime.audit_budget_v1 import BudgetedCallable, HttpAttemptBudgetLedger
from src.news_verification.runtime.l1_value_candidates import build_span_candidates, sentence_offset_map
from src.news_verification.runtime.l3_role_assignment import attach_indicator_evidence_monthly_v2h
from src.news_verification.runtime.adapters.shadow_compat import failure_recovery_api, user_intent_api

def corrective_claim_query(*args: Any, **kwargs: Any) -> Any:
    return failure_recovery_api()[0](*args, **kwargs)


def merge_candidate_rounds(*args: Any, **kwargs: Any) -> Any:
    return failure_recovery_api()[1](*args, **kwargs)


def plan_failure_recovery(*args: Any, **kwargs: Any) -> Any:
    return failure_recovery_api()[2](*args, **kwargs)


def route_user_intent(*args: Any, **kwargs: Any) -> Any:
    return user_intent_api()(*args, **kwargs)
from src.news_verification.runtime.operational_article_acquisition_v2 import (
    ArticleAcquisitionError,
    acquire_article_url,
)
from src.news_verification.runtime.operational_retrieval_v2 import (
    build_candidate_passages,
    rerank_top50,
    retrieve_parallel,
)
from src.news_verification.runtime.r4c1_claim_core_v2 import (
    ClaimAtom,
    ClaimCore,
    MonthlyClaimProvenanceError,
    build_claim_core_monthly_v2h,
    build_claim_core_v2,
    canonical_bytes,
)
from src.news_verification.runtime.l4_field_normalization import (
    NO_BASIS,
    YOY,
    comparison_basis,
    comparison_basis_monthly_v2h,
)
from src.news_verification.contracts.query_plan_inventory import validate_query_plan_inventory
from src.news_verification.runtime.services.terminal_partition import build_terminal_partition
from src.news_verification.runtime.r4c1_projection_v2 import (
    CandidateAssignment,
    CandidateProjection,
    TargetResolution,
    membership_receipt_sha256_monthly_v2h,
    placeholder_projection_monthly_v2h,
    project_candidate_monthly_v2j,
    project_candidate_monthly_v2h,
    project_candidate_v2,
    _query_plan,
    validate_target_monthly_v2h,
    validate_target_v2,
)
from src.news_verification.runtime.run_pipeline_replay_v1 import run_replay, write_replay
from src.news_verification.runtime.adapters.legacy_stages import l2_runner

def run_hcx_l2(*args: Any, **kwargs: Any) -> Any:
    return l2_runner()(*args, **kwargs)
from src.news_verification.runtime.run_layer_stack import run_stack
from src.news_verification.runtime.v6_search_channels import OfficialKosisSearchChannel, V6Bm25Channel, V6DenseChannel
from src.news_verification.runtime.release_bound_live_adapters_v1 import (
    RRF_ONLY_ORDERING,
    RRF_ORDER_RERANKER_DISABLED,
    build_release_bound_runtime,
    release_runtime_enabled,
)


def _snapshot_target_call_counts(
    search_channels: Mapping[str, Callable[..., Any]],
    profile_provider: Any,
) -> dict[str, Any]:
    """Capture only integer counters at the start of one routed target."""
    channel_calls: dict[str, int | None] = {}
    audit_cursors: dict[str, int | None] = {}
    for name, channel in search_channels.items():
        value = getattr(channel, "calls", None)
        channel_calls[str(name)] = value if isinstance(value, int) and not isinstance(value, bool) else None
        cursor_method = getattr(channel, "audit_cursor", None)
        cursor = cursor_method() if callable(cursor_method) else None
        audit_cursors[str(name)] = cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0 else None
    metadata_calls = getattr(profile_provider, "metadata_api_calls", None)
    metadata_lookups = getattr(profile_provider, "lookups", None)
    return {
        "channel_calls": channel_calls,
        "audit_cursors": audit_cursors,
        "metadata_api_calls": metadata_calls
        if isinstance(metadata_calls, int) and not isinstance(metadata_calls, bool)
        else None,
        "metadata_lookups": metadata_lookups
        if isinstance(metadata_lookups, int) and not isinstance(metadata_lookups, bool)
        else None,
    }


def _bounded_channel_audit_events(raw_events: Any) -> list[dict[str, Any]]:
    """Validate and order every release dense audit event without dropping one."""
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
        raise OperationalPipelineError("DENSE_AUDIT_EVENTS_INVALID")
    events: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            raise OperationalPipelineError("DENSE_AUDIT_EVENT_INVALID")
        query_sha256 = raw.get("query_sha256")
        status = raw.get("boundary_status")
        if not isinstance(query_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", query_sha256):
            raise OperationalPipelineError("DENSE_AUDIT_EVENT_INVALID")
        if status not in {"CLOSED", "DROPPED_UNCLOSED_CUTOFF_TIE"}:
            raise OperationalPipelineError("DENSE_AUDIT_EVENT_INVALID")
        event: dict[str, Any] = {"query_sha256": query_sha256, "boundary_status": status}
        for key in ("cutoff_score", "observed_tied_count", "requested_window"):
            value = raw.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise OperationalPipelineError("DENSE_AUDIT_EVENT_INVALID")
            event[key] = value
        expansions = raw.get("expansions")
        if not isinstance(expansions, Sequence) or isinstance(expansions, (str, bytes)) or len(expansions) > 16:
            raise OperationalPipelineError("DENSE_AUDIT_EVENT_INVALID")
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in expansions):
            raise OperationalPipelineError("DENSE_AUDIT_EVENT_INVALID")
        event["expansions"] = list(expansions)
        events.append(event)
    return sorted(events, key=lambda event: json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _target_call_deltas(
    snapshot: Mapping[str, Any],
    search_channels: Mapping[str, Callable[..., Any]],
    profile_provider: Any,
) -> dict[str, Any]:
    """Return bounded non-negative per-target counter deltas."""
    before_channels = snapshot.get("channel_calls")
    if not isinstance(before_channels, Mapping):
        before_channels = {}
    channel_calls: dict[str, int] = {}
    for name, channel in search_channels.items():
        before = before_channels.get(str(name))
        after = getattr(channel, "calls", None)
        if (
            isinstance(before, int) and not isinstance(before, bool)
            and isinstance(after, int) and not isinstance(after, bool)
        ):
            channel_calls[str(name)] = max(0, after - before)
    channel_audits: dict[str, list[dict[str, Any]]] = {}
    before_cursors = snapshot.get("audit_cursors")
    if not isinstance(before_cursors, Mapping):
        before_cursors = {}
    for name, call_delta in channel_calls.items():
        if call_delta <= 0:
            continue
        channel = search_channels[name]
        cursor = before_cursors.get(name)
        events_method = getattr(channel, "audits_since", None)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or not callable(events_method):
            continue
        events = _bounded_channel_audit_events(events_method(cursor))
        if events:
            channel_audits[name] = events
    before_metadata = snapshot.get("metadata_api_calls")
    after_metadata = getattr(profile_provider, "metadata_api_calls", None)
    metadata_calls = (
        max(0, after_metadata - before_metadata)
        if isinstance(before_metadata, int) and not isinstance(before_metadata, bool)
        and isinstance(after_metadata, int) and not isinstance(after_metadata, bool)
        else None
    )
    before_lookups = snapshot.get("metadata_lookups")
    after_lookups = getattr(profile_provider, "lookups", None)
    metadata_lookups = (
        max(0, after_lookups - before_lookups)
        if isinstance(before_lookups, int) and not isinstance(before_lookups, bool)
        and isinstance(after_lookups, int) and not isinstance(after_lookups, bool)
        else None
    )
    return {
        "retrieval": {
            "calls": sum(channel_calls.values()),
            "channel_calls": channel_calls,
            "channel_audits": channel_audits,
        },
        "metadata_api_calls": metadata_calls,
        "metadata_lookups": metadata_lookups,
    }
from src.news_verification.runtime.bge_reranker_v2_service import HttpRerankerClient
from src.news_verification.runtime.adapters.shadow_compat import role_aware_dimension_api

def extract_source_terms(*args: Any, **kwargs: Any) -> Any:
    return role_aware_dimension_api()[0](*args, **kwargs)


def infer_profile_units(*args: Any, **kwargs: Any) -> Any:
    return role_aware_dimension_api()[1](*args, **kwargs)


def build_role_aware_reranker_query(*args: Any, **kwargs: Any) -> Any:
    return role_aware_dimension_api()[2](*args, **kwargs)


def select_query_target(*args: Any, **kwargs: Any) -> Any:
    return role_aware_dimension_api()[3](*args, **kwargs)


def source_sentence(*args: Any, **kwargs: Any) -> Any:
    return role_aware_dimension_api()[4](*args, **kwargs)


def _preserve_role_aware_sentence_inventory(
    row: dict[str, Any], article_text: str, sentence: str,
) -> tuple[Any, str]:
    """Update the current sentence without dropping inherited source context."""
    existing = row.get("sentences")
    sentences = dict(existing) if isinstance(existing, Mapping) else {}
    current_id = row.get("article_sentence_id", row.get("sentence_id"))
    if current_id is not None:
        sentences[current_id] = sentence

    source_id = row.get("source_region_sentence_id")
    source_id_text = str(source_id).strip() if source_id is not None else ""
    try:
        int(source_id)
    except (TypeError, ValueError):
        source_id_valid = False
    else:
        source_id_valid = bool(source_id_text) and source_id_text.casefold() != "none"
    source_text = ""
    if source_id_valid:
        source_text = str(source_sentence(article_text, source_id) or "")
        sentences[source_id] = source_text
    row["sentences"] = sentences
    return source_id, source_text


CONTRACT_VERSION = "kosis-operational-article-verification-v2"
RELEASE_BOUND_DOMINANCE_RULE_ID = "release-bound-evidence-specificity-dominance-v1"
RELEASE_BOUND_DOMINANCE_RULE_VERSION = 1


@dataclass(frozen=True)
class RrfOnlyCandidate:
    """RRF order without fabricated model scores when reranking is disabled."""

    table_key: str
    rank: int
    rrf_score: float
    ranking_mode: str = RRF_ONLY_ORDERING


def _rrf_only_order(candidates: Sequence[Any]) -> tuple[RrfOnlyCandidate, ...]:
    ordered = sorted(
        candidates,
        key=lambda candidate: (-float(candidate.rrf_score), int(candidate.best_rank), str(candidate.table_key)),
    )
    return tuple(
        RrfOnlyCandidate(str(candidate.table_key), rank, float(candidate.rrf_score))
        for rank, candidate in enumerate(ordered[:50], 1)
    )


def _evidence_first_enabled(value: bool | None = None) -> bool:
    if value is not None:
        return bool(value)
    return os.getenv(FEATURE_GATE_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_CONFIG = Path("configs/pipeline_operational_v2.json")
_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9])\d[\d,.]*(?:조|억|만|천)?(?:원|명|가구|건|회|달러|%p?|포인트)?")


class OperationalPipelineError(RuntimeError):
    pass


_TRACE_ERROR_KINDS = {
    "MISSING_SENTENCES",
    "UNRESOLVED_SPANS",
    "CALL_FAILED",
    "L2_RESPONSE_INVALID",
}
_TRACE_SPAN_FIELDS = {"indicator_scope", "source_region"}
_TRACE_SPAN_ERROR_CODES = {
    "EMPTY",
    "NOT_FOUND",
    "AMBIGUOUS",
    "OCCURRENCE_INDEX_INVALID",
    "UNKNOWN",
}


def _trace_safe_error(error: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(error, Mapping):
        raise OperationalPipelineError("L2_RESPONSE_INVALID")
    raw_kind = error.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) else ""
    if kind not in _TRACE_ERROR_KINDS:
        raise OperationalPipelineError("L2_RESPONSE_INVALID")
    result: dict[str, Any] = {
        "article_idx": str(error.get("article_idx") or ""),
        "kind": kind,
    }
    if kind == "MISSING_SENTENCES":
        result["sentence_ids"] = [int(value) for value in (error.get("sentence_ids") or []) if isinstance(value, int)]
    elif kind == "UNRESOLVED_SPANS":
        count = error.get("count")
        details = error.get("details")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 64
            or not isinstance(details, list)
            or len(details) != count
        ):
            raise OperationalPipelineError("L2_RESPONSE_INVALID")
        safe_details: list[dict[str, Any]] = []
        for detail in details:
            if not isinstance(detail, dict) or set(detail) != {
                "sentence_id",
                "field",
                "source_span_text",
                "span_error_code",
            }:
                raise OperationalPipelineError("L2_RESPONSE_INVALID")
            sentence_id = detail["sentence_id"]
            source_span_text = detail["source_span_text"]
            field = detail["field"]
            span_error_code = detail["span_error_code"]
            if (
                isinstance(sentence_id, bool)
                or not isinstance(sentence_id, int)
                or not isinstance(field, str)
                or field not in _TRACE_SPAN_FIELDS
                or not isinstance(source_span_text, str)
                or len(source_span_text) > 512
                or not isinstance(span_error_code, str)
                or span_error_code not in _TRACE_SPAN_ERROR_CODES
            ):
                raise OperationalPipelineError("L2_RESPONSE_INVALID")
            safe_details.append({
                "sentence_id": sentence_id,
                "field": field,
                "source_span_text": source_span_text,
                "span_error_code": span_error_code,
            })
        result["count"] = count
        result["details"] = safe_details
    return result


def _strip_trace_secrets(value: Any) -> Any:
    """Recursively remove diagnostic-only keys from an opt-in trace object."""
    banned = {"detail", "span_error", "exception", "message", "traceback", "headers", "body"}
    if isinstance(value, Mapping):
        return {str(key): _strip_trace_secrets(item) for key, item in value.items() if str(key) not in banned}
    if isinstance(value, list):
        return [_strip_trace_secrets(item) for item in value]
    return value


def project_trace_operational_l2(materialized: Mapping[str, Any]) -> dict[str, Any]:
    """Project legacy L2 materialization to the finite trace contract."""
    result = dict(materialized)
    manifest = dict(result.get("manifest") or {})
    errors = [_trace_safe_error(error) for error in (manifest.get("errors") or [])]
    rejections: list[dict[str, Any]] = []
    for rejection in manifest.get("operational_scope_rejections") or []:
        item = _strip_trace_secrets(rejection)
        if str(item.get("reason") or "") != "INDICATOR_SCOPE_UNRESOLVED":
            raise OperationalPipelineError("L2_RESPONSE_INVALID")
        rejections.append(item)
    manifest["errors"] = errors
    manifest["operational_scope_rejections"] = rejections
    manifest.pop("operational_scope_rejection_count", None)
    manifest = _strip_trace_secrets(manifest)
    result["manifest"] = manifest
    result["results"] = _strip_trace_secrets(result.get("results") or [])
    result["external_model_calls"] = int(result.get("external_model_calls") or 0)
    return result


def materialize_operational_l2(
    articles: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
    raw_manifest: Mapping[str, Any],
    *,
    external_model_calls: int,
    sentence_span_iterator: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Materialize L2 while locally disposing of nonnumeric missing rows.

    HCX's missing-sentence diagnostic is only a location hint.  The source
    sentence inventory and the frozen L1 ``value_unit`` extractor decide
    whether a missing row can be filtered safely.  No prediction is created
    here: only rows returned by HCX are passed downstream.
    """
    errors = list(raw_manifest.get("errors") or [])
    failed = {
        str(error.get("article_idx") or "")
        for error in errors
        if str(error.get("kind") or "") not in {"UNRESOLVED_SPANS", "MISSING_SENTENCES"}
    }
    source_inventory: dict[str, dict[int, dict[str, Any]]] = {}
    value_candidates: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for article in articles:
        article_id = str(article.get("article_idx") or "")
        body = str(article.get("article_text") or "")
        sentence_rows = (
            sentence_offset_map(body, sentence_span_iterator=sentence_span_iterator)
            if sentence_span_iterator is not None else sentence_offset_map(body)
        )
        source_inventory[article_id] = {
            int(row["sentence_id"]): dict(row) for row in sentence_rows
        }
        candidates = (
            build_span_candidates(body, sentence_span_iterator=sentence_span_iterator)
            if sentence_span_iterator is not None else build_span_candidates(body)
        )
        grouped: dict[int, list[dict[str, Any]]] = {}
        for candidate in candidates:
            if candidate.get("kind") == "value_unit":
                grouped.setdefault(int(candidate["sentence_id"]), []).append(dict(candidate))
        value_candidates[article_id] = grouped

    reported_missing: dict[str, set[int]] = {}
    for error in errors:
        if str(error.get("kind") or "") != "MISSING_SENTENCES":
            continue
        article_id = str(error.get("article_idx") or "")
        reported_missing.setdefault(article_id, set()).update(
            int(value) for value in (error.get("sentence_ids") or []) if isinstance(value, int)
        )
    predicted_sentence_ids: dict[str, set[int]] = {}
    prediction_sentence_counts: dict[str, Counter[int]] = {}
    for prediction in predictions:
        article_id = str(prediction.get("article_idx") or "")
        sentence_id = prediction.get("sentence_id")
        if isinstance(sentence_id, int):
            predicted_sentence_ids.setdefault(article_id, set()).add(sentence_id)
            prediction_sentence_counts.setdefault(article_id, Counter())[sentence_id] += 1
    duplicate_prediction_dispositions = [
        {
            "article_idx": article_id,
            "sentence_id": sentence_id,
            "occurrence_count": count,
            "decision": "DUPLICATE_PREDICTION_SENTENCE_ID",
            "final_attribution": "ARTICLE_FAIL_CLOSED",
        }
        for article_id, counts in prediction_sentence_counts.items()
        for sentence_id, count in sorted(counts.items())
        if count > 1
    ]
    failed.update(str(row["article_idx"]) for row in duplicate_prediction_dispositions)
    inventory_mismatch: set[str] = set()
    dispositions: list[dict[str, Any]] = []
    for article_id, inventory in source_inventory.items():
        source_ids = set(inventory)
        predicted_ids = predicted_sentence_ids.get(article_id, set())
        actual = source_ids - predicted_ids
        reported = reported_missing.get(article_id, set())
        if reported != actual or (predicted_ids - source_ids):
            inventory_mismatch.add(article_id)
        all_missing_ids = sorted(reported | actual | (predicted_ids - source_ids))
        for sentence_id in all_missing_ids:
            sentence = inventory.get(sentence_id)
            source_text = str(sentence.get("text") or "") if sentence else ""
            candidates = value_candidates.get(article_id, {}).get(sentence_id, [])
            mismatch_row = (
                sentence is None
                or article_id in inventory_mismatch
                and (reported != actual or sentence_id in (predicted_ids - source_ids))
            )
            if mismatch_row:
                decision = "MISSING_SENTENCE_INVENTORY_MISMATCH"
                final_attribution = "ARTICLE_FAIL_CLOSED"
            elif candidates:
                decision = "MISSING_NUMERIC_CANDIDATE"
                final_attribution = "ARTICLE_FAIL_CLOSED"
                failed.add(article_id)
            else:
                decision = "FILTERED_NON_NUMERIC"
                final_attribution = "SENTENCE_FILTERED"
            dispositions.append({
                "article_idx": article_id,
                "sentence_id": sentence_id,
                "source_text": source_text,
                "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "decision": decision,
                "rule_id": "L1_VALUE_CANDIDATE_ABSENCE_V1",
                "evidence": {
                    "l1_value_candidate_count": len(candidates),
                    "l1_value_candidates": [
                        {key: candidate.get(key) for key in ("span_id", "text", "kind")}
                        for candidate in candidates
                    ],
                },
                "final_attribution": final_attribution,
            })
        if article_id in inventory_mismatch:
            failed.add(article_id)

    unresolved_reported = {
        str(error.get("article_idx") or ""): int(error.get("count") or 0)
        for error in errors
        if str(error.get("kind") or "") == "UNRESOLVED_SPANS"
    }
    sanitized_predictions: list[dict[str, Any]] = []
    scope_rejections: list[dict[str, Any]] = []
    unresolved_observed: Counter[str] = Counter()
    for prediction in predictions:
        article_id = str(prediction.get("article_idx") or "")
        sanitized = dict(prediction)
        kept_scopes = []
        for scope in prediction.get("indicator_scopes") or []:
            if str(scope.get("span_status") or "") == "UNRESOLVED":
                unresolved_observed[article_id] += 1
                scope_rejections.append({
                    "article_idx": article_id,
                    "sentence_id": prediction.get("sentence_id"),
                    "indicator_label": scope.get("indicator_label"),
                    "source_span_text": scope.get("source_span_text"),
                    "reason": "INDICATOR_SCOPE_UNRESOLVED",
                })
            else:
                kept_scopes.append(scope)
        # Do not add a field to a raw prediction that did not contain one.
        # Filter only the existing scope list so a nonnumeric missing sentence
        # cannot cause unrelated prediction bytes to change.
        if "indicator_scopes" in prediction:
            sanitized["indicator_scopes"] = kept_scopes
        region = prediction.get("source_region") or {}
        if str(region.get("span_status") or "") == "UNRESOLVED":
            unresolved_observed[article_id] += 1
            failed.add(article_id)
        sanitized_predictions.append(sanitized)
    for article_id, reported in unresolved_reported.items():
        if unresolved_observed[article_id] != reported:
            failed.add(article_id)
    article_ids = [str(article.get("article_idx") or "") for article in articles]
    source_sentence_rows = sum(len(source_inventory.get(article_id, {})) for article_id in article_ids)
    model_sentence_rows = sum(len(predicted_sentence_ids.get(article_id, set())) for article_id in article_ids)
    claim_relevant_source_rows = sum(
        1 for article_id in article_ids for sentence_id in value_candidates.get(article_id, {})
    )
    claim_relevant_model_rows = sum(
        1 for article_id in article_ids
        for sentence_id in predicted_sentence_ids.get(article_id, set())
        if sentence_id in value_candidates.get(article_id, {})
    )
    call_failed_ids = {
        str(error.get("article_idx") or "") for error in errors
        if str(error.get("kind") or "") == "CALL_FAILED"
    }
    call_failed_ids.update(
        str(run.get("article_idx") or "")
        for run in (raw_manifest.get("article_runs") or [])
        if str(run.get("status") or "") == "CALL_FAILED"
    )
    call_failed_ids.intersection_update(article_ids)
    filtered_count = sum(item["decision"] == "FILTERED_NON_NUMERIC" for item in dispositions)
    numeric_missing_count = sum(item["decision"] == "MISSING_NUMERIC_CANDIDATE" for item in dispositions)
    manifest = {
        **dict(raw_manifest),
        "operational_scope_rejections": scope_rejections,
        "operational_scope_rejection_count": len(scope_rejections),
        "unresolved_span_policy": "INDICATOR_SCOPE_TARGET_FAIL_CLOSED;SOURCE_REGION_ARTICLE_FAIL_CLOSED",
        "missing_sentence_dispositions": dispositions,
        "prediction_duplicate_dispositions": duplicate_prediction_dispositions,
        "l2_disposition_metrics": {
            "transport_success_articles": {"numerator": len(article_ids) - len(call_failed_ids), "denominator": len(article_ids)},
            "model_sentence_rows": {"numerator": model_sentence_rows, "denominator": source_sentence_rows},
            "claim_relevant_model_rows": {"numerator": claim_relevant_model_rows, "denominator": claim_relevant_source_rows},
            "l2_ready_articles": {"numerator": 0, "denominator": len(article_ids)},
            "filtered_non_numeric_sentences": filtered_count,
            "missing_numeric_fail_closed_sentences": numeric_missing_count,
            "unresolved_indicator_scopes": sum(
                1 for rejection in scope_rejections
            ),
            "unresolved_source_regions": sum(
                1 for prediction in predictions
                if str((prediction.get("source_region") or {}).get("span_status") or "") == "UNRESOLVED"
            ),
        },
    }
    by_article: dict[str, list[dict[str, Any]]] = {}
    for prediction in sanitized_predictions:
        by_article.setdefault(str(prediction.get("article_idx") or ""), []).append(prediction)
    results = []
    for article in articles:
        article_id = str(article.get("article_idx") or "")
        article_predictions = by_article.get(article_id, [])
        filtered_sentence_ids = {
            int(row["sentence_id"])
            for row in dispositions
            if row["article_idx"] == article_id and row["decision"] == "FILTERED_NON_NUMERIC"
        }
        all_source_sentences_filtered = bool(source_inventory.get(article_id)) and (
            filtered_sentence_ids == set(source_inventory[article_id])
        )
        if article_id in failed or (
            not article_predictions and not all_source_sentences_filtered
        ):
            results.append({"article_idx": article_id, "status": "L2_UNAVAILABLE", "predictions": []})
        else:
            results.append({"article_idx": article_id, "status": "L2_READY", "predictions": article_predictions})
    manifest["l2_disposition_metrics"]["l2_ready_articles"]["numerator"] = sum(
        row.get("status") == "L2_READY" for row in results
    )
    return {"results": results, "manifest": manifest, "external_model_calls": external_model_calls}


def _bounded_exception_code(exc: BaseException, fallback: str, allowed: set[str]) -> str:
    """Preserve only an explicitly registered contract code, never a message."""
    candidate = exc.args[0] if len(exc.args) == 1 and isinstance(exc.args[0], str) else ""
    return candidate if candidate in allowed else fallback


_ANNUAL_REQUERY_REASON_CODES = frozenset({
    "ANNUAL_FREQUENCY_REQUIRED",
    "SINGLE_ANNUAL_CELL_REQUIRED",
    "BASELINE_MUST_PRECEDE_MEASUREMENT",
    "REQUERY_SERIES_MUTATED",
    "CURRENT_TARGET_NOT_FOUND",
    "ANNUAL_CHANGE_NOT_UNIQUE",
    "ANNUAL_MEASUREMENT_UNSUPPORTED",
    "CURRENT_CELL_NOT_RESOLVED",
    "BASELINE_CELL_NOT_RESOLVED",
    "OFFICIAL_CELL_NOT_NUMERIC",
    "BASELINE_ZERO",
})


def _bounded_annual_requery_reason(exc: BaseException) -> str:
    """Return only a registered annual reason, without exposing exception text."""
    prefix = str(exc).strip().split(":", 1)[0].strip()
    return (
        prefix
        if prefix in _ANNUAL_REQUERY_REASON_CODES
        else "ANNUAL_REQUERY_NOT_EVALUATED"
    )


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise OperationalPipelineError(f"JSON_OBJECT_REQUIRED:{path}")
    return value


_CLARIFICATION_ROLES = ("article_date", "period", "region", "population", "indicator", "unit")
_CLARIFICATION_PERIOD_RE = re.compile(
    r"^(?:\d{4}|\d{4}-\d{2}|\d{4}년\s*[1-4]분기|\d{4}년\s*(?:0?[1-9]|1[0-2])월)$"
)


def _load_articles_for_clarification(
    path: str | Path,
    clarification_context_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Live input loader that defers a missing date to the binding stage."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
        if source.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            value = json.loads(text)
            rows = value if isinstance(value, list) else [value]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveAdapterError("ARTICLE_INPUT_INVALID") from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise LiveAdapterError("ARTICLE_INPUT_OBJECTS_REQUIRED")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        article_id = str(row.get("article_idx") or "").strip()
        title = str(row.get("title") or "").strip()
        body = str(row.get("article_text") or "").strip()
        published = str(row.get("date") or "").strip()
        if not article_id or article_id in seen or not title or not body:
            raise LiveAdapterError("ARTICLE_REQUIRED_FIELD_MISSING")
        if published:
            try:
                calendar_date.fromisoformat(published[:10])
            except ValueError as exc:
                raise LiveAdapterError("ARTICLE_DATE_INVALID") from exc
        seen.add(article_id)
        result.append({**row, "article_idx": article_id, "title": title, "article_text": body, "date": published})
    if clarification_context_path is None:
        return result
    try:
        context_path = Path(clarification_context_path).resolve()
        context = _read_json(context_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, OperationalPipelineError) as exc:
        raise LiveAdapterError("CLARIFICATION_CONTEXT_INVALID") from exc
    answers = context.get("clarification_answers")
    expected_sha = str(context.get("article_body_sha256") or "")
    if context.get("contract_version") != "clarification-context-v1" or not isinstance(answers, list) or len(answers) > 3:
        raise LiveAdapterError("CLARIFICATION_CONTEXT_INVALID")
    if expected_sha and any(
        hashlib.sha256(str(row.get("article_text") or "").encode("utf-8")).hexdigest() != expected_sha
        for row in result
    ):
        raise LiveAdapterError("CLARIFICATION_CONTEXT_FINGERPRINT_MISMATCH")
    hydrated: list[dict[str, Any]] = []
    date_answers = [
        answer
        for answer in answers
        if isinstance(answer, Mapping) and str(answer.get("role") or "").strip() == "article_date"
    ]
    for row in result:
        updated = {**row, "clarification_answers": list(answers)}
        if date_answers:
            answer = date_answers[-1]
            body_sha = hashlib.sha256(str(updated["article_text"]).encode("utf-8")).hexdigest()
            try:
                date_provenance = _article_date_provenance_from_clarification(
                    answer,
                    article_text_sha256=body_sha,
                )
            except OperationalPipelineError as exc:
                raise LiveAdapterError("CLARIFICATION_CONTEXT_INVALID") from exc
            resolved_date = str(answer.get("value") or "").strip()
            updated["date"] = resolved_date
            updated["article_date"] = resolved_date
            updated["article_date_provenance"] = date_provenance
        hydrated.append(updated)
    return hydrated


def _clarification_answer_sha(answer: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(answer), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _article_date_provenance_from_clarification(
    answer: Mapping[str, Any],
    *,
    article_text_sha256: str,
) -> dict[str, str]:
    question_id = str(answer.get("question_id") or "").strip()
    role = str(answer.get("role") or "").strip()
    value = str(answer.get("value") or "").strip()
    if (
        question_id != "clarify-article_date"
        or role != "article_date"
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
    ):
        raise OperationalPipelineError("CLARIFICATION_INVALID")
    try:
        calendar_date.fromisoformat(value)
    except ValueError as exc:
        raise OperationalPipelineError("CLARIFICATION_INVALID") from exc
    answer_sha = _clarification_answer_sha(
        {"question_id": question_id, "role": role, "value": value}
    )
    return {
        "source": "USER_CLARIFICATION",
        "question_id": question_id,
        "role": role,
        "date_source": "user_feedback",
        "source_path": "clarification_context",
        "date_field": "date",
        "article_text_sha256": article_text_sha256,
        "answer_sha256": answer_sha,
    }


def _merge_user_clarifications(row: Mapping[str, Any], article: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(row)
    raw_answers = article.get("clarification_answers")
    if raw_answers in (None, []):
        return merged
    if not isinstance(raw_answers, list) or len(raw_answers) > 3:
        raise OperationalPipelineError("CLARIFICATION_INVALID")
    fields = dict(merged.get("retrieval_fields") if isinstance(merged.get("retrieval_fields"), Mapping) else {})
    clarifications: dict[str, dict[str, Any]] = dict(
        merged.get("user_clarifications") if isinstance(merged.get("user_clarifications"), Mapping) else {}
    )
    for answer in raw_answers:
        if not isinstance(answer, Mapping):
            raise OperationalPipelineError("CLARIFICATION_INVALID")
        role = str(answer.get("role") or "").strip()
        question_id = str(answer.get("question_id") or "").strip()
        value = str(answer.get("value") or "").strip()
        if role not in _CLARIFICATION_ROLES or question_id != f"clarify-{role}" or not value:
            raise OperationalPipelineError("CLARIFICATION_INVALID")
        if role == "article_date":
            try:
                calendar_date.fromisoformat(value)
            except ValueError as exc:
                raise OperationalPipelineError("CLARIFICATION_INVALID") from exc
        elif role == "period" and not _CLARIFICATION_PERIOD_RE.fullmatch(value):
            raise OperationalPipelineError("CLARIFICATION_INVALID")
        elif role != "period" and not 1 <= len(value) <= 120:
            raise OperationalPipelineError("CLARIFICATION_INVALID")
        prior = clarifications.get(role)
        if prior and str(prior.get("value") or "") != value:
            raise OperationalPipelineError("CLARIFICATION_CONFLICT")
        record = {
            "question_id": question_id, "role": role, "value": value,
            "source": "USER_CLARIFICATION",
            "answer_sha256": _clarification_answer_sha({"question_id": question_id, "role": role, "value": value}),
        }
        clarifications[role] = record
        if role == "article_date":
            article_text = str(article.get("article_text") or row.get("article_text") or "")
            merged["article_date"] = value
            merged["date"] = value
            merged["article_date_provenance"] = _article_date_provenance_from_clarification(
                {"question_id": question_id, "role": role, "value": value},
                article_text_sha256=hashlib.sha256(article_text.encode("utf-8")).hexdigest(),
            )
        elif role == "period":
            normalized = value.replace("-", ".")
            if "년" in value:
                normalized = re.sub(r"년\s*", ".", value).replace("월", "").replace("분기", " Q")
            fields["period_absolute"] = normalized
            fields["period_raw"] = value
        else:
            fields[role] = value
    merged["retrieval_fields"] = fields
    merged["user_clarifications"] = clarifications
    return merged


def _service_urls(
    config: Mapping[str, Any],
    *,
    service_urls_override: Mapping[str, str] | None = None,
    ignore_env: bool = False,
) -> dict[str, str]:
    services = config["services"]
    if service_urls_override is not None:
        required = {"qdrant", "query_encoder", "reranker"}
        if set(service_urls_override) != required:
            raise OperationalPipelineError("SERVICE_URL_OVERRIDE_KEYS_INVALID")
        urls = {name: str(service_urls_override[name]) for name in required}
        if any(not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", value) for value in urls.values()):
            raise OperationalPipelineError("SERVICE_URL_OVERRIDE_NON_LOOPBACK")
        return urls
    return {
        "reranker": (None if ignore_env else os.getenv("BGE_RERANKER_URL")) or str(services["reranker"]),
        "query_encoder": (None if ignore_env else os.getenv("BGE_QUERY_ENCODER_URL")) or str(services["query_encoder"]),
        "qdrant": (None if ignore_env else os.getenv("QDRANT_URL")) or str(services["qdrant"]),
    }


def preflight(
    config_path: str | Path = DEFAULT_CONFIG,
    *,
    check_service: bool = False,
    service_urls_override: Mapping[str, str] | None = None,
    ignore_env: bool = False,
    allow_deterministic_answer_only: bool = False,
) -> dict[str, Any]:
    config_path = Path(config_path).resolve()
    config = _read_json(config_path)
    root = config_path.parent.parent
    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env", override=False)
    except ImportError:
        pass
    blockers: list[str] = []
    service_urls = _service_urls(config, service_urls_override=service_urls_override, ignore_env=ignore_env)
    gpu_receipts: dict[str, Any] | None = None
    receipt_config = config.get("assets", {}).get("gpu_receipts") or {}
    try:
        encoder_receipt = receipt_config["query_encoder"]
        reranker_receipt = receipt_config["reranker"]
        gpu_receipts = validate_gpu_receipts(
            (root / encoder_receipt["path"]).resolve(),
            (root / reranker_receipt["path"]).resolve(),
            query_encoder_sha256=str(encoder_receipt["sha256"]),
            reranker_sha256=str(reranker_receipt["sha256"]),
        )
    except (KeyError, TypeError, GpuReceiptError):
        blockers.append("GPU_RECEIPTS_INVALID")
    dense_root = (root / config["assets"]["v6_dense_return"]).resolve()
    dense_manifest_path = dense_root / "return_manifest.json"
    dense_zip = (root / config["assets"].get("v6_dense_return_zip", "")).resolve()
    dense_receipt_path = (root / config["assets"].get("v6_dense_verification_receipt", "")).resolve()
    dense_manifest: dict[str, Any] | None = None
    if dense_receipt_path.is_file() and dense_zip.is_file():
        dense_manifest = _read_json(dense_receipt_path)
        expected = config["contracts"]["v6_dense"]
        if (
            dense_manifest.get("contract") != "embedding-kosis-v6-role-catalog-zip-verification-v1"
            or dense_manifest.get("valid") is not True
            or dense_manifest.get("model") != expected["model"]
            or dense_manifest.get("revision") != expected["revision"]
            or dense_manifest.get("zip_sha256") != _sha_file(dense_zip)
        ):
            blockers.append("V6_DENSE_CONTRACT_MISMATCH")
    elif not dense_manifest_path.is_file():
        blockers.append("V6_DENSE_UNAVAILABLE")
    else:
        dense_manifest = _read_json(dense_manifest_path)
        expected = config["contracts"]["v6_dense"]
        if (
            dense_manifest.get("contract") != "embedding-kosis-v6-role-catalog-return-v1"
            or dense_manifest.get("status") != "COMPLETE"
            or dense_manifest.get("model") != expected["model"]
            or dense_manifest.get("revision") != expected["revision"]
        ):
            blockers.append("V6_DENSE_CONTRACT_MISMATCH")

    bm25_manifest_path = (root / config["assets"].get("v6_bm25_manifest", "")).resolve()
    bm25_manifest: dict[str, Any] | None = None
    if not bm25_manifest_path.is_file():
        blockers.append("V6_BM25_UNAVAILABLE")
    else:
        bm25_manifest = _read_json(bm25_manifest_path)
        bm25_index_path = (root / config["assets"].get("v6_bm25_index", "")).resolve()
        if (
            bm25_manifest.get("contract") != "kosis-v6-bm25-index-v1"
            or bm25_manifest.get("status") != "COMPLETE"
            or bm25_manifest.get("records") != 2_116_791
            or (bm25_manifest.get("authority") or {}).get("candidate_generation_only") is not True
            or (bm25_manifest.get("authority") or {}).get("dimension_value_evidence_authority") is not False
            or not bm25_index_path.is_file()
            or _sha_file(bm25_index_path) != bm25_manifest.get("index_sha256")
        ):
            blockers.append("V6_BM25_CONTRACT_MISMATCH")

    profile_cache_path = (root / config["assets"].get("profile_cache", "")).resolve()
    if (
        not profile_cache_path.is_file()
        or _sha_file(profile_cache_path) != config["assets"].get("profile_cache_sha256")
    ):
        blockers.append("PROFILE_CACHE_CONTRACT_MISMATCH")

    qdrant_manifest_path = (root / config["assets"].get("v6_qdrant_import_manifest", "")).resolve()
    qdrant_manifest: dict[str, Any] | None = None
    if not qdrant_manifest_path.is_file():
        blockers.append("V6_QDRANT_IMPORT_UNAVAILABLE")
    else:
        qdrant_manifest = _read_json(qdrant_manifest_path)
        if (
            qdrant_manifest.get("contract") != "kosis-v6-bge-m3-ko-qdrant-import-v1"
            or qdrant_manifest.get("status") != "COMPLETE"
            or qdrant_manifest.get("points") != 2_116_791
            or qdrant_manifest.get("collection") != config["services"]["qdrant_collection"]
            or qdrant_manifest.get("vector_name") != config["services"].get("qdrant_vector_name")
            or (qdrant_manifest.get("authority") or {}).get("candidate_generation_only") is not True
        ):
            blockers.append("V6_QDRANT_IMPORT_CONTRACT_MISMATCH")
    qdrant_readiness_path = (root / config["assets"].get("v6_qdrant_readiness_receipt", "")).resolve()
    qdrant_readiness: dict[str, Any] | None = None
    if qdrant_readiness_path.is_file():
        qdrant_readiness = _read_json(qdrant_readiness_path)
    if (
        qdrant_readiness is None
        or _sha_file(qdrant_readiness_path) != config["assets"].get("v6_qdrant_readiness_sha256")
        or qdrant_readiness.get("contract") != "kosis-v6-bge-m3-ko-qdrant-readiness-v1"
        or qdrant_readiness.get("status") != "READY"
        or qdrant_readiness.get("points") != 2_116_791
        or qdrant_readiness.get("indexed_vectors") != 2_116_791
        or qdrant_readiness.get("authority") != "CANDIDATE_GENERATION_ONLY"
    ):
        blockers.append("V6_QDRANT_READINESS_RECEIPT_INVALID")
    service_status: dict[str, Any] | None = None
    encoder_status: dict[str, Any] | None = None
    qdrant_status: dict[str, Any] | None = None
    if check_service:
        try:
            with request.urlopen(service_urls["reranker"].rstrip("/") + "/health", timeout=5) as response:
                service_status = json.loads(response.read().decode("utf-8"))
            if (
                service_status.get("contract") != SERVICE_CONTRACT
                or service_status.get("model_revision") != MODEL_REVISION
                or service_status.get("status") != "READY"
                or str((service_status.get("settings") or {}).get("device") or "").lower() != "cuda"
                or not service_status.get("cuda")
            ):
                blockers.append("RERANKER_CONTRACT_MISMATCH")
        except Exception:
            blockers.append("RERANKER_UNAVAILABLE")
        try:
            with request.urlopen(service_urls["query_encoder"].rstrip("/") + "/health", timeout=5) as response:
                encoder_status = json.loads(response.read().decode("utf-8"))
            if (
                encoder_status.get("contract") != ENCODER_SERVICE_CONTRACT
                or encoder_status.get("model_revision") != ENCODER_MODEL_REVISION
                or encoder_status.get("status") != "READY"
                or encoder_status.get("vector_dimension") != 1024
                or encoder_status.get("authority") != "CANDIDATE_GENERATION_ONLY"
                or str((encoder_status.get("settings") or {}).get("device") or "").lower() != "cuda"
                or not encoder_status.get("cuda")
            ):
                blockers.append("QUERY_ENCODER_CONTRACT_MISMATCH")
        except Exception:
            blockers.append("QUERY_ENCODER_UNAVAILABLE")
        try:
            collection_url = (
                service_urls["qdrant"].rstrip("/")
                + "/collections/"
                + config["services"]["qdrant_collection"]
            )
            with request.urlopen(collection_url, timeout=10) as response:
                qdrant_payload = json.loads(response.read().decode("utf-8"))
            qdrant_status = dict(qdrant_payload.get("result") or {})
            if (
                qdrant_status.get("status") != "green"
                or qdrant_status.get("points_count") != 2_116_791
                or qdrant_status.get("indexed_vectors_count") != 2_116_791
            ):
                blockers.append("V6_QDRANT_HNSW_NOT_READY")
        except Exception:
            blockers.append("V6_QDRANT_UNAVAILABLE")
    else:
        blockers.append("RERANKER_SERVICE_NOT_CHECKED")
        blockers.append("QUERY_ENCODER_SERVICE_NOT_CHECKED")
        blockers.append("V6_QDRANT_SERVICE_NOT_CHECKED")
    for variable in ("KOSIS_API_KEY", "NCP_CLOVASTUDIO_API_KEY"):
        if variable == "NCP_CLOVASTUDIO_API_KEY" and allow_deterministic_answer_only:
            continue
        if not os.environ.get(variable):
            blockers.append(f"{variable}_UNAVAILABLE")
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "READY" if not blockers else "BLOCKED",
        "blockers": sorted(set(blockers)),
        "v6_dense_manifest": dense_manifest,
        "v6_dense_manifest_sha256": (
            _sha_file(dense_receipt_path) if dense_receipt_path.is_file()
            else (_sha_file(dense_manifest_path) if dense_manifest_path.is_file() else None)
        ),
        "v6_bm25_manifest": bm25_manifest,
        "v6_qdrant_import_manifest": qdrant_manifest,
        "v6_qdrant_readiness": qdrant_readiness,
        "gpu_receipts": gpu_receipts,
        "service_urls": service_urls,
        "reranker_health": service_status,
        "query_encoder_health": encoder_status,
        "qdrant_health": qdrant_status,
        "silent_fallback": False,
    }


def l2_service_assessment(config_path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Expose the frozen service-readiness decision without overstating quality."""
    config = _read_json(Path(config_path))
    quality = dict(config["l2_quality"])
    observed = float(quality["observed_joint_relaxed"])
    threshold = float(quality["required_joint_relaxed"])
    return {
        "model": "HCX-007",
        "structured_output": True,
        "article_level_inference": True,
        "exact_source_span_validation": True,
        "status": "SERVICE_READY" if observed >= threshold else "SHADOW_READY",
        "observed_joint_relaxed": observed,
        "required_joint_relaxed": threshold,
        "promotion_allowed": observed >= threshold,
        "evidence": quality["evidence"],
    }


def run_operational_l2(
    articles: list[dict[str, Any]],
    *,
    api_key: str,
    runner: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = run_hcx_l2,
    budget_ledger: HttpAttemptBudgetLedger | None = None,
    budget_run_id: str | None = None,
    sentence_span_iterator: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Run HCX-007 once per article and fail each malformed article closed."""
    generation_config = {
        "temperature": 0.0,
        "top_p": 0.8,
        "seed": 1,
        "max_completion_tokens": 4000,
    }
    if budget_ledger is None:
        # Preserve the public/default runner behavior.  Audit mode always
        # supplies the shared ledger below, where retries are explicitly zero.
        runner_kwargs: dict[str, Any] = {
            "api_key": api_key, "model": "HCX-007", "contract": "single",
            "generation_config": generation_config,
        }
        if sentence_span_iterator is not None:
            runner_kwargs["sentence_span_iterator"] = sentence_span_iterator
        predictions, manifest = runner(articles, **runner_kwargs)
    else:
        predictions = []
        errors: list[dict[str, Any]] = []
        manifests: list[dict[str, Any]] = []
        for article in articles:
            article_id = str(article.get("article_idx") or "")
            runner_kwargs = {
                "api_key": api_key, "model": "HCX-007", "retries": 0,
                "contract": "single", "generation_config": generation_config,
            }
            if sentence_span_iterator is not None:
                runner_kwargs["sentence_span_iterator"] = sentence_span_iterator
            one_predictions, one_manifest = budget_ledger.execute(
                budget_run_id or "audit", "hcx_l2",
                lambda article=article, runner_kwargs=runner_kwargs: runner([article], **runner_kwargs),
                target_id=article_id,
            )
            predictions.extend(one_predictions)
            errors.extend(one_manifest.get("errors") or [])
            manifests.append(one_manifest)
        manifest = {"errors": errors, "article_runs": [row for item in manifests for row in item.get("article_runs") or []]}
    return materialize_operational_l2(
        articles, predictions, manifest, external_model_calls=len(articles),
        sentence_span_iterator=sentence_span_iterator,
    )


@dataclass(frozen=True)
class Top50Resolution:
    resolution: TargetResolution
    projections: tuple[CandidateProjection, ...]
    candidate_membership: tuple[str, ...]
    projected_count: int
    # Generic resolution used to carry only the four fields above.  Keep
    # defaults so existing positional construction remains source-compatible,
    # while pinning the already-read profiles for the live selection step.
    pinned_raw_profiles: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    pinned_projection_profiles: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}),
    )


def _pin_profile_snapshots(
    scope: Sequence[str],
    profiles: Sequence[Mapping[str, Any] | None],
) -> Mapping[str, Mapping[str, Any]]:
    """Freeze in-memory profile snapshots without performing a provider read."""

    pinned: dict[str, Mapping[str, Any]] = {}
    for table_key, profile in zip(scope, profiles):
        if isinstance(profile, Mapping):
            # Copy before exposing the snapshot so a transform or later
            # caller mutation cannot alter the profile used for the receipt.
            pinned[str(table_key)] = MappingProxyType(deepcopy(dict(profile)))
    return MappingProxyType(pinned)


@dataclass(frozen=True)
class MonthlyTop50ResolutionV2H:
    resolution: TargetResolution
    projections: tuple[CandidateProjection, ...]
    candidate_membership: tuple[str, ...]
    projected_count: int
    pinned_raw_profiles: Mapping[str, Mapping[str, Any]]
    pinned_projection_profiles: Mapping[str, Mapping[str, Any]]
    profile_receipts: tuple[dict[str, Any], ...]
    membership_receipt_sha256: str


def _monthly_profile_bytes_v2h(profile: Mapping[str, Any]) -> bytes:
    return json.dumps(
        profile, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")


def _monthly_profile_inventory_complete_v2h(profile: Mapping[str, Any]) -> bool:
    return all(
        isinstance(profile.get(key), list) and bool(profile.get(key))
        for key in ("items", "dimensions", "periods")
    )


def resolve_top50_monthly_v2h(
    claim_core: Any,
    ranked_candidates: Sequence[Any],
    profile_provider: Callable[[str], Mapping[str, Any] | None],
    *,
    profile_transform: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    allow_unqualified_nationwide: bool = False,
    table_context_terms: Sequence[str] = (),
) -> MonthlyTop50ResolutionV2H:
    """Pin, receipt, identity-check, and project one fixed Top-50 scope."""

    scope = tuple(
        str(getattr(row, "table_key", None) or row["table_key"])
        for row in ranked_candidates[:50]
    )
    if len(scope) != len(set(scope)):
        raise OperationalPipelineError("DUPLICATE_CANDIDATE_SCOPE")
    raw_profiles: dict[str, Mapping[str, Any]] = {}
    projection_profiles: dict[str, Mapping[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    for table_key in scope:
        supplied = profile_provider(table_key)
        raw_sha: str | None = None
        projection_sha: str | None = None
        status = "UNAVAILABLE"
        raw_snapshot: Mapping[str, Any] | None = None
        if isinstance(supplied, Mapping):
            raw_bytes = _monthly_profile_bytes_v2h(supplied)
            raw_sha = hashlib.sha256(raw_bytes).hexdigest()
            decoded = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(decoded, Mapping):
                raw_snapshot = decoded
                raw_profiles[table_key] = raw_snapshot
                if str(raw_snapshot.get("table_key") or "") != table_key:
                    status = "TABLE_KEY_MISMATCH"
                elif not _monthly_profile_inventory_complete_v2h(raw_snapshot):
                    status = "INCOMPLETE"
                else:
                    transform_input = json.loads(raw_bytes.decode("utf-8"))
                    try:
                        transformed = (
                            profile_transform(transform_input)
                            if profile_transform is not None else transform_input
                        )
                    except Exception:
                        status = "TRANSFORM_ERROR"
                    else:
                        if not isinstance(transformed, Mapping):
                            status = "TRANSFORM_INVALID"
                        elif str(transformed.get("table_key") or "") != table_key:
                            status = "TRANSFORM_TABLE_KEY_MISMATCH"
                        elif not _monthly_profile_inventory_complete_v2h(transformed):
                            status = "TRANSFORM_INCOMPLETE"
                        else:
                            projection_bytes = _monthly_profile_bytes_v2h(transformed)
                            projection_snapshot = json.loads(projection_bytes.decode("utf-8"))
                            projection_profiles[table_key] = projection_snapshot
                            projection_sha = hashlib.sha256(projection_bytes).hexdigest()
                            status = "COMPLETE"
        receipts.append({
            "contract_version": "monthly-profile-receipt-v2f",
            "table_key": table_key,
            "status": status,
            "raw_profile_sha256": raw_sha,
            "projection_profile_sha256": projection_sha,
        })

    membership_sha = membership_receipt_sha256_monthly_v2h(scope, receipts)
    coverage_incomplete = any(receipt["status"] != "COMPLETE" for receipt in receipts)
    normalized_terms = [
        re.sub(r"[\s\-_./:(),]+", "", str(term or "")).casefold()
        for term in table_context_terms if str(term or "").strip()
    ]
    context_matches: dict[str, bool] = {}
    if not coverage_incomplete and normalized_terms:
        context_matches = {
            table_key: any(
                term in re.sub(
                    r"[\s\-_./:(),]+", "",
                    str(projection_profiles[table_key].get("tbl_name") or ""),
                ).casefold()
                for term in normalized_terms
            )
            for table_key in scope
        }
        if not any(context_matches.values()):
            context_matches = {}

    if coverage_incomplete:
        reason_by_key = {
            receipt["table_key"]: (
                "PROFILE_UNAVAILABLE"
                if receipt["status"] == "UNAVAILABLE" else "PROFILE_INCOMPLETE"
            )
            for receipt in receipts
        }
        projections = tuple(
            placeholder_projection_monthly_v2h(
                table_key, reason_by_key.get(table_key, "PROFILE_INCOMPLETE")
            )
            for table_key in scope
        )
    else:
        projections = tuple(
            placeholder_projection_monthly_v2h(table_key, "TABLE_CONTEXT_MISMATCH")
            if context_matches and not context_matches[table_key]
            else project_candidate_monthly_v2h(
                claim_core, projection_profiles[table_key], table_key=table_key,
                allow_unqualified_nationwide=allow_unqualified_nationwide,
            )
            for table_key in scope
        )
    if len(projections) != len(scope):
        raise OperationalPipelineError("EARLY_PROJECTION_EXIT")
    resolution = validate_target_monthly_v2h(
        projections,
        candidate_membership=scope,
        profile_receipts=receipts,
    )
    return MonthlyTop50ResolutionV2H(
        resolution=resolution,
        projections=projections,
        candidate_membership=scope,
        projected_count=len(projections),
        pinned_raw_profiles=MappingProxyType(dict(raw_profiles)),
        pinned_projection_profiles=MappingProxyType(dict(projection_profiles)),
        profile_receipts=tuple(receipts),
        membership_receipt_sha256=membership_sha,
    )


def resolve_top50_monthly_v2j(
    claim_core: Any,
    ranked_candidates: Sequence[Any],
    profile_provider: Callable[[str], Mapping[str, Any] | None],
    *,
    profile_transform: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    allow_unqualified_nationwide: bool = False,
    table_context_terms: Sequence[str] = (),
) -> MonthlyTop50ResolutionV2H:
    """Run the sealed v2h pinning gates with the v2j monthly projector."""

    scope = tuple(
        str(getattr(row, "table_key", None) or row["table_key"])
        for row in ranked_candidates[:50]
    )
    if len(scope) != len(set(scope)):
        raise OperationalPipelineError("DUPLICATE_CANDIDATE_SCOPE")
    raw_profiles: dict[str, Mapping[str, Any]] = {}
    projection_profiles: dict[str, Mapping[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    for table_key in scope:
        supplied = profile_provider(table_key)
        raw_sha: str | None = None
        projection_sha: str | None = None
        status = "UNAVAILABLE"
        if isinstance(supplied, Mapping):
            raw_bytes = _monthly_profile_bytes_v2h(supplied)
            raw_sha = hashlib.sha256(raw_bytes).hexdigest()
            raw_snapshot = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(raw_snapshot, Mapping):
                raw_profiles[table_key] = raw_snapshot
                if str(raw_snapshot.get("table_key") or "") != table_key:
                    status = "TABLE_KEY_MISMATCH"
                elif not _monthly_profile_inventory_complete_v2h(raw_snapshot):
                    status = "INCOMPLETE"
                else:
                    transform_input = json.loads(raw_bytes.decode("utf-8"))
                    try:
                        transformed = (
                            profile_transform(transform_input)
                            if profile_transform is not None else transform_input
                        )
                    except Exception:
                        status = "TRANSFORM_ERROR"
                    else:
                        if not isinstance(transformed, Mapping):
                            status = "TRANSFORM_INVALID"
                        elif str(transformed.get("table_key") or "") != table_key:
                            status = "TRANSFORM_TABLE_KEY_MISMATCH"
                        elif not _monthly_profile_inventory_complete_v2h(transformed):
                            status = "TRANSFORM_INCOMPLETE"
                        else:
                            projection_bytes = _monthly_profile_bytes_v2h(transformed)
                            projection_profiles[table_key] = json.loads(projection_bytes.decode("utf-8"))
                            projection_sha = hashlib.sha256(projection_bytes).hexdigest()
                            status = "COMPLETE"
        receipts.append({
            "contract_version": "monthly-profile-receipt-v2f",
            "table_key": table_key,
            "status": status,
            "raw_profile_sha256": raw_sha,
            "projection_profile_sha256": projection_sha,
        })

    membership_sha = membership_receipt_sha256_monthly_v2h(scope, receipts)
    coverage_incomplete = any(receipt["status"] != "COMPLETE" for receipt in receipts)
    normalized_terms = [
        re.sub(r"[\s\-_./:(),]+", "", str(term or "")).casefold()
        for term in table_context_terms if str(term or "").strip()
    ]
    context_matches: dict[str, bool] = {}
    if not coverage_incomplete and normalized_terms:
        context_matches = {
            table_key: any(
                term in re.sub(
                    r"[\s\-_./:(),]+", "",
                    str(projection_profiles[table_key].get("tbl_name") or ""),
                ).casefold()
                for term in normalized_terms
            )
            for table_key in scope
        }
        if not any(context_matches.values()):
            context_matches = {}

    if coverage_incomplete:
        reason_by_key = {
            receipt["table_key"]: (
                "PROFILE_UNAVAILABLE" if receipt["status"] == "UNAVAILABLE"
                else "PROFILE_INCOMPLETE"
            )
            for receipt in receipts
        }
        projections = tuple(
            placeholder_projection_monthly_v2h(
                table_key, reason_by_key.get(table_key, "PROFILE_INCOMPLETE")
            )
            for table_key in scope
        )
    else:
        projections = tuple(
            placeholder_projection_monthly_v2h(table_key, "TABLE_CONTEXT_MISMATCH")
            if context_matches and not context_matches[table_key]
            else project_candidate_monthly_v2j(
                claim_core, projection_profiles[table_key], table_key=table_key,
                allow_unqualified_nationwide=allow_unqualified_nationwide,
            )
            for table_key in scope
        )
    if len(projections) != len(scope):
        raise OperationalPipelineError("EARLY_PROJECTION_EXIT")
    resolution = validate_target_monthly_v2h(
        projections, candidate_membership=scope, profile_receipts=receipts,
    )
    if (
        resolution.outcome != "QUERY_READY"
        and any("MULTIPLE_COMPATIBLE_SERIES" in projection.hold_reasons for projection in projections)
    ):
        data = {
            "outcome": "HOLD",
            "hold_reason": "MULTIPLE_COMPATIBLE_SERIES",
            "query_plan": None,
            "chosen_table_key": None,
            "compatible_series": resolution.compatible_series,
            "audit": resolution.audit,
        }
        resolution = TargetResolution(
            **data,
            canonical_sha256=hashlib.sha256(
                json.dumps(
                    data, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=str,
                ).encode("utf-8")
            ).hexdigest(),
        )
    return MonthlyTop50ResolutionV2H(
        resolution=resolution,
        projections=projections,
        candidate_membership=scope,
        projected_count=len(projections),
        pinned_raw_profiles=MappingProxyType(dict(raw_profiles)),
        pinned_projection_profiles=MappingProxyType(dict(projection_profiles)),
        profile_receipts=tuple(receipts),
        membership_receipt_sha256=membership_sha,
    )


def _release_bound_item_specificity(assignment: CandidateAssignment) -> dict[str, Any]:
    item = next(
        (binding for binding in assignment.bindings if binding.axis_kind == "ITEM"),
        None,
    )
    evidence = item.evidence if item is not None else {}
    consumed = evidence.get("consumed_span") if isinstance(evidence, Mapping) else None
    source_backed = bool(
        isinstance(consumed, Mapping)
        and isinstance(consumed.get("start"), int)
        and isinstance(consumed.get("end"), int)
        and consumed.get("end") > consumed.get("start")
        and bool(consumed.get("text"))
    )
    binding_basis = str(item.binding_basis if item is not None else "")
    exact_or_semantic = binding_basis in {"EXACT_LABEL", "SEMANTIC_ALIAS"} or (
        isinstance(evidence, Mapping)
        and str(evidence.get("match_rule") or "").endswith("semantic_alias")
    )
    specific = source_backed and exact_or_semantic and binding_basis != "SINGLETON_INVENTORY"
    return {
        "source_backed_consumed_span": source_backed,
        "exact_or_semantic_item": exact_or_semantic,
        "binding_basis": binding_basis,
        "score": 1 if specific else 0,
    }


def _release_bound_geo_binding(binding: Any) -> bool:
    if getattr(binding, "axis_kind", "") != "DIMENSION":
        return False
    if getattr(binding, "binding_basis", "") != "DISCLOSED_NATIONWIDE_DEFAULT":
        return False
    evidence = getattr(binding, "evidence", {})
    disclosure = evidence.get("inference_disclosure") if isinstance(evidence, Mapping) else None
    return isinstance(disclosure, Mapping) and disclosure.get("rule_id") == "unqualified-geographic-axis-nationwide"


def _release_bound_semantic_signature(assignment: CandidateAssignment) -> tuple[Any, ...]:
    return tuple(
        (
            binding.axis_kind,
            binding.axis_id,
            binding.value_id,
            binding.bound_atom,
        )
        for binding in assignment.bindings
        if not _release_bound_geo_binding(binding)
    )


def _release_bound_geo_signature(assignment: CandidateAssignment) -> tuple[Any, ...]:
    result: list[tuple[Any, ...]] = []
    for binding in assignment.bindings:
        if not _release_bound_geo_binding(binding):
            continue
        evidence = binding.evidence if isinstance(binding.evidence, Mapping) else {}
        value_evidence = evidence.get("value_evidence") if isinstance(evidence.get("value_evidence"), Mapping) else {}
        result.append(
            (
                binding.value_id,
                str(value_evidence.get("profile_label") or evidence.get("profile_label") or ""),
            )
        )
    return tuple(result)


def _release_bound_geo_cardinality(
    assignment: CandidateAssignment, profile: Mapping[str, Any] | None,
) -> tuple[int, ...] | None:
    if not isinstance(profile, Mapping):
        return None
    dimensions = profile.get("dimensions")
    if not isinstance(dimensions, list):
        return None
    cardinalities: list[int] = []
    for binding in assignment.bindings:
        if not _release_bound_geo_binding(binding):
            continue
        evidence = binding.evidence if isinstance(binding.evidence, Mapping) else {}
        axis_evidence = evidence.get("axis_evidence") if isinstance(evidence.get("axis_evidence"), Mapping) else {}
        path = str(axis_evidence.get("profile_inventory_path") or "")
        match = re.fullmatch(r"dimensions\[(\d+)\]", path)
        if match is None:
            return None
        index = int(match.group(1))
        if index >= len(dimensions) or not isinstance(dimensions[index], Mapping):
            return None
        values = dimensions[index].get("values")
        if not isinstance(values, list) or not values:
            return None
        cardinalities.append(len(values))
    return tuple(cardinalities) if cardinalities else None


def _release_bound_send_de(profile: Mapping[str, Any] | None) -> str | None:
    if not isinstance(profile, Mapping):
        return None
    raw = profile.get("send_de")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = calendar_date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == raw else None


def _release_bound_candidate_receipt(
    assignment: CandidateAssignment, profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    item = _release_bound_item_specificity(assignment)
    geo_bindings = [binding for binding in assignment.bindings if _release_bound_geo_binding(binding)]
    dimension_bindings = [binding for binding in assignment.bindings if binding.axis_kind == "DIMENSION"]
    cardinality = _release_bound_geo_cardinality(assignment, profile)
    send_de = _release_bound_send_de(profile)
    return {
        "table_key": assignment.table_key,
        "signature": str(assignment.signature),
        "send_de": send_de,
        "send_de_valid": send_de is not None,
        "item_specificity": item,
        "components": {
            "non_geo_semantic_signature": str(_release_bound_semantic_signature(assignment)),
            "geo_signature": str(_release_bound_geo_signature(assignment)),
            "all_geographic_axes_disclosed_nationwide": bool(dimension_bindings) and len(geo_bindings) == len(dimension_bindings),
            "geo_dimension_cardinality": list(cardinality) if cardinality is not None else None,
        },
        "score": {
            "item_source_specificity": item["score"],
            "coarser_geo_cardinality": -min(cardinality) if cardinality else None,
        },
    }


def _apply_release_bound_evidence_specificity_dominance(
    resolution: TargetResolution,
    projections: Sequence[CandidateProjection],
    profiles_by_table: Mapping[str, Mapping[str, Any] | None],
) -> TargetResolution:
    if resolution.hold_reason != "MULTIPLE_COMPATIBLE_SERIES":
        return resolution
    records = [
        {
            "assignment": assignment,
            "receipt": _release_bound_candidate_receipt(
                assignment, profiles_by_table.get(assignment.table_key)
            ),
        }
        for projection in projections
        for assignment in projection.assignments
    ]
    receipt: dict[str, Any] = {
        "rule_id": RELEASE_BOUND_DOMINANCE_RULE_ID,
        "rule_version": RELEASE_BOUND_DOMINANCE_RULE_VERSION,
        "candidates": [row["receipt"] for row in records],
        "score_components": [
            "item_source_specificity",
            "coarser_geo_cardinality",
            "latest_send_de",
        ],
        "chosen_table": None,
        "decision": "NOT_UNIQUE",
        "latest_send_de_decision": {
            "status": "NOT_APPLIED",
            "latest_send_de": None,
            "reason": "EVIDENCE_SPECIFICITY_NOT_TIED",
        },
    }
    if not records:
        return resolution

    specific = [
        row for row in records if row["receipt"]["item_specificity"]["score"] == 1
    ]
    considered = specific if specific else records
    if len(considered) == 1:
        chosen = considered[0]["assignment"]
    else:
        semantic_groups = {
            _release_bound_semantic_signature(row["assignment"])
            for row in considered
        }
        geo_groups = {
            _release_bound_geo_signature(row["assignment"])
            for row in considered
        }
        all_nationwide = all(
            row["receipt"]["components"]["all_geographic_axes_disclosed_nationwide"]
            for row in considered
        )
        chosen = None
        selector_groups = {
            (
                _release_bound_semantic_signature(row["assignment"]),
                _release_bound_geo_signature(row["assignment"]),
            )
            for row in considered
        }

        # Publication date is usable only inside one already-compatible
        # semantic/cell-selector group.  It deliberately precedes the
        # geographic cardinality rule: a newer compatible table must win even
        # when its geographic inventory is finer.
        date_pool = considered
        send_dates = [row["receipt"].get("send_de") for row in date_pool]
        valid_send_dates = all(
            isinstance(value, str)
            and _release_bound_send_de({"send_de": value}) == value
            for value in send_dates
        )
        if len(selector_groups) == 1 and all_nationwide:
            if not valid_send_dates:
                receipt["latest_send_de_decision"] = {
                    "status": "FAIL_CLOSED",
                    "latest_send_de": None,
                    "reason": "SEND_DE_MISSING_OR_INVALID",
                }
            else:
                latest = max(send_dates)
                latest_rows = [
                    row for row in date_pool
                    if row["receipt"].get("send_de") == latest
                ]
                latest_decision = "CHOSEN" if len(latest_rows) == 1 else "TIE_SUBSET"
                receipt["latest_send_de_decision"] = {
                    "status": latest_decision,
                    "latest_send_de": latest,
                    "reason": "SEMANTIC_PERIOD_SELECTOR_TIE",
                }
                if len(latest_rows) == 1:
                    chosen = latest_rows[0]["assignment"]
                else:
                    # If publication dates tie, apply coarser geography only
                    # within that tied subset; never let an older candidate
                    # participate in this specificity decision.
                    latest_cardinalities = [
                        _release_bound_geo_cardinality(
                            row["assignment"], profiles_by_table.get(row["assignment"].table_key)
                        )
                        for row in latest_rows
                    ]
                    if all(value is not None and len(value) == 1 for value in latest_cardinalities):
                        minimum = min(value[0] for value in latest_cardinalities if value is not None)
                        winners = [
                            row for row, value in zip(latest_rows, latest_cardinalities)
                            if value is not None and value[0] == minimum
                        ]
                        if len(winners) == 1:
                            chosen = winners[0]["assignment"]
                            receipt["latest_send_de_decision"]["status"] = "CHOSEN_COARSER_GEO"
                        else:
                            receipt["latest_send_de_decision"]["status"] = "NOT_UNIQUE"
        elif len(selector_groups) == 1:
            receipt["latest_send_de_decision"] = {
                "status": "FAIL_CLOSED",
                "latest_send_de": None,
                "reason": "SEND_DE_NOT_APPLICABLE_OR_GEOGRAPHY_NOT_NATIONWIDE",
            }

    if chosen is None:
        return TargetResolution(
            resolution.outcome,
            resolution.hold_reason,
            resolution.query_plan,
            resolution.chosen_table_key,
            resolution.compatible_series,
            {**resolution.audit, "release_bound_evidence_specificity_dominance": receipt},
            hashlib.sha256(
                canonical_bytes({
                    "outcome": resolution.outcome,
                    "hold_reason": resolution.hold_reason,
                    "query_plan": resolution.query_plan,
                    "chosen_table_key": resolution.chosen_table_key,
                    "compatible_series": resolution.compatible_series,
                    "audit": {**resolution.audit, "release_bound_evidence_specificity_dominance": receipt},
                })
            ).hexdigest(),
        )
    plan = _query_plan(chosen)
    if plan is None:
        return resolution
    receipt["chosen_table"] = chosen.table_key
    receipt["decision"] = "CHOSEN"
    audit = {**resolution.audit, "release_bound_evidence_specificity_dominance": receipt}
    data = {
        "outcome": "QUERY_READY",
        "hold_reason": None,
        "query_plan": plan,
        "chosen_table_key": chosen.table_key,
        "compatible_series": resolution.compatible_series,
        "audit": audit,
    }
    return TargetResolution(**data, canonical_sha256=hashlib.sha256(canonical_bytes(data)).hexdigest())


def resolve_top50(
    claim_core: Any,
    ranked_candidates: Sequence[Any],
    profile_provider: Callable[[str], Mapping[str, Any] | None],
    *,
    profile_transform: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    allow_unqualified_nationwide: bool = False,
    table_context_terms: Sequence[str] = (),
    release_bound_mode: bool = False,
) -> Top50Resolution:
    """Project every fixed-scope candidate before global uniqueness.

    ``release_bound_mode`` is an explicit opt-in for evidence-specificity
    dominance.  The ordinary resolver remains unchanged when it is false.
    """
    scope = tuple(str(getattr(row, "table_key", None) or row["table_key"]) for row in ranked_candidates[:50])
    if len(scope) != len(set(scope)):
        raise OperationalPipelineError("DUPLICATE_CANDIDATE_SCOPE")
    profiles = [profile_provider(table_key) for table_key in scope]
    raw_profiles = tuple(profiles)
    if profile_transform is not None:
        profiles = [
            profile_transform(deepcopy(profile)) if profile is not None else None
            for profile in profiles
        ]
    normalized_terms = [
        re.sub(r"[\s\-_./:(),]+", "", str(term or "")).casefold()
        for term in table_context_terms
        if str(term or "").strip()
    ]
    if normalized_terms:
        context_matches = [
            bool(profile) and any(
                term in re.sub(r"[\s\-_./:(),]+", "", str(profile.get("tbl_name") or "")).casefold()
                for term in normalized_terms
            )
            for profile in profiles
        ]
        # Context is a constraint only when at least one candidate title
        # actually carries it. Otherwise the ordinary fail-closed scope is
        # preserved rather than eliminating every candidate.
        if any(context_matches):
            profiles = [profile if matched else None for profile, matched in zip(profiles, context_matches)]
    projections = tuple(
        project_candidate_v2(
            claim_core,
            profile,
            allow_unqualified_nationwide=allow_unqualified_nationwide,
        )
        for profile in profiles
    )
    if len(projections) != len(scope):
        raise OperationalPipelineError("EARLY_PROJECTION_EXIT")
    resolution = validate_target_v2(projections)
    if release_bound_mode:
        resolution = _apply_release_bound_evidence_specificity_dominance(
            resolution,
            projections,
            {table_key: profile for table_key, profile in zip(scope, profiles)},
        )
    return Top50Resolution(
        resolution,
        projections,
        scope,
        len(projections),
        pinned_raw_profiles=_pin_profile_snapshots(scope, raw_profiles),
        pinned_projection_profiles=_pin_profile_snapshots(scope, profiles),
    )


def assignment_for_resolution(result: Top50Resolution) -> CandidateAssignment | None:
    if result.resolution.outcome != "QUERY_READY":
        return None
    matches = [
        assignment
        for projection in result.projections
        for assignment in projection.assignments
        if assignment.table_key == result.resolution.chosen_table_key
    ]
    return matches[0] if len(matches) == 1 else None


def fetch_exact_single_cell(
    query_plan: Mapping[str, Any],
    fetcher: Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
) -> dict[str, Any]:
    """Fetch one official cell and fail closed on zero, multiple, or API error."""
    response = fetcher(dict(query_plan))
    if isinstance(response, dict):
        return {"status": "CELL_API_ERROR", "query": dict(query_plan), "response": response}
    if not isinstance(response, list):
        return {"status": "CELL_RESPONSE_INVALID", "query": dict(query_plan)}
    if not response:
        return {"status": "NO_CELL", "query": dict(query_plan), "rows": []}
    if len(response) != 1:
        return {"status": "MULTIPLE_CELLS", "query": dict(query_plan), "row_count": len(response)}
    cell = dict(response[0])
    checks = {
        "ORG_ID": query_plan.get("org_id"),
        "TBL_ID": query_plan.get("tbl_id"),
        "ITM_ID": query_plan.get("itm_id"),
    }
    for key, expected in checks.items():
        actual = cell.get(key)
        if actual not in (None, "") and str(actual) != str(expected):
            return {
                "status": "CELL_QUERY_MISMATCH",
                "query": dict(query_plan),
                "mismatch": {"field": key, "expected": expected, "actual": actual},
            }
    response_frequency = cell.get("PRD_SE")
    requested_frequency = query_plan.get("prd_se")
    annual_alias = {str(response_frequency), str(requested_frequency)} == {"A", "Y"}
    if (
        response_frequency not in (None, "")
        and str(response_frequency) != str(requested_frequency)
        and not annual_alias
    ):
        return {
            "status": "CELL_QUERY_MISMATCH",
            "query": dict(query_plan),
            "mismatch": {
                "field": "PRD_SE",
                "expected": requested_frequency,
                "actual": response_frequency,
                "normalization_rule": "kosis-param-annual-a-y-v1",
            },
        }
    period = cell.get("PRD_DE")
    if period not in (None, "") and not (
        str(query_plan.get("start_prd_de")) <= str(period) <= str(query_plan.get("end_prd_de"))
    ):
        return {
            "status": "CELL_QUERY_MISMATCH",
            "query": dict(query_plan),
            "mismatch": {"field": "PRD_DE", "expected": [query_plan.get("start_prd_de"), query_plan.get("end_prd_de")], "actual": period},
        }
    for index, expected in enumerate((query_plan.get("obj_levels") or {}).values(), 1):
        actual = cell.get(f"C{index}")
        if actual not in (None, "") and str(actual) != str(expected):
            return {
                "status": "CELL_QUERY_MISMATCH",
                "query": dict(query_plan),
                "mismatch": {"field": f"C{index}", "expected": expected, "actual": actual},
            }
    response_sha = hashlib.sha256(
        json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return {
        "status": "CELL_RESOLVED",
        "query": dict(query_plan),
        "cell": cell,
        "response_sha256": response_sha,
        "response_contract": {
            "period_frequency_rule": "kosis-param-annual-a-y-v1" if annual_alias else "exact",
        },
    }


def compare_official_cell(
    claim_value_text: str,
    claim_unit_text: str,
    cell: Mapping[str, Any],
    official_unit_text: str,
) -> dict[str, Any]:
    """Return a deterministic verdict through the sole Decimal quantity path."""
    try:
        claim = normalize_quantity(claim_value_text, claim_unit_text, provenance={"source": "claim"})
        official = normalize_quantity(cell.get("DT"), official_unit_text, provenance={"source": "KOSIS", "cell": dict(cell)})
    except QuantityNormalizationError as exc:
        return {"verdict": "UNVERIFIABLE", "reason": exc.code}
    comparison = compare_canonical(claim, official, precision_tolerance=True)
    if comparison.status not in {"MATCH", "VALUE_MISMATCH"}:
        return {"verdict": "UNVERIFIABLE", "reason": comparison.status}
    return {
        "verdict": "VERIFIED" if comparison.match else "REFUTED",
        "reason": comparison.status,
        "claim": asdict(claim),
        "official": asdict(official),
        "comparison": asdict(comparison),
    }


def _target_id(row: Mapping[str, Any]) -> str:
    return f"{row.get('article_idx')}:{row.get('value_span_id') or row.get('sentence_id') or 'target'}"


def _bounded_article_date_provenance(article: Mapping[str, Any], article_text: str) -> dict[str, Any]:
    """Carry the bounded input date provenance into routed evidence."""
    input_provenance = article.get("article_date_provenance")
    if not isinstance(input_provenance, Mapping):
        input_provenance = article.get("date_provenance")
    provenance = dict(input_provenance) if isinstance(input_provenance, Mapping) else {}
    date_source = str(article.get("date_source") or "").strip()
    if date_source and "date_source" not in provenance:
        provenance["date_source"] = date_source
    provenance.setdefault("source_path", "operational_input")
    provenance.setdefault("date_field", "date")
    provenance["article_text_sha256"] = hashlib.sha256(article_text.encode("utf-8")).hexdigest()
    return provenance


def _uses_monthly_claim_contract(row: Mapping[str, Any]) -> bool:
    """Select monthly-v2h only for a lossless monthly source expression."""
    fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
    period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
    measurement = period.get("measurement") if isinstance(period.get("measurement"), Mapping) else {}
    absolute = str(measurement.get("absolute") or fields.get("period_absolute") or "").strip()
    raw = str(measurement.get("raw") or fields.get("period_raw") or "").strip()
    return bool(
        re.fullmatch(r"\d{4}-\d{2}", absolute)
        and re.search(r"(?:월|month)|\d{4}[-./]\d{1,2}", raw, re.IGNORECASE)
    )


def _annual_claim_contract(row: Mapping[str, Any], query_plan: Mapping[str, Any]) -> bool:
    fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
    measurement = str(fields.get("measurement_type") or "").upper()
    period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
    measure = period.get("measurement") if isinstance(period.get("measurement"), Mapping) else {}
    absolute = str(measure.get("absolute") or fields.get("period_absolute") or "")
    return (
        measurement in {"LEVEL", "CHANGE_RATE", "CHANGE_POINT", "DIFFERENCE"}
        and str(query_plan.get("prd_se") or "") in {"Y", "A"}
        and bool(re.fullmatch(r"\d{4}", absolute))
    )


def _annual_change_only_row(row: Mapping[str, Any]) -> bool:
    fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
    measurement = str(fields.get("measurement_type") or "").upper()
    period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
    measure = period.get("measurement") if isinstance(period.get("measurement"), Mapping) else {}
    absolute = str(measure.get("absolute") or fields.get("period_absolute") or "").strip()
    return measurement in {"CHANGE_RATE", "CHANGE_POINT", "DIFFERENCE"} and bool(
        re.fullmatch(r"\d{4}", absolute)
    )


def _release_bound_annual_period_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse one source-backed annual YOY duplicate only in release-bound mode."""
    result = dict(row)
    fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
    measurement = str(fields.get("measurement_type") or "").upper()
    period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
    measurement_period = period.get("measurement") if isinstance(period.get("measurement"), Mapping) else {}
    current_absolute = str(
        measurement_period.get("absolute") or fields.get("period_absolute") or ""
    ).strip()
    if (
        measurement not in {"CHANGE_RATE", "CHANGE_POINT", "DIFFERENCE"}
        or period.get("basis") not in {None, "", NO_BASIS}
        or not re.fullmatch(r"\d{4}", current_absolute)
    ):
        return result
    sentence = str(row.get("sentence_text") or "")
    generic_basis, _generic_marker, _generic_start = comparison_basis(sentence, measurement)
    collapsed_basis, marker, marker_start = comparison_basis_monthly_v2h(sentence, measurement)
    if (
        generic_basis != NO_BASIS
        or collapsed_basis != YOY
        or marker_start is None
        or not marker
    ):
        return result
    marker_end = marker_start + len(marker)
    if sentence[marker_start:marker_end] != marker:
        return result
    baseline_absolute = str(int(current_absolute) - 1)
    receipt = {
        "rule_id": "release-bound-annual-yoy-same-span-collapse-v1",
        "rule_version": 1,
        "source_span": {
            "start": marker_start,
            "end": marker_end,
            "text": sentence[marker_start:marker_end],
        },
        "collapsed_bases": ["YOY", "PERIOD_TO_PERIOD"],
        "selected_basis": YOY,
        "measurement_absolute": current_absolute,
        "baseline_absolute": baseline_absolute,
    }
    normalized_period = {
        **dict(period),
        "basis": YOY,
        "baseline": {"raw": marker, "absolute": baseline_absolute},
        "release_bound_basis_receipt": receipt,
    }
    result["retrieval_fields"] = {**dict(fields), "period": normalized_period}
    result["release_bound_annual_period_receipt"] = receipt
    return result


def _annual_change_projection_core(row: Mapping[str, Any]) -> ClaimCore:
    """Project an annual change claim against a LEVEL item's series unit.

    A percent/difference claim is an operation over two official LEVEL cells;
    its claim unit must not be used as the profile ITEM unit during binding.
    The original claim unit remains in the atom provenance for audit, while
    the projection-facing unit atom is intentionally UNKNOWN.
    """
    core = build_claim_core_v2(row)
    if not _annual_change_only_row(row):
        return core
    atoms = dict(core.atoms)
    period_atom = atoms.get("period")
    period_receipt = row.get("release_bound_annual_period_receipt")
    if period_atom is not None and isinstance(period_receipt, Mapping):
        atoms["period"] = ClaimAtom(
            period_atom.role,
            period_atom.surface,
            period_atom.status,
            {
                **dict(period_atom.provenance),
                "release_bound_period_basis_receipt": dict(period_receipt),
            },
        )
    unit_atom = atoms.get("unit")
    if unit_atom is None:
        return core
    unit_provenance = {
        **dict(unit_atom.provenance),
        "projection_unit_policy": "ANNUAL_CHANGE_USES_LEVEL_ITEM_UNIT",
        "claim_unit": str(unit_atom.surface or ""),
    }
    atoms["unit"] = ClaimAtom("unit", "", "UNKNOWN", unit_provenance)
    proposal = {**core.proposal_view, "unit": ""}
    data = {
        "atoms": {name: asdict(atom) for name, atom in atoms.items()},
        "proposal_view": proposal,
        "provenance": core.provenance,
        "contract_version": core.contract_version,
    }
    digest = hashlib.sha256(canonical_bytes(data)).hexdigest()
    return ClaimCore(atoms, proposal, core.provenance, core.contract_version, digest)


def _profile_item_unit(profile: Mapping[str, Any] | None, item_id: Any) -> str:
    if not isinstance(profile, Mapping):
        return ""
    expected = str(item_id or "")
    for item in profile.get("items") or ():
        if isinstance(item, Mapping) and str(item.get("itm_id") or "") == expected:
            return str(item.get("unit_nm") or item.get("unit_name") or "")
    return ""


def _evidence_family_key(row: Mapping[str, Any]) -> tuple[str, Any, str, str]:
    fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
    sentence_id = row.get("article_sentence_id", row.get("sentence_id"))
    period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
    measurement = period.get("measurement") if isinstance(period.get("measurement"), Mapping) else {}
    period_absolute = str(measurement.get("absolute") or fields.get("period_absolute") or "")
    family = indicator_family_key(str(fields.get("indicator") or row.get("indicator_label") or ""))
    return (str(row.get("article_idx") or ""), sentence_id, family, period_absolute)


def _evidence_family_groups(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, Any, str, str], tuple[Mapping[str, Any], ...]]:
    groups: dict[tuple[str, Any, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
        if str(fields.get("measurement_type") or "") not in {"LEVEL", "CHANGE_RATE", "CHANGE_POINT"}:
            continue
        groups.setdefault(_evidence_family_key(row), []).append(row)
    return {key: tuple(value) for key, value in groups.items()}


def _annual_range_claim(sentence: str) -> bool:
    """Recognize range/ranking language without assigning a value to it."""
    return bool(re.search(r"(?:\d+년\s*만에|\d{4}년\s*이후|역대\s*\d+번째|\d+년\s*연속)", sentence))


def _evidence_first_ledger_projection(
    top50: Any, *, target_id: str, article_date_limitation: str,
) -> dict[str, Any]:
    """Project monthly-only receipt fields without weakening generic identity."""
    return {
        "target_id": target_id,
        "article_date_limitation": article_date_limitation,
        "profile_receipts": list(getattr(top50, "profile_receipts", ()) or ()),
        "membership_receipt_sha256": getattr(top50, "membership_receipt_sha256", None),
    }


ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H = (
    "기준일은 client가 제공한 값이며 기사 원문에서 확인된 발행일은 아닙니다."
)


def _preprojection_resolution_monthly_v2h(
    error: MonthlyClaimProvenanceError,
) -> TargetResolution:
    data = {
        "outcome": "HOLD",
        "hold_reason": error.code,
        "query_plan": None,
        "chosen_table_key": None,
        "compatible_series": [],
        "audit": {"preprojection": error.data},
    }
    digest = hashlib.sha256(json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    return TargetResolution(**data, canonical_sha256=digest)


def build_article_summaries(
    articles: Sequence[Mapping[str, Any]],
    routed_rows: Sequence[Mapping[str, Any]],
    answers: Sequence[Mapping[str, Any]],
    ledgers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build a deterministic count-only article summary and gate coverage."""
    summaries: list[dict[str, Any]] = []
    for article in articles:
        article_id = str(article.get("article_idx") or "")
        article_rows = [row for row in routed_rows if str(row.get("article_idx") or "") == article_id]
        article_answers = [row for row in answers if str(row.get("article_idx") or "") == article_id]
        article_ledgers = [row for row in ledgers if str(row.get("article_idx") or "") == article_id]
        value_surfaces = [str(row.get("value_text") or "").replace(",", "") for row in article_rows]
        numeric_coverage = []
        body = str(article.get("article_text") or "")
        for match in _NUMBER_TOKEN.finditer(body):
            surface = match.group(0)
            compact = surface.replace(",", "")
            covered = any(compact == value or compact in value or value in compact for value in value_surfaces if value)
            numeric_coverage.append({
                "start": match.start(),
                "end": match.end(),
                "surface": surface,
                "disposition": "L2_VALUE_CANDIDATE" if covered else "L2_NOT_EXTRACTED",
            })
        gate_dispositions = [
            {
                "target_id": _target_id(row),
                "value_span_id": row.get("value_span_id"),
                "value_text": row.get("value_text"),
                "routing_class": row.get("routing_class"),
                "reason": row.get("reason"),
                "confidence": row.get("confidence"),
            }
            for row in article_rows
        ]
        verdict_counts = Counter(str(row.get("verdict") or "UNKNOWN") for row in article_answers)
        blocker_counts: Counter[str] = Counter()
        for ledger in article_ledgers:
            resolution = ledger.get("resolution")
            if isinstance(resolution, str):
                blocker_counts[resolution] += 1
            elif isinstance(resolution, Mapping) and resolution.get("outcome") != "QUERY_READY":
                blocker_counts[str(resolution.get("hold_reason") or resolution.get("outcome") or "HOLD")] += 1
        summaries.append({
            "article_idx": article_id,
            "title": article.get("title"),
            "date": article.get("date"),
            "article_text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "l2_value_candidates": len(article_rows),
            "gate_dispositions": gate_dispositions,
            "numeric_surface_coverage": numeric_coverage,
            "numeric_surface_dispositions_complete": all(row.get("disposition") for row in numeric_coverage),
            "claim_answer_count": len(article_answers),
            "verdict_counts": dict(sorted(verdict_counts.items())),
            "blocker_counts": dict(sorted(blocker_counts.items())),
            "article_level_verdict": None,
            "article_level_rule": "COUNT_ONLY_NO_ARTICLE_TRUTH_INFERENCE",
        })
    return summaries


def _official_unit_for_query(profile: Mapping[str, Any], query_plan: Mapping[str, Any]) -> str:
    selected_ids = {str(value) for value in (query_plan.get("obj_levels") or {}).values()}
    dimension_units = [
        str(value.get("unit_nm") or "")
        for dimension in profile.get("dimensions") or []
        for value in dimension.get("values") or []
        if str(value.get("value_id") or "") in selected_ids and str(value.get("unit_nm") or "")
    ]
    if len(set(dimension_units)) == 1:
        return dimension_units[0]
    item_units = [
        str(item.get("unit_nm") or "")
        for item in profile.get("items") or []
        if str(item.get("itm_id") or "") == str(query_plan.get("itm_id") or "") and str(item.get("unit_nm") or "")
    ]
    return item_units[0] if len(set(item_units)) == 1 else ""


def run_technical_canary(
    canary: Mapping[str, Any],
    *,
    profile_provider: Callable[[str], Mapping[str, Any] | None],
    cell_fetcher: Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
    hcx_answerer: Any | None,
) -> dict[str, Any]:
    """Exercise the sealed metadata-to-cell spine outside performance metrics."""
    table_key = str(canary.get("table_key") or "")
    query_plan = dict(canary.get("query_plan") or {})
    claim_row = dict(canary.get("claim_row") or {})
    profile = profile_provider(table_key)
    if profile is None:
        return {"namespace": "TECHNICAL_CANARY", "status": "UNVERIFIABLE", "reason": "PROFILE_UNAVAILABLE"}
    core = build_claim_core_v2(claim_row)
    inventory_errors = validate_query_plan_inventory(query_plan, profile, core)
    if inventory_errors:
        return {
            "namespace": "TECHNICAL_CANARY",
            "status": "UNVERIFIABLE",
            "reason": "QUERY_PLAN_INVENTORY_INVALID",
            "inventory_errors": inventory_errors,
        }
    cell_result = fetch_exact_single_cell(query_plan, cell_fetcher)
    official_unit = _official_unit_for_query(profile, query_plan)
    comparison: dict[str, Any] = {}
    if cell_result.get("status") == "CELL_RESOLVED":
        comparison = compare_official_cell(
            str(claim_row.get("value_text") or ""),
            str(claim_row.get("value_unit") or ""),
            cell_result["cell"],
            official_unit,
        )
        verdict = str(comparison.get("verdict") or "UNVERIFIABLE")
        reason = str(comparison.get("reason") or "")
    else:
        verdict = "UNVERIFIABLE"
        reason = str(cell_result.get("status") or "CELL_UNAVAILABLE")
    packet = build_evidence_packet(
        verdict=verdict,
        claim_source={"sentence": claim_row.get("sentence_text"), "value": claim_row.get("value_text")},
        binding_plan={"table_key": table_key, "query_plan": query_plan, "inventory_errors": inventory_errors},
        official_cell=cell_result,
        comparison=comparison,
        limitation={} if verdict != "UNVERIFIABLE" else {"reason": reason},
        placeholders={
            "CLAIM": str(claim_row.get("sentence_text") or "기술 canary 주장"),
            "OFFICIAL_VALUE": str((cell_result.get("cell") or {}).get("DT") or ""),
            "LIMITATION": reason,
        },
    )
    answer = generate_guarded_answer(packet, hcx_answerer)
    return {
        "namespace": "TECHNICAL_CANARY",
        "metric_inclusion": False,
        "status": "COMPLETE" if verdict in {"VERIFIED", "REFUTED"} else "UNVERIFIABLE",
        "verdict": verdict,
        "reason": reason,
        "table_key": table_key,
        "profile_sha256": profile.get("profile_sha256"),
        "query_plan": query_plan,
        "inventory_validation": {"status": "VALID", "errors": []},
        "official_unit": official_unit,
        "cell": cell_result,
        "comparison": comparison,
        "answer": answer,
    }


def run_new_articles_v2(
    articles: list[dict[str, Any]],
    *,
    l2_api_key: str,
    search_channels: Mapping[str, Callable[..., Any]],
    release_sha256_by_channel: Mapping[str, str],
    catalog_records: Sequence[Mapping[str, Any]],
    reranker: Any,
    profile_provider: Callable[[str], Mapping[str, Any] | None],
    cell_fetcher: Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]],
    hcx_answerer: Any | None,
    candidate_record_provider: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]] | None = None,
    rag_reasoner: Any | None = None,
    use_rag: bool = False,
    l2_runner: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = run_hcx_l2,
    stack_runner: Callable[[list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]] = run_stack,
    budget_ledger: HttpAttemptBudgetLedger | None = None,
    budget_run_id: str | None = None,
    _audit_barrier_child: bool = False,
    forced_budget_phase: str | None = None,
    precomputed_l2: Mapping[str, Any] | None = None,
    precomputed_routed: Mapping[str, Any] | None = None,
    role_aware_dimension_shadow: bool = False,
    claim_query: str | None = None,
    failure_recovery_shadow: bool = False,
    user_intent_shadow: bool = False,
    evidence_first_statistics_shadow: bool | None = None,
    release_bound_mode: bool | None = None,
) -> dict[str, Any]:
    """Execute the complete live contract with injectable external adapters."""
    evidence_first_statistics_shadow = _evidence_first_enabled(evidence_first_statistics_shadow)
    release_bound_mode = release_runtime_enabled() if release_bound_mode is None else bool(release_bound_mode)
    if (precomputed_l2 is None) != (precomputed_routed is None):
        raise OperationalPipelineError("PRECOMPUTED_INPUT_INCOMPLETE")
    if precomputed_l2 is not None and precomputed_routed is not None:
        if not isinstance(precomputed_l2, Mapping) or not isinstance(precomputed_routed, Mapping):
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        expected_ids = [str(article.get("article_idx") or "") for article in articles]
        l2_prov = precomputed_l2.get("provenance")
        routed_prov = precomputed_routed.get("provenance")
        required_l2_prov = {"stage_manifest_path", "stage_manifest_sha256", "article_input_sha256", "ordered_article_ids", "article_body_sha256", "splitter_mode", "splitter_source_sha256", "predictions", "results"}
        required_routed_prov = {"stage_manifest_path", "stage_manifest_sha256", "predecessor_manifest_sha256", "article_input_sha256", "ordered_article_ids", "article_body_sha256", "splitter_mode", "splitter_source_sha256", "routed"}
        if not isinstance(l2_prov, Mapping) or not required_l2_prov.issubset(l2_prov) or not isinstance(routed_prov, Mapping) or not required_routed_prov.issubset(routed_prov):
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        from src.news_verification.runtime.article_body_sentence_splitter_v1 import SPLITTER_MODE, splitter_source_sha256
        body_sha = {str(article.get("article_idx") or ""): hashlib.sha256(str(article.get("article_text") or "").encode("utf-8")).hexdigest() for article in articles}
        for provenance in (l2_prov, routed_prov):
            if list(provenance.get("ordered_article_ids") or []) != expected_ids or dict(provenance.get("article_body_sha256") or {}) != body_sha or provenance.get("splitter_mode") != SPLITTER_MODE or provenance.get("splitter_source_sha256") != splitter_source_sha256():
                raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        if str(routed_prov.get("predecessor_manifest_sha256") or "") != str(l2_prov.get("stage_manifest_sha256") or ""):
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        results = list(precomputed_l2.get("results") or [])
        result_ids = [str(row.get("article_idx") or "") for row in results]
        if result_ids != expected_ids:
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        ready_ids = {str(row.get("article_idx") or "") for row in results if row.get("status") == "L2_READY"}
        routed_rows = list(precomputed_routed.get("rows") or [])
        routed_article_ids = [str(row.get("article_idx") or "") for row in routed_rows]
        routed_target_ids = [str(row.get("target_id") or row.get("value_span_id") or "") for row in routed_rows]
        if any(not str(row.get("target_id") or row.get("value_span_id") or "").strip() for row in routed_rows):
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        if len(routed_target_ids) != len(set(routed_target_ids)):
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        if not set(routed_article_ids).issubset(ready_ids):
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
    if budget_ledger is not None and not _audit_barrier_child and len(articles) > 1:
        run_id = budget_run_id or "audit"
        budget_ledger.set_phase(run_id, "pilot")
        metadata_before = budget_ledger.used("metadata")
        pilot = run_new_articles_v2(
            articles[:1], l2_api_key=l2_api_key, search_channels=search_channels,
            release_sha256_by_channel=release_sha256_by_channel, catalog_records=catalog_records,
            reranker=reranker, profile_provider=profile_provider, cell_fetcher=cell_fetcher,
            hcx_answerer=hcx_answerer, candidate_record_provider=candidate_record_provider,
            rag_reasoner=rag_reasoner, use_rag=use_rag, l2_runner=l2_runner,
            stack_runner=stack_runner, budget_ledger=budget_ledger, budget_run_id=run_id,
            _audit_barrier_child=True, precomputed_l2=precomputed_l2, precomputed_routed=precomputed_routed,
            role_aware_dimension_shadow=role_aware_dimension_shadow, claim_query=claim_query,
            failure_recovery_shadow=failure_recovery_shadow,
            user_intent_shadow=user_intent_shadow,
            evidence_first_statistics_shadow=evidence_first_statistics_shadow,
            release_bound_mode=release_bound_mode,
        )
        if len(pilot.get("stage_ledger") or []) == 0 or len(pilot.get("answers") or []) == 0:
            raise OperationalPipelineError("AUDIT_PILOT_BARRIER_PARTITION_MISSING")
        try:
            _, pilot_partition = build_terminal_partition(articles[:1], pilot.get("stage_ledger") or [], run_id=run_id)
        except Exception as exc:
            raise OperationalPipelineError("AUDIT_PILOT_BARRIER_PARTITION_INVALID") from exc
        if int(pilot_partition.get("articles") or 0) != 1:
            raise OperationalPipelineError("AUDIT_PILOT_BARRIER_PARTITION_MISSING")
        if budget_ledger.used("metadata") - metadata_before > int(budget_ledger.pilot_metadata_limit):
            raise OperationalPipelineError("AUDIT_PILOT_BARRIER_METADATA_LIMIT")
        budget_ledger.set_phase(run_id, "batch")
        batch = run_new_articles_v2(
            articles[1:], l2_api_key=l2_api_key, search_channels=search_channels,
            release_sha256_by_channel=release_sha256_by_channel, catalog_records=catalog_records,
            reranker=reranker, profile_provider=profile_provider, cell_fetcher=cell_fetcher,
            hcx_answerer=hcx_answerer, candidate_record_provider=candidate_record_provider,
            rag_reasoner=rag_reasoner, use_rag=use_rag, l2_runner=l2_runner,
            stack_runner=stack_runner, budget_ledger=budget_ledger, budget_run_id=run_id,
            _audit_barrier_child=True, precomputed_l2=precomputed_l2, precomputed_routed=precomputed_routed,
            role_aware_dimension_shadow=role_aware_dimension_shadow, claim_query=claim_query,
            failure_recovery_shadow=failure_recovery_shadow,
            user_intent_shadow=user_intent_shadow,
            evidence_first_statistics_shadow=evidence_first_statistics_shadow,
            release_bound_mode=release_bound_mode,
        )
        pilot_manifest, batch_manifest = pilot.get("l2", {}), batch.get("l2", {})
        merged_l2 = {**pilot_manifest, **batch_manifest,
                     "errors": list(pilot_manifest.get("errors") or []) + list(batch_manifest.get("errors") or []),
                     "article_runs": list(pilot_manifest.get("article_runs") or []) + list(batch_manifest.get("article_runs") or [])}
        return {
            **pilot,
            "articles": len(articles), "l2": merged_l2,
            "routed_targets": int(pilot.get("routed_targets") or 0) + int(batch.get("routed_targets") or 0),
            "answers": list(pilot.get("answers") or []) + list(batch.get("answers") or []),
            "stage_ledger": list(pilot.get("stage_ledger") or []) + list(batch.get("stage_ledger") or []),
            "article_summaries": list(pilot.get("article_summaries") or []) + list(batch.get("article_summaries") or []),
            "evidence_answers": list(pilot.get("evidence_answers") or []) + list(batch.get("evidence_answers") or []),
            "evidence_first_statistics_shadow": evidence_first_statistics_shadow,
        }
    def answer_for(packet: Any, target_id: str, *, rag: Any | None = None, use_rag: bool = False) -> dict[str, Any]:
        answerer = hcx_answerer
        if budget_ledger is not None and answerer is not None:
            from src.news_verification.runtime.audit_budget_v1 import BudgetedAnswerer
            answerer = BudgetedAnswerer(answerer, budget_ledger, budget_run_id or "audit", target_id)
        return generate_guarded_answer(packet, answerer, rag=rag, use_rag=use_rag)

    if precomputed_l2 is None:
        l2 = run_operational_l2(articles, api_key=l2_api_key, runner=l2_runner, budget_ledger=budget_ledger, budget_run_id=budget_run_id)
    else:
        l2 = dict(precomputed_l2)
        l2["external_model_calls"] = 0
        l2["reused_model_calls"] = int(l2.get("reused_model_calls") or len(articles))
    ready_ids = {row["article_idx"] for row in l2["results"] if row["status"] == "L2_READY"}
    predictions = [row for result in l2["results"] for row in result["predictions"]]
    if precomputed_routed is None:
        routed = stack_runner([article for article in articles if str(article.get("article_idx")) in ready_ids], predictions)
        stack_recomputed = True
    else:
        routed = list(precomputed_routed.get("rows") or [])
        stack_recomputed = False
    # Keep the stack result as an immutable evidence-only snapshot.  Article
    # mode (claim_query is None) executes every routed target; primary
    # selection is evidence-synthesis audit only.  Explicit query mode may
    # still narrow membership below.
    routed_evidence_snapshot = (
        tuple(MappingProxyType(dict(row)) for row in routed if isinstance(row, Mapping))
        if evidence_first_statistics_shadow else ()
    )
    evidence_family_groups = _evidence_family_groups(routed_evidence_snapshot)
    primary_info: dict[str, Any] | None = None
    primary_selection_error: str | None = None
    if evidence_first_statistics_shadow:
        try:
            primary_info = select_primary_target(routed_evidence_snapshot)
        except SameSeriesEvidenceError as exc:
            primary_selection_error = exc.code
    query_selection: dict[str, Any] | None = None
    user_intent: dict[str, Any] | None = None
    intent_clarification = False
    if claim_query is not None:
        if user_intent_shadow:
            user_intent = route_user_intent(claim_query, routed)
            if user_intent["status"] == "CLARIFICATION_REQUIRED":
                routed = []
                query_selection = {
                    "status": "CLARIFICATION_REQUIRED",
                    "query": claim_query,
                    "user_intent_sha256": user_intent["sha256"],
                    "value_used": False,
                }
                intent_clarification = True
            else:
                routed, query_selection = select_query_target(routed, claim_query, user_intent)
        else:
            routed, query_selection = select_query_target(routed, claim_query)
        if not routed and not intent_clarification:
            raise OperationalPipelineError("CLAIM_QUERY_TARGET_NOT_FOUND")
    if evidence_first_statistics_shadow:
        claim_query_selection = dict(query_selection or {})
        claim_query_target_id = str(claim_query_selection.get("target_id") or "")
        if not claim_query_target_id and len(routed) == 1:
            claim_query_target_id = str(
                routed[0].get("target_id") or routed[0].get("value_span_id") or ""
            )
        primary_audit = {
            "claim_query": claim_query,
            "claim_query_target_id": claim_query_target_id or None,
            "claim_query_selection": claim_query_selection if claim_query is not None else None,
            "user_intent": user_intent,
            "primary_selection_source": "value_span_id_same_series_group",
        }
        if claim_query is None:
            routed = [dict(row) for row in routed_evidence_snapshot]
            query_selection = {
                **claim_query_selection,
                **primary_audit,
                "status": "FAMILY_TARGETS_PRESERVED",
                "value_used": False,
                "family_count": len(evidence_family_groups),
                "family_keys": [list(key) for key in sorted(evidence_family_groups, key=str)],
                "target_ids": [_target_id(row) for row in routed_evidence_snapshot],
            }
        elif intent_clarification:
            query_selection = {**claim_query_selection, **primary_audit}
        elif primary_info is None:
            query_fallback_selected = (
                claim_query is not None
                and len(routed) == 1
                and claim_query_selection.get("status") == "SELECTED"
            )
            if query_fallback_selected:
                selected = dict(routed[0])
                selected_value_span_id = str(selected.get("value_span_id") or "")
                query_selection = {
                    **claim_query_selection,
                    **primary_audit,
                    "status": "PRIMARY_SELECTED_BY_QUERY_FALLBACK",
                    "value_used": False,
                    "target_id": selected_value_span_id or claim_query_selection.get("target_id"),
                    "value_span_id": selected_value_span_id or claim_query_selection.get("value_span_id"),
                    "primary_target_id": _target_id(selected),
                    "primary_error": primary_selection_error or "PRIMARY_TARGET_AMBIGUOUS",
                }
            else:
                routed = []
                query_selection = {
                    **claim_query_selection,
                    **primary_audit,
                    "status": "PRIMARY_TARGET_NOT_EVALUATED",
                    "reason": primary_selection_error or "PRIMARY_TARGET_AMBIGUOUS",
                    "target_id": None,
                    "value_span_id": None,
                    "primary_target_id": None,
                }
        else:
            primary = primary_info["primary"]
            primary_value_span_id = str(primary.get("value_span_id") or "")
            primary_target_id = _target_id(primary)
            routed = [dict(primary)]
            query_selection = {
                **claim_query_selection,
                **primary_audit,
                "status": "PRIMARY_SELECTED",
                "target_id": primary_value_span_id,
                "value_span_id": primary_value_span_id,
                "primary_target_id": primary_target_id,
                "primary_group_key": [str(value) for value in primary_info["group_key"]],
                "same_series_value_span_ids": list(primary_info["value_span_ids"]),
            }
    article_index = {str(article.get("article_idx")): article for article in articles}
    if forced_budget_phase not in (None, "pilot", "batch"):
        raise OperationalPipelineError("AUDIT_BUDGET_PHASE_INVALID")
    pilot_article_id = str(articles[0].get("article_idx")) if articles and budget_ledger is not None and forced_budget_phase is None else None
    def set_article_phase(article_id: str) -> None:
        if budget_ledger is not None and hasattr(budget_ledger, "set_phase"):
            budget_ledger.set_phase(budget_run_id or "audit", forced_budget_phase or ("pilot" if article_id == pilot_article_id else "batch"))
    ledgers: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    represented_articles: set[str] = set()
    if intent_clarification and user_intent is not None:
        prompts = [str(question.get("prompt") or "") for question in user_intent.get("questions") or []]
        for article_id in sorted(ready_ids):
            explanation = " ".join(value for value in prompts if value) or "질의를 처리하려면 통계 조건을 더 알려주세요."
            answer = {
                "article_idx": article_id, "target_id": f"intent:{article_id}",
                "verdict": "CLARIFICATION_REQUIRED", "headline": "추가 정보가 필요합니다.",
                "explanation": explanation, "limitation": "USER_INTENT_CLARIFICATION_REQUIRED",
                "questions": list(user_intent.get("questions") or []),
                "user_intent": user_intent, "fallback": False,
            }
            answers.append(answer)
            ledgers.append({
                "article_idx": article_id, "target_id": f"intent:{article_id}",
                "resolution": "USER_INTENT_CLARIFICATION_REQUIRED",
                "user_intent_shadow": user_intent, "answer": answer,
            })
            represented_articles.add(article_id)
    for l2_result in l2["results"]:
        if l2_result["status"] == "L2_READY":
            continue
        article_id = str(l2_result["article_idx"])
        set_article_phase(article_id)
        article = article_index[article_id]
        packet = build_evidence_packet(
            verdict="UNVERIFIABLE",
            claim_source={"article_text_sha256": hashlib.sha256(str(article.get("article_text") or "").encode("utf-8")).hexdigest()},
            binding_plan={}, official_cell={}, comparison={}, limitation={"reason": "L2_UNAVAILABLE"},
            placeholders={"CLAIM": str(article.get("title") or "해당 기사"), "LIMITATION": "주장 구조를 원문 span과 함께 확정하지 못했습니다."},
        )
        answer = {"article_idx": article_id, "target_id": f"article:{article_id}", **answer_for(packet, f"article:{article_id}", rag=rag_reasoner, use_rag=use_rag)}
        answers.append(answer)
        ledgers.append({"article_idx": article_id, "resolution": "L2_UNAVAILABLE", "answer": answer})
        represented_articles.add(article_id)
    def append_target_ledger(
        payload: Mapping[str, Any],
        *,
        target_call_snapshot: Mapping[str, Any],
        candidate_membership: Sequence[Any] = (),
        retrieval_audit: Mapping[str, Any] | None = None,
    ) -> None:
        """Attach bounded per-target call receipts to every routed ledger."""
        ledger = dict(payload)
        membership = []
        for value in candidate_membership:
            table_key = getattr(value, "table_key", None)
            if table_key is None and isinstance(value, Mapping):
                table_key = value.get("table_key")
            text = str(table_key or "").strip()
            if text:
                membership.append(text)
        audit = dict(retrieval_audit) if isinstance(retrieval_audit, Mapping) else {}
        deltas = _target_call_deltas(target_call_snapshot, search_channels, profile_provider)
        audit.update(deltas["retrieval"])
        audit["candidate_membership"] = membership
        ledger["retrieval"] = audit
        ledger["candidate_membership"] = membership
        ledger["metadata_api_calls"] = deltas["metadata_api_calls"]
        ledger["metadata_lookups"] = deltas["metadata_lookups"]
        ledgers.append(ledger)

    for row in routed:
        target_call_snapshot = _snapshot_target_call_counts(search_channels, profile_provider)
        article_id = str(row.get("article_idx"))
        set_article_phase(article_id)
        target_id = f"{article_id}:{row.get('value_span_id') or row.get('sentence_id') or 'target'}"
        represented_articles.add(article_id)
        article = article_index[article_id]
        sentence = str(row.get("sentence_text") or "")
        article_text = str(article.get("article_text") or "")
        date = str(article.get("date") or article.get("article_date") or "")
        row = _merge_user_clarifications(row, article)
        effective_date = str(row.get("article_date") or date)
        if sentence and sentence in article_text and effective_date:
            row["article_date"] = effective_date
            if evidence_first_statistics_shadow:
                row["article_text"] = article_text
                input_date_receipt = article.get("article_date_provenance")
                row["article_date_provenance"] = (
                    dict(input_date_receipt) if isinstance(input_date_receipt, Mapping) else {}
                )
                row["sentences"] = {
                    item["sentence_id"]: item["text"]
                    for item in sentence_offset_map(article_text)
                }
                if _uses_monthly_claim_contract(row):
                    row = attach_indicator_evidence_monthly_v2h(row, article_text)
            else:
                row["article_date_provenance"] = _bounded_article_date_provenance(article, article_text)
        routing_class = str(row.get("routing_class") or "")
        if routing_class and routing_class != "KOSIS_CANDIDATE":
            gate_reason = f"L5_{routing_class}:{row.get('reason') or 'UNSPECIFIED'}"
            packet = build_evidence_packet(
                verdict="UNVERIFIABLE",
                claim_source={"sentence": sentence, "value": row.get("value_text")},
                binding_plan={}, official_cell={}, comparison={},
                limitation={"reason": gate_reason},
                placeholders={"CLAIM": sentence, "LIMITATION": gate_reason},
            )
            answer = {
                "article_idx": article_id,
                "target_id": target_id,
                **answer_for(packet, target_id),
            }
            answers.append(answer)
            append_target_ledger({
                "article_idx": article_id,
                "value_span_id": row.get("value_span_id"),
                "l1_l5": {
                    "routing_class": routing_class,
                    "confidence": row.get("confidence"),
                    "reason": row.get("reason"),
                },
                "resolution": gate_reason,
                "user_intent_shadow": user_intent,
                "answer": answer,
            }, target_call_snapshot=target_call_snapshot)
            continue
        monthly_core = None
        monthly_contract = evidence_first_statistics_shadow and _uses_monthly_claim_contract(row)
        if release_bound_mode and not monthly_contract:
            row = _release_bound_annual_period_row(row)
        if monthly_contract:
            try:
                monthly_core = build_claim_core_monthly_v2h(row)
            except MonthlyClaimProvenanceError as exc:
                preprojection_resolution = _preprojection_resolution_monthly_v2h(exc)
                packet = build_evidence_packet(
                    verdict="UNVERIFIABLE",
                    claim_source={"sentence": sentence, "value": row.get("value_text")},
                    binding_plan={}, official_cell={}, comparison={},
                    limitation={
                        "reason": exc.code,
                        "article_date": ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H,
                    },
                    placeholders={"CLAIM": sentence, "LIMITATION": exc.code},
                )
                answer = {
                    "article_idx": article_id, "target_id": target_id,
                    **answer_for(packet, target_id),
                }
                answers.append(answer)
                append_target_ledger({
                    "article_idx": article_id,
                    "target_id": target_id,
                    "value_span_id": row.get("value_span_id"),
                    "resolution": asdict(preprojection_resolution),
                    "article_date_limitation": ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H,
                    "answer": answer,
                }, target_call_snapshot=target_call_snapshot)
                continue
        retrieval_fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
        claim_query = {
            "indicator": retrieval_fields.get("indicator") or "",
            "item": retrieval_fields.get("indicator") or "",
            "sentence": sentence,
        }
        source_terms: tuple[dict[str, str], ...] = ()
        if role_aware_dimension_shadow:
            source_id, source_text = _preserve_role_aware_sentence_inventory(
                row, article_text, sentence,
            )
            source_terms = extract_source_terms(source_text)
            claim_query["source_terms"] = source_terms
            period_surface = str(
                retrieval_fields.get("period_absolute") or row.get("period_raw") or ""
            ).strip()
            if (
                source_text
                and source_id is not None
                and period_surface
                and source_text.count(period_surface) == 1
            ):
                row["period_sentence_id"] = source_id
        failure_recovery: dict[str, Any] = {
            "contract_version": "failure-recovery-shadow-v1", "action": "DISABLED",
            "retry_budget": {"used": 0, "limit": 1},
        }
        try:
            candidates, retrieval_audit = retrieve_parallel(
                claim_query,
                search_channels,
                release_sha256_by_channel=release_sha256_by_channel,
                path_top_k=20,
                union_top_k=100,
            )
        except RuntimeError as exc:
            retrieval_code = _bounded_exception_code(exc, "RETRIEVAL_UNAVAILABLE", {
                "KOSIS_SEARCH_UNAVAILABLE", "KOSIS_SEARCH_INVALID_RESPONSE", "V6_BM25_EMPTY_QUERY",
                "V6_BM25_UNAVAILABLE", "V6_BM25_QUERY_FAILED", "V6_QUERY_VECTOR_DIMENSION_MISMATCH",
                "V6_DENSE_QUERY_FAILED", "OPENSEARCH_UNAVAILABLE", "OPENSEARCH_MAPPING_INVALID",
                "OPENSEARCH_INDEX_NOT_CONCRETE", "OPENSEARCH_INDEX_CONFIG_MISMATCH",
                "OPENSEARCH_FIELD_INVALID", "QDRANT_UNAVAILABLE", "QDRANT_QUERY_API_UNAVAILABLE",
                "QDRANT_GROUP_QUERY_UNAVAILABLE", "QDRANT_VECTOR_INVALID", "QDRANT_PAYLOAD_CONTRACT_MISMATCH",
                "QDRANT_COLLECTION_CONTRACT_MISMATCH", "QDRANT_COLLECTION_NOT_CONCRETE",
                "KOSIS_RELEASE_MISMATCH", "CROSS_STORE_RELEASE_MISMATCH", "METADATA_PROFILE_INCOMPLETE",
                "METADATA_PROFILE_CONTRACT_MISMATCH", "METADATA_PROFILE_READ_FAILED",
                "QUERY_ENCODER_UNAVAILABLE", "QUERY_ENCODER_CONTRACT_MISMATCH",
            })
            retrieval_failure = safe_adapter_failure(retrieval_code, exc)
            reason = retrieval_failure["error_code"]
            packet = build_evidence_packet(
                verdict="UNVERIFIABLE", claim_source={"sentence": sentence}, binding_plan={},
                official_cell={}, comparison={}, limitation={"reason": reason},
                placeholders={"CLAIM": sentence, "LIMITATION": reason},
            )
            answer = {"article_idx": article_id, "target_id": target_id, **answer_for(packet, target_id)}
            answers.append(answer)
            append_target_ledger({
                "article_idx": article_id, "value_span_id": row.get("value_span_id"),
                "resolution": reason, "failure": retrieval_failure,
                "user_intent_shadow": user_intent, "answer": answer,
            }, target_call_snapshot=target_call_snapshot)
            continue
        if not candidates and failure_recovery_shadow:
            failure_recovery = plan_failure_recovery(row, None)
            if failure_recovery.get("action") == "CORRECTIVE_RETRIEVAL":
                try:
                    correction_query = corrective_claim_query(claim_query, failure_recovery)
                    round1, round1_audit = retrieve_parallel(
                        correction_query,
                        search_channels,
                        release_sha256_by_channel=release_sha256_by_channel,
                        path_top_k=20,
                        union_top_k=100,
                    )
                    candidates = merge_candidate_rounds(candidates, round1, limit=100)
                    failure_recovery = {
                        **failure_recovery,
                        "retry_budget": {"used": 1, "limit": 1},
                        "round1_retrieval": round1_audit,
                        "round1_candidate_membership": [candidate.table_key for candidate in round1],
                        "union_candidate_membership": [candidate.table_key for candidate in candidates],
                        "recovered_candidate_membership": bool(candidates),
                    }
                except Exception as exc:
                    failure_recovery = {
                        **failure_recovery,
                        "retry_budget": {"used": 1, "limit": 1},
                        "recovered_candidate_membership": False,
                        "correction_failure": {
                            "error_code": "CORRECTIVE_RETRIEVAL_FAILED",
                            "exception_type": type(exc).__name__,
                        },
                    }
        if not candidates:
            packet = build_evidence_packet(
                verdict="UNVERIFIABLE", claim_source={"sentence": sentence}, binding_plan={},
                official_cell={}, comparison={}, limitation={"reason": "NO_CANDIDATES"},
                placeholders={"CLAIM": sentence, "LIMITATION": "검증 가능한 통계표 후보를 찾지 못했습니다."},
            )
            answer = answer_for(packet, target_id, rag=rag_reasoner, use_rag=use_rag)
            answer = {"article_idx": article_id, "target_id": target_id, **answer}
            answers.append(answer)
            append_target_ledger({
                "article_idx": row.get("article_idx"), "value_span_id": row.get("value_span_id"),
                "retrieval": retrieval_audit, "resolution": "NO_CANDIDATES",
                "failure_recovery_shadow": failure_recovery,
                "user_intent_shadow": user_intent, "answer": answer,
            }, target_call_snapshot=target_call_snapshot, candidate_membership=candidates, retrieval_audit=retrieval_audit)
            continue
        if release_bound_mode:
            candidate_records = ()
            passages = ()
        else:
            candidate_records = (
                candidate_record_provider([candidate.table_key for candidate in candidates])
                if candidate_record_provider is not None
                else catalog_records
            )
            passages = build_candidate_passages(candidates, candidate_records)
        try:
            if release_bound_mode:
                rerank_text = ""
                reranked = _rrf_only_order(candidates)
            else:
                rerank_text = (
                    build_role_aware_reranker_query(
                        claim_query["indicator"], source_terms, retrieval_fields.get("period_absolute")
                    )
                    if role_aware_dimension_shadow
                    else str(claim_query["indicator"])
                )
                reranked = rerank_top50(rerank_text, candidates, passages, reranker)
        except RuntimeError as exc:
            reranker_code = _bounded_exception_code(exc, "RERANKER_UNAVAILABLE", {
                "RERANKER_UNAVAILABLE", "RERANKER_CONTRACT_MISMATCH", "RERANKER_CUDA_UNAVAILABLE",
                "RERANKER_INVALID_RESPONSE",
            })
            reranker_failure = safe_adapter_failure(reranker_code, exc)
            reason = reranker_failure["error_code"]
            packet = build_evidence_packet(
                verdict="UNVERIFIABLE", claim_source={"sentence": sentence}, binding_plan={},
                official_cell={}, comparison={}, limitation={"reason": reason},
                placeholders={"CLAIM": sentence, "LIMITATION": reason},
            )
            answer = {"article_idx": article_id, "target_id": target_id, **answer_for(packet, target_id)}
            answers.append(answer)
            append_target_ledger({
                "article_idx": article_id, "value_span_id": row.get("value_span_id"),
                "retrieval": retrieval_audit, "resolution": reason,
                "failure": reranker_failure, "user_intent_shadow": user_intent,
                "answer": answer,
            }, target_call_snapshot=target_call_snapshot, candidate_membership=candidates, retrieval_audit=retrieval_audit)
            continue
        prefetch = getattr(profile_provider, "prefetch", None)
        if callable(prefetch):
            prefetch(candidate.table_key for candidate in reranked)
        core = (
            monthly_core
            if monthly_contract
            else _annual_change_projection_core(row)
            if release_bound_mode
            else build_claim_core_v2(row)
        )
        resolver = resolve_top50_monthly_v2j if monthly_contract else resolve_top50
        resolver_kwargs = {
            "profile_transform": infer_profile_units if role_aware_dimension_shadow else None,
            "allow_unqualified_nationwide": role_aware_dimension_shadow,
            "table_context_terms": [
                term["text"] for term in source_terms if term.get("role") == "report"
            ] if role_aware_dimension_shadow else (),
        }
        if not monthly_contract:
            resolver_kwargs["release_bound_mode"] = release_bound_mode
        top50 = resolver(
            core, reranked, profile_provider,
            **resolver_kwargs,
        )
        if failure_recovery_shadow and failure_recovery["retry_budget"]["used"] == 0:
            failure_recovery = plan_failure_recovery(row, top50)
            if failure_recovery.get("action") == "CORRECTIVE_RETRIEVAL":
                try:
                    correction_query = corrective_claim_query(claim_query, failure_recovery)
                    round1, round1_audit = retrieve_parallel(
                        correction_query,
                        search_channels,
                        release_sha256_by_channel=release_sha256_by_channel,
                        path_top_k=20,
                        union_top_k=100,
                    )
                    union = merge_candidate_rounds(candidates, round1, limit=100)
                    if release_bound_mode:
                        corrected_reranked = _rrf_only_order(union)
                    else:
                        correction_records = (
                            candidate_record_provider([candidate.table_key for candidate in union])
                            if candidate_record_provider is not None else catalog_records
                        )
                        correction_passages = build_candidate_passages(union, correction_records)
                        corrected_reranked = rerank_top50(rerank_text, union, correction_passages, reranker)
                    if callable(prefetch):
                        prefetch(candidate.table_key for candidate in corrected_reranked)
                    corrected_resolver_kwargs = {
                        "profile_transform": infer_profile_units if role_aware_dimension_shadow else None,
                        "allow_unqualified_nationwide": role_aware_dimension_shadow,
                        "table_context_terms": [
                            term["text"] for term in source_terms if term.get("role") == "report"
                        ] if role_aware_dimension_shadow else (),
                    }
                    if not monthly_contract:
                        corrected_resolver_kwargs["release_bound_mode"] = release_bound_mode
                    corrected_top50 = resolver(
                        core,
                        corrected_reranked,
                        profile_provider,
                        **corrected_resolver_kwargs,
                    )
                    recovered = corrected_top50.resolution.outcome == "QUERY_READY"
                    failure_recovery = {
                        **failure_recovery,
                        "retry_budget": {"used": 1, "limit": 1},
                        "round1_retrieval": round1_audit,
                        "round1_candidate_membership": [candidate.table_key for candidate in round1],
                        "union_candidate_membership": [candidate.table_key for candidate in union],
                        "round1_resolution": asdict(corrected_top50.resolution),
                        "recovered": recovered,
                    }
                    if recovered:
                        candidates, reranked, top50 = union, corrected_reranked, corrected_top50
                except Exception as exc:
                    failure_recovery = {
                        **failure_recovery,
                        "retry_budget": {"used": 1, "limit": 1},
                        "recovered": False,
                        "correction_failure": {
                            "error_code": "CORRECTIVE_RETRIEVAL_FAILED",
                            "exception_type": type(exc).__name__,
                        },
                    }
        elif failure_recovery_shadow:
            post_retry = plan_failure_recovery(row, top50)
            failure_recovery = {
                **failure_recovery,
                "post_retry": post_retry,
                "recovered": top50.resolution.outcome == "QUERY_READY",
            }
            if post_retry.get("action") == "ASK_USER":
                failure_recovery["action"] = "ASK_USER_AFTER_CORRECTION"
                failure_recovery["question"] = post_retry["question"]
        assignment = assignment_for_resolution(top50)
        cell_result: dict[str, Any] = {}
        cell_calls_before = getattr(cell_fetcher, "calls", None)
        cell_calls_before = cell_calls_before if isinstance(cell_calls_before, int) else None
        comparison: dict[str, Any] = {}
        inventory_validation: dict[str, Any] = {"status": "NOT_APPLICABLE", "errors": []}
        query_plan = dict(top50.resolution.query_plan or {})
        chosen_profile: Mapping[str, Any] | None = None
        official_unit = ""
        profile_sha256 = ""
        release_id = os.getenv("KOSIS_RELEASE_ID", "")
        annual_requery: dict[str, Any] | None = None
        if top50.resolution.outcome == "QUERY_READY" and assignment is not None:
            chosen_key = str(top50.resolution.chosen_table_key or "")
            # Both generic and monthly resolutions pin the profiles read for
            # projection.  Selection must not reread the backing DB/provider.
            chosen_profile = top50.pinned_raw_profiles.get(chosen_key)
            validation_profile = top50.pinned_projection_profiles.get(chosen_key)
            inventory_errors = (
                validate_query_plan_inventory(top50.resolution.query_plan or {}, validation_profile, core)
                if validation_profile is not None
                else ["PROFILE_UNAVAILABLE"]
            )
            inventory_validation = {
                "status": "VALID" if not inventory_errors else "INVALID",
                "errors": inventory_errors,
                "table_key": top50.resolution.chosen_table_key,
                "profile_sha256": chosen_profile.get("profile_sha256") if chosen_profile else None,
            }
            unit_binding = next((binding for binding in assignment.bindings if binding.axis_kind == "UNIT"), None)
            official_unit = str(unit_binding.evidence.get("profile_label") if unit_binding else "")
            if not official_unit and _annual_change_only_row(row):
                official_unit = _profile_item_unit(validation_profile, query_plan.get("itm_id"))
            profile_sha256 = str(chosen_profile.get("profile_sha256") or "") if chosen_profile else ""
            release_id = str((chosen_profile or {}).get("release_id") or release_id)
            if inventory_errors:
                verdict, reason = "UNVERIFIABLE", "QUERY_PLAN_INVENTORY_INVALID"
            else:
                if budget_ledger is not None:
                    cell_result = budget_ledger.execute(
                        budget_run_id or "audit", "cell",
                        lambda: fetch_exact_single_cell(query_plan, cell_fetcher),
                        target_id=target_id,
                    )
                else:
                    cell_result = fetch_exact_single_cell(query_plan, cell_fetcher)
            if not inventory_errors and cell_result["status"] == "CELL_RESOLVED":
                if _annual_change_only_row(row):
                    # A CHANGE_RATE claim is not a LEVEL claim.  The current
                    # cell is only an endpoint for the annual two-cell
                    # operation below; compare the computed rate there.
                    verdict, reason = "UNVERIFIABLE", "ANNUAL_REQUERY_PENDING"
                    comparison = {}
                else:
                    comparison = compare_official_cell(
                        str(row.get("value_text") or ""), str(row.get("value_unit") or ""),
                        cell_result["cell"], official_unit,
                    )
                    verdict = comparison["verdict"]
                    reason = comparison.get("reason")
            elif not inventory_errors:
                verdict, reason = "UNVERIFIABLE", cell_result["status"]
        else:
            verdict, reason = "UNVERIFIABLE", top50.resolution.hold_reason or "NO_COMPATIBLE_SERIES"
        cell_calls_after = getattr(cell_fetcher, "calls", None)
        cell_calls_after = cell_calls_after if isinstance(cell_calls_after, int) else None
        current_cell_calls = (
            max(0, cell_calls_after - cell_calls_before)
            if cell_calls_before is not None and cell_calls_after is not None
            else 0
        )
        if (
            release_bound_mode
            and os.getenv("ANNUAL_REQUERY_SHADOW_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
            and cell_result.get("status") == "CELL_RESOLVED"
            and assignment is not None
            and _annual_claim_contract(row, query_plan)
        ):
            annual_rows = [
                _release_bound_annual_period_row(candidate_row)
                for candidate_row in routed_evidence_snapshot
                if str(candidate_row.get("article_idx") or "") == article_id
            ] if evidence_first_statistics_shadow else [
                _release_bound_annual_period_row(candidate_row)
                for candidate_row in routed
                if str(candidate_row.get("article_idx") or "") == article_id
            ]
            def annual_cell_fetcher(query: dict[str, Any]) -> list[dict[str, Any]] | dict[str, Any]:
                if budget_ledger is None:
                    return cell_fetcher(query)
                period = str(query.get("start_prd_de") or query.get("end_prd_de") or "")
                return budget_ledger.execute(
                    budget_run_id or "audit", "cell", lambda: cell_fetcher(query),
                    target_id=f"{article_id}:annual:{period}",
                )
            try:
                annual_requery = verify_annual_requery(
                    rows=annual_rows,
                    current_plan=query_plan,
                    current_cell_result=cell_result,
                    current_target_id=str(row.get("value_span_id") or ""),
                    cell_fetcher=annual_cell_fetcher,
                    official_unit=official_unit,
                )
                comparison = {**comparison, "annual_requery": annual_requery}
                verdict = str(annual_requery.get("verdict") or verdict)
                reason = "ANNUAL_REQUERY_EVALUATED"
            except AnnualRequeryError as exc:
                # Annual evidence is additive.  A missing/ambiguous sibling
                # must not turn a resolved current cell into a fabricated claim.
                annual_requery = {
                    "status": "not_evaluated",
                    "reason": _bounded_annual_requery_reason(exc),
                }
        binding_payload = asdict(assignment) if assignment is not None else {}
        nationwide_assumption = bool(
            assignment is not None
            and any(
                isinstance(binding.evidence.get("inference_disclosure"), Mapping)
                and binding.evidence["inference_disclosure"].get("assumption") == "UNQUALIFIED_QUERY_MEANS_NATIONWIDE"
                for binding in assignment.bindings
            )
        )
        limitation_payload: dict[str, Any] = {}
        limitation_text = str(reason or "")
        if evidence_first_statistics_shadow:
            limitation_payload["article_date"] = ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H
            limitation_text = ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H
        if nationwide_assumption:
            limitation_payload["assumption"] = "지역이 명시되지 않아 전국 기준으로 해석했습니다."
            limitation_text = "지역이 명시되지 않아 전국 기준으로 해석했습니다."
        if _annual_range_claim(sentence) and _annual_claim_contract(row, query_plan):
            limitation_payload["annual_range"] = (
                "연간 역사적 범위·순위 주장은 이번 단계에서 지원 범위를 벗어나 별도로 확인하지 않았습니다."
            )
            limitation_text = (
                f"{limitation_text} {limitation_payload['annual_range']}"
            ).strip()
        if verdict == "UNVERIFIABLE":
            limitation_payload["reason"] = reason
        packet = build_evidence_packet(
            verdict=verdict,
            claim_source={"sentence": sentence, "value": row.get("value_text")},
            binding_plan={
                "query_plan": query_plan,
                "assignment": binding_payload,
                "inventory_validation": inventory_validation,
                "user_intent": user_intent,
            },
            official_cell=cell_result,
            comparison=comparison,
            limitation=limitation_payload,
            placeholders={
                "CLAIM": sentence,
                "OFFICIAL_VALUE": str((cell_result.get("cell") or {}).get("DT") or ""),
                "LIMITATION": limitation_text,
            },
        )
        answer = answer_for(packet, target_id, rag=rag_reasoner, use_rag=use_rag)
        answer = {"article_idx": article_id, "target_id": target_id, **answer}
        if annual_requery and annual_requery.get("verdict"):
            answer["evidence_answer"] = {
                "contract_version": annual_requery.get("contract_version"),
                "text": annual_requery.get("answer"),
                "verdict": annual_requery.get("verdict"),
                "annual_requery": annual_requery,
            }
        if user_intent is not None:
            answer["user_intent"] = user_intent
        answers.append(answer)
        execution_ledger = {
            "article_idx": row.get("article_idx"),
            "target_id": target_id,
            "value_span_id": row.get("value_span_id"),
            "retrieval": retrieval_audit,
            "role_aware_shadow": {
                "enabled": role_aware_dimension_shadow,
                "source_terms": list(source_terms),
                "reranker_query": rerank_text,
                "unqualified_region_assumption": "NATIONWIDE" if role_aware_dimension_shadow else None,
            },
            "ranking_mode": RRF_ORDER_RERANKER_DISABLED if release_bound_mode else "MODEL_RERANKER",
            "failure_recovery_shadow": failure_recovery,
            "user_intent_shadow": user_intent or {
                "contract_version": "user-intent-router-shadow-v1", "status": "DISABLED",
            },
            "candidate_membership": list(top50.candidate_membership),
            "reranker_scope": [asdict(candidate) for candidate in reranked],
            "projections": [asdict(projection) for projection in top50.projections],
            "resolution": asdict(top50.resolution),
            "inventory_validation": inventory_validation,
            "cell": cell_result,
            "comparison": comparison,
            "query_plan": query_plan,
            "table_key": top50.resolution.chosen_table_key,
            "official_unit": official_unit,
            "profile_sha256": profile_sha256,
            "release_id": release_id,
            "selected_table": {
                "table_key": top50.resolution.chosen_table_key,
                "send_de": _release_bound_send_de(chosen_profile),
                "release_id": release_id,
                "profile_sha256": profile_sha256,
                "query_plan_sha256": hashlib.sha256(canonical_bytes(query_plan)).hexdigest(),
            },
            "call_ledger": {"cell_api": current_cell_calls},
            "assignment_provenance": binding_payload,
            "answer": answer,
        }
        if annual_requery is not None:
            execution_ledger["annual_requery"] = annual_requery
        if evidence_first_statistics_shadow:
            execution_ledger.update(_evidence_first_ledger_projection(
                top50,
                target_id=target_id,
                article_date_limitation=ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H,
            ))
        append_target_ledger(
            execution_ledger,
            target_call_snapshot=target_call_snapshot,
            candidate_membership=top50.candidate_membership,
            retrieval_audit=retrieval_audit,
        )
    for article_id in sorted(ready_ids - represented_articles):
        set_article_phase(article_id)
        article = article_index[article_id]
        packet = build_evidence_packet(
            verdict="UNVERIFIABLE", claim_source={"title": article.get("title")}, binding_plan={},
            official_cell={}, comparison={}, limitation={"reason": "NO_ROUTED_TARGETS"},
            placeholders={"CLAIM": str(article.get("title") or "해당 기사"), "LIMITATION": "검증 가능한 수치 주장을 추출하지 못했습니다."},
        )
        answer = {"article_idx": article_id, "target_id": f"article:{article_id}", **answer_for(packet, f"article:{article_id}", rag=rag_reasoner, use_rag=use_rag)}
        answers.append(answer)
        ledgers.append({"article_idx": article_id, "resolution": "NO_ROUTED_TARGETS", "answer": answer})
    target_ids_by_span = {
        (str(row.get("article_idx") or ""), str(row.get("value_span_id") or "")): _target_id(row)
        for row in routed
    }
    for ledger in ledgers:
        if ledger.get("target_id"):
            continue
        key = (str(ledger.get("article_idx") or ""), str(ledger.get("value_span_id") or ""))
        target_id = target_ids_by_span.get(key)
        if target_id:
            ledger["target_id"] = target_id
    evidence_results: list[dict[str, Any]] = []
    if evidence_first_statistics_shadow:
        for article in articles:
            article_id = str(article.get("article_idx") or "")
            article_routed = tuple(
                row for row in routed_evidence_snapshot
                if str(row.get("article_idx") or "") == article_id
            )
            article_ledgers = [row for row in ledgers if str(row.get("article_idx") or "") == article_id]
            article_answers = [row for row in answers if str(row.get("article_idx") or "") == article_id]

            def evidence_cell_fetcher(
                query: dict[str, Any],
                *,
                _article_id: str = article_id,
            ) -> list[dict[str, Any]] | dict[str, Any]:
                if budget_ledger is None:
                    return cell_fetcher(query)
                period = str(query.get("start_prd_de") or query.get("end_prd_de") or "")
                return budget_ledger.execute(
                    budget_run_id or "audit", "cell", lambda: cell_fetcher(query),
                    target_id=f"{_article_id}:evidence:{period}",
                )

            family_groups = _evidence_family_groups(article_routed)
            if not family_groups:
                family_groups = {("", None, "", ""): article_routed}
            for family_key, family_rows in family_groups.items():
                evidence_result = synthesize_same_series_evidence(
                    article=article,
                    routed_rows=family_rows,
                    ledgers=article_ledgers,
                    answers=article_answers,
                    cell_fetcher=evidence_cell_fetcher,
                    exact_fetcher=fetch_exact_single_cell,
                    feature_enabled=True,
                    release_id=os.getenv("KOSIS_RELEASE_ID", ""),
                )
                evidence_result["indicator_family_key"] = family_key[2] or None
                evidence_result["article_date_limitation"] = ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H
                evidence_text = str(evidence_result.get("text") or "").strip()
                if evidence_text and ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H not in evidence_text:
                    evidence_result["text"] = (
                        f"{evidence_text} {ARTICLE_DATE_CLIENT_ASSERTION_LIMITATION_V2H}"
                    )
                evidence_results.append(evidence_result)
    l2_output = l2["manifest"]
    if precomputed_l2 is not None:
        l2_output = {
            **dict(l2_output),
            "external_model_calls": 0,
            "reused_model_calls": int(l2.get("reused_model_calls") or len(articles)),
        }
    return {
        "contract_version": CONTRACT_VERSION,
        "articles": len(articles),
        "l2": l2_output,
        "routed_targets": len(routed),
        "answers": answers,
        "stage_ledger": ledgers,
        "article_summaries": build_article_summaries(articles, routed, answers, ledgers),
        "stack_recomputed": stack_recomputed,
        "claim_query_selection": query_selection,
        "role_aware_dimension_shadow": role_aware_dimension_shadow,
        "failure_recovery_shadow": failure_recovery_shadow,
        "user_intent_shadow": user_intent,
        "evidence_first_statistics_shadow": evidence_first_statistics_shadow,
        "evidence_answers": evidence_results,
    }


def run_replay_v2(config_path: str | Path, output: str | Path) -> dict[str, Any]:
    """Re-run the immutable v1 baseline while registering v2 as shadow-only."""
    config = _read_json(Path(config_path))
    report = write_replay(config["assets"]["replay_config"], output)
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": "replay",
        "baseline_report": report,
        "operational_components": {
            "l2": "HCX-007_SHADOW_READY",
            "retrieval": "V6_DENSE_VERIFIED_QDRANT_READY",
            "query_encoder": "GPU_RECEIPT_READY_LAN_SERVICE_REQUIRED",
            "reranker": "GPU_RECEIPT_READY_LAN_SERVICE_REQUIRED",
            "late_binding": "R4-C1_V2_ACTIVE",
            "quantity": "DECIMAL_V1_ACTIVE_FOR_OPERATIONAL_MODE",
            "rag_reasoning": "POST_VERDICT_SHADOW",
            "answer": "HCX-007_GUARDED_WITH_TEMPLATE_FALLBACK",
        },
    }


_STAGE_ALLOWLISTS = {
    "02": ({"02_l2_predictions.jsonl", "02_l2_results.jsonl"}, {"02_trace.log"}, False),
    "03": ({"03_routed.jsonl"}, {"03_trace.log"}, False),
}


def _file_record(path: Path, *, rows: bool = True) -> dict[str, Any]:
    data = path.read_bytes()
    record = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if rows:
        record["rows"] = sum(1 for line in data.decode("utf-8").splitlines() if line.strip())
    return record


def _validate_manifest_maps(manifest: Mapping[str, Any], root: Path, stage: str) -> dict[str, dict[str, Any]]:
    expected_data, expected_logs, runtime_empty = _STAGE_ALLOWLISTS[stage]
    data = manifest.get("data_payloads")
    logs = manifest.get("sealed_logs")
    runtime = manifest.get("runtime_payloads")
    if not isinstance(data, Mapping) or not isinstance(logs, Mapping) or not isinstance(runtime, Mapping):
        raise OperationalPipelineError("MANIFEST_INVALID")
    if set(data) != expected_data or set(logs) != expected_logs or (runtime_empty and runtime):
        raise OperationalPipelineError("MANIFEST_INVALID")
    if set(data) & set(logs) or set(data) & set(runtime) or set(logs) & set(runtime):
        raise OperationalPipelineError("MANIFEST_INVALID")
    records: dict[str, dict[str, Any]] = {}
    for name, item in list(data.items()) + list(logs.items()) + list(runtime.items()):
        if not isinstance(name, str) or Path(name).name != name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise OperationalPipelineError("MANIFEST_INVALID")
        if not isinstance(item, Mapping) or not isinstance(item.get("sha256"), str) or not isinstance(item.get("bytes"), int):
            raise OperationalPipelineError("MANIFEST_INVALID")
        path = (root / name).resolve()
        if root not in path.parents or not path.is_file():
            raise OperationalPipelineError("MANIFEST_INVALID")
        actual = _file_record(path, rows=name.endswith((".jsonl", ".log")))
        if actual["sha256"] != item["sha256"] or actual["bytes"] != item["bytes"]:
            raise OperationalPipelineError("SHA_MISMATCH")
        if "rows" in item and actual.get("rows") != item.get("rows"):
            raise OperationalPipelineError("SHA_MISMATCH")
        records[name] = {**item, "path": str(path)}
    return records


def _discover_precomputed_manifest(manifest_path: Path, *, stage: str, article_path: Path, expected_article_ids: list[str]) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise OperationalPipelineError("PREDECESSOR_MISSING")
    try:
        manifest = _read_json(manifest_path)
    except Exception as exc:
        raise OperationalPipelineError("MANIFEST_INVALID") from exc
    if manifest.get("status") != "COMPLETE" or manifest.get("stage") != stage or not isinstance(manifest.get("contract_version"), str):
        raise OperationalPipelineError("MANIFEST_INVALID")
    records = _validate_manifest_maps(manifest, manifest_path.parent.resolve(), stage)
    article_input = manifest.get("article_input")
    if not isinstance(article_input, Mapping) or article_input.get("path") != str(article_path.resolve()) or article_input.get("sha256") != _sha_file(article_path):
        raise OperationalPipelineError("SHA_MISMATCH")
    if not isinstance(manifest.get("ordered_article_ids"), list) or list(manifest.get("ordered_article_ids")) != expected_article_ids:
        raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
    try:
        source_rows = [json.loads(line) for line in article_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception as exc:
        raise OperationalPipelineError("ARTICLE_INPUT_INVALID") from exc
    expected_body_sha = {str(row.get("article_idx") or ""): hashlib.sha256(str(row.get("article_text") or "").encode("utf-8")).hexdigest() for row in source_rows}
    body_sha = manifest.get("article_body_sha256")
    if not isinstance(body_sha, Mapping) or dict(body_sha) != expected_body_sha:
        raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
    from src.news_verification.runtime.article_body_sentence_splitter_v1 import SPLITTER_MODE, splitter_source_sha256
    if manifest.get("splitter_mode") != SPLITTER_MODE or manifest.get("splitter_source_sha256") != splitter_source_sha256():
        raise OperationalPipelineError("SENTENCE_INVENTORY_MISMATCH")
    data_paths = {name: Path(item["path"]) for name, item in records.items() if name in _STAGE_ALLOWLISTS[stage][0]}
    def read_rows(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if stage == "02":
        predictions = read_rows(data_paths["02_l2_predictions.jsonl"])
        result_rows = read_rows(data_paths["02_l2_results.jsonl"])
        if [str(row.get("article_idx") or "") for row in result_rows] != expected_article_ids:
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        flattened: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        cursor = 0
        for row in result_rows:
            status = row.get("status")
            if status not in {"L2_READY", "L2_UNAVAILABLE"}:
                raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
            if not isinstance(row.get("prediction_row_start"), int) or not isinstance(row.get("prediction_row_end"), int):
                raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
            start, end = row["prediction_row_start"], row["prediction_row_end"]
            if start != cursor or start < 0 or end < start or end > len(predictions):
                raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
            if status == "L2_UNAVAILABLE":
                if start != end:
                    raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
                selected: list[dict[str, Any]] = []
            else:
                selected = predictions[start:end]
                if any(str(item.get("article_idx") or "") != str(row.get("article_idx") or "") for item in selected):
                    raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
                flattened.extend(selected)
            cursor = end
            results.append({"article_idx": str(row.get("article_idx") or ""), "status": status, "predictions": selected})
        if cursor != len(predictions):
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        if flattened != predictions:
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        call_ledger = manifest.get("call_ledger")
        hcx_ledger = call_ledger.get("hcx_l2") if isinstance(call_ledger, Mapping) else None
        if (
            not isinstance(hcx_ledger, Mapping)
            or isinstance(hcx_ledger.get("used"), bool)
            or not isinstance(hcx_ledger.get("used"), int)
            or hcx_ledger["used"] < 0
            or isinstance(hcx_ledger.get("limit"), bool)
            or not isinstance(hcx_ledger.get("limit"), int)
            or hcx_ledger["limit"] != 1
            or hcx_ledger["used"] > hcx_ledger["limit"]
        ):
            raise OperationalPipelineError("MANIFEST_INVALID")
        return {"results": results, "manifest": manifest.get("operational_l2") or manifest, "external_model_calls": 0,
                "reused_model_calls": hcx_ledger["used"],
                "provenance": {"stage_manifest_path": str(manifest_path.resolve()), "stage_manifest_sha256": _sha_file(manifest_path),
                                "article_input_sha256": _sha_file(article_path), "ordered_article_ids": expected_article_ids,
                                "article_body_sha256": body_sha, "splitter_mode": manifest.get("splitter_mode"),
                                "splitter_source_sha256": manifest.get("splitter_source_sha256"),
                                "predictions": records["02_l2_predictions.jsonl"], "results": records["02_l2_results.jsonl"]}}
    rows = read_rows(data_paths["03_routed.jsonl"])
    if any(not str(row.get("article_idx") or "") in set(expected_article_ids) for row in rows):
        raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
    return {"rows": rows, "provenance": {"stage_manifest_path": str(manifest_path.resolve()),
            "stage_manifest_sha256": _sha_file(manifest_path), "article_input_sha256": _sha_file(article_path),
            "ordered_article_ids": expected_article_ids, "article_body_sha256": body_sha,
            "splitter_mode": manifest.get("splitter_mode"), "splitter_source_sha256": manifest.get("splitter_source_sha256"),
            "predecessor_manifest_sha256": str(manifest.get("predecessor_manifest_sha256") or ""),
            "routed": records["03_routed.jsonl"]}}


def run_live_from_files(
    config_path: str | Path,
    article_path: str | Path,
    output_root: str | Path,
    *,
    allow_existing_output: bool = False,
    acquisition_receipt: Mapping[str, Any] | None = None,
    include_technical_canary: bool = True,
    operational_cache_override: str | Path | None = None,
    snapshot_root_override: str | Path | None = None,
    budget_ledger_override: str | Path | None = None,
    audit_run_id: str | None = None,
    defer_manifest_finalization: bool = False,
    service_urls_override: Mapping[str, str] | None = None,
    ignore_env: bool = False,
    audit_phase: str | None = None,
    audit_expected_articles: int = 12,
    precomputed_l2_manifest_path: str | Path | None = None,
    precomputed_routed_manifest_path: str | Path | None = None,
    clarification_context_path: str | Path | None = None,
    pilot_metadata_limit_override: int | None = None,
    role_aware_dimension_shadow: bool = False,
    claim_query: str | None = None,
    profile_seed_override: str | Path | None = None,
    failure_recovery_shadow: bool = False,
    user_intent_shadow: bool = False,
    deterministic_answer_only: bool = False,
) -> dict[str, Any]:
    """Construct every approved live adapter and execute a new article file."""
    config_path = Path(config_path).resolve()
    article_path = Path(article_path).resolve()
    output_root = Path(output_root).resolve()
    if output_root.exists() and not allow_existing_output:
        raise OperationalPipelineError("OUTPUT_EXISTS")
    if (precomputed_l2_manifest_path is None) != (precomputed_routed_manifest_path is None):
        raise OperationalPipelineError("PRECOMPUTED_INPUT_INCOMPLETE")
    if deterministic_answer_only:
        if include_technical_canary:
            raise OperationalPipelineError("DETERMINISTIC_ANSWER_CANARY_ENABLED")
        if precomputed_l2_manifest_path is None or precomputed_routed_manifest_path is None:
            raise OperationalPipelineError("DETERMINISTIC_ANSWER_PRECOMPUTED_REQUIRED")
    # Article and predecessor validation is deliberately before dotenv/service
    # preflight or adapter construction: malformed trace inputs must be zero-call.
    precomputed_l2 = precomputed_routed = None
    local_articles = _load_articles_for_clarification(article_path, clarification_context_path)
    expected_article_ids = [str(row.get("article_idx") or "") for row in local_articles]
    if precomputed_l2_manifest_path is not None:
        precomputed_l2 = _discover_precomputed_manifest(Path(precomputed_l2_manifest_path).resolve(), stage="02", article_path=article_path, expected_article_ids=expected_article_ids)
        precomputed_routed = _discover_precomputed_manifest(Path(precomputed_routed_manifest_path).resolve(), stage="03", article_path=article_path, expected_article_ids=expected_article_ids)
        from src.news_verification.runtime.article_body_sentence_splitter_v1 import SPLITTER_MODE, splitter_source_sha256
        expected_body_sha = {str(row.get("article_idx") or ""): hashlib.sha256(str(row.get("article_text") or "").encode("utf-8")).hexdigest() for row in local_articles}
        for provenance in (precomputed_l2.get("provenance", {}), precomputed_routed.get("provenance", {})):
            if dict(provenance.get("article_body_sha256") or {}) != expected_body_sha or provenance.get("splitter_mode") != SPLITTER_MODE or provenance.get("splitter_source_sha256") != splitter_source_sha256():
                raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
        l2_sha = str(precomputed_l2.get("provenance", {}).get("stage_manifest_sha256") or "")
        predecessor_sha = str(precomputed_routed.get("provenance", {}).get("predecessor_manifest_sha256") or "")
        if predecessor_sha and predecessor_sha != l2_sha:
            raise OperationalPipelineError("PRECOMPUTED_PARTITION_MISMATCH")
    config = _read_json(config_path)
    root = config_path.parent.parent
    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env", override=False)
    except ImportError:
        pass
    release_bound_mode = release_runtime_enabled()
    release_runtime = None
    if release_bound_mode:
        try:
            release_runtime = build_release_bound_runtime()
            gate = release_runtime.preflight()
        except BackendError as exc:
            raise OperationalPipelineError(f"RELEASE_RUNTIME_PREFLIGHT_BLOCKED:{exc.code}") from None
        if service_urls_override is not None or ignore_env:
            raise OperationalPipelineError("RELEASE_RUNTIME_SERVICE_OVERRIDE_UNSUPPORTED")
    else:
        preflight_kwargs = {
            "check_service": True,
            "service_urls_override": service_urls_override,
            "ignore_env": ignore_env,
        }
        if deterministic_answer_only:
            preflight_kwargs["allow_deterministic_answer_only"] = True
        gate = preflight(config_path, **preflight_kwargs)
    if gate["status"] != "READY":
        raise OperationalPipelineError("LIVE_PREFLIGHT_BLOCKED:" + ",".join(gate["blockers"]))
    articles = local_articles
    if audit_phase not in (None, "pilot", "batch"):
        raise OperationalPipelineError("AUDIT_PHASE_INVALID")
    if audit_phase is not None:
        if audit_expected_articles < 2 or audit_expected_articles > 12:
            raise OperationalPipelineError("AUDIT_EXPECTED_ARTICLES_INVALID")
        if not audit_run_id or budget_ledger_override is None or len(articles) != audit_expected_articles:
            raise OperationalPipelineError("AUDIT_PHASE_CONTRACT_REQUIRED")
        articles = articles[:1] if audit_phase == "pilot" else articles[1:]
    output_root.mkdir(parents=True, exist_ok=allow_existing_output)
    service_urls = gate["service_urls"]
    budget_ledger = None
    if budget_ledger_override is not None:
        budget_ledger = HttpAttemptBudgetLedger(
            budget_ledger_override,
            {"metadata": 6000, "hcx_l2": 12, "cell": 100000, "answer": 100000},
            owner_id=f"audit:{audit_run_id or output_root.name}:pid:{os.getpid()}:process:{uuid.uuid4().hex}",
            pilot_metadata_limit=(750 if pilot_metadata_limit_override is None else int(pilot_metadata_limit_override)),
        )
        budget_ledger.startup_recover()
        budget_ledger.set_phase(audit_run_id or output_root.name, audit_phase or "pilot")
    encoder_inner: Any = None
    reranker_inner: Any = None
    if release_bound_mode:
        assert release_runtime is not None
        encoder_call = release_runtime.encoder.encode
        reranker_call = None
    else:
        encoder_inner = HttpQueryEncoderClient(service_urls["query_encoder"])
        reranker_inner = HttpRerankerClient(service_urls["reranker"])
        encoder_call = encoder_inner.encode
        reranker_call = reranker_inner.rerank
    if budget_ledger is not None:
        phase_provider = lambda: budget_ledger.current_phase(audit_run_id or output_root.name)
        encoder_call = BudgetedCallable(budget_ledger, audit_run_id or output_root.name, "metadata", encoder_call, phase_provider=phase_provider)
        if reranker_call is not None:
            reranker_call = BudgetedCallable(budget_ledger, audit_run_id or output_root.name, "metadata", reranker_call, phase_provider=phase_provider)
    encoder = CountingEncoder(encoder_call)
    reranker = None if release_bound_mode else CountingReranker(reranker_call)
    passage_store = None
    seed_cache_path: Path | None = None
    seed_cache_sha_before: str | None = None
    official_inner: Any = OfficialKosisSearchChannel(os.environ["KOSIS_API_KEY"])
    if budget_ledger is not None:
        official_inner = BudgetedCallable(budget_ledger, audit_run_id or output_root.name, "metadata", official_inner, phase_provider=lambda: budget_ledger.current_phase(audit_run_id or output_root.name))
    official = CountingAdapter(official_inner)
    if release_bound_mode:
        assert release_runtime is not None
        release_channels = release_runtime.channels(encoder)
        bm25 = CountingAdapter(release_channels["bm25"])
        dense = CountingAdapter(release_channels["dense"])
        profile_provider = release_runtime.metadata
    else:
        qdrant_client_module = __import__("qdrant_client", fromlist=["QdrantClient"])
        qdrant_timeout = float(config.get("limits", {}).get("qdrant_query_timeout_seconds", 90))
        if not 30 <= qdrant_timeout <= 300:
            raise OperationalPipelineError("QDRANT_QUERY_TIMEOUT_INVALID")
        qdrant = qdrant_client_module.QdrantClient(
            url=service_urls["qdrant"], timeout=qdrant_timeout,
        )
        bm25_index = (root / config["assets"]["v6_bm25_index"]).resolve()
        passage_store = V6CatalogPassageStore(bm25_index)
        bm25 = CountingAdapter(V6Bm25Channel(bm25_index))
        dense = CountingAdapter(V6DenseChannel(
            qdrant,
            config["services"]["qdrant_collection"],
            encoder,
            vector_name=config["services"]["qdrant_vector_name"],
        ))
        seed_cache_path = (
            Path(profile_seed_override).resolve()
            if profile_seed_override is not None
            else (root / config["assets"]["profile_cache"]).resolve()
        )
        if profile_seed_override is not None and not seed_cache_path.is_file():
            raise OperationalPipelineError("PROFILE_CACHE_SEED_UNAVAILABLE")
        seed_cache_sha_before = sha256_file(seed_cache_path)
        operational_cache_path = Path(operational_cache_override).resolve() if operational_cache_override is not None else (root / config["assets"]["operational_profile_cache"]).resolve()
        snapshot_root_path = Path(snapshot_root_override).resolve() if snapshot_root_override is not None else (root / config["assets"]["operational_profile_snapshots"]).resolve()
        profile_provider = OperationalProfileProvider(
            seed_cache_path,
            operational_cache_path,
            snapshot_root_path,
            max_age_seconds=float(config["limits"]["profile_max_age_seconds"]),
            delay_seconds=float(config["limits"]["metadata_delay_seconds"]),
            budget_ledger=budget_ledger,
            budget_run_id=audit_run_id or output_root.name,
            budget_phase="pilot" if budget_ledger is not None else "batch",
        )
    from src.news_verification.runtime.kosis_client import get_data_from_query
    # run_new_articles_v2 applies target-scoped cell/answer reservations at
    # the exact call sites.  Keep these inner transports unwrapped here to
    # avoid double-counting one network attempt.
    cell_fetcher = FailClosedCellFetcher(get_data_from_query)
    answerer = (
        None
        if deterministic_answer_only
        else CountingAnswerer(Hcx007AnswerClient(os.environ["NCP_CLOVASTUDIO_API_KEY"]))
    )
    release_sha = (
        {
            "official": "KOSIS_LIVE",
            "bm25": str(gate["release_binding_sha256"]),
            "dense": str(gate["release_binding_sha256"]),
        }
        if release_bound_mode else
        {
            "official": "KOSIS_LIVE",
            "bm25": str(gate["v6_bm25_manifest"]["index_sha256"]),
            "dense": str(gate["v6_dense_manifest"]["zip_sha256"]),
        }
    )
    result = run_new_articles_v2(
        articles,
        l2_api_key="" if deterministic_answer_only else os.environ["NCP_CLOVASTUDIO_API_KEY"],
        search_channels={"official": official, "bm25": bm25, "dense": dense},
        release_sha256_by_channel=release_sha,
        catalog_records=(),
        candidate_record_provider=None if release_bound_mode else passage_store.records_for_tables,
        reranker=reranker,
        profile_provider=profile_provider,
        cell_fetcher=cell_fetcher,
        hcx_answerer=answerer,
        rag_reasoner=None,
        use_rag=False,
        budget_ledger=budget_ledger,
        budget_run_id=audit_run_id or output_root.name,
        _audit_barrier_child=audit_phase is not None,
        forced_budget_phase=audit_phase,
        precomputed_l2=precomputed_l2,
        precomputed_routed=precomputed_routed,
        role_aware_dimension_shadow=role_aware_dimension_shadow,
        claim_query=claim_query,
        failure_recovery_shadow=failure_recovery_shadow,
        user_intent_shadow=user_intent_shadow,
        release_bound_mode=release_bound_mode,
    )
    article_call_snapshot = {
        "metadata_api": profile_provider.metadata_api_calls,
        "cell_api": cell_fetcher.calls,
        "hcx_answer": 0 if answerer is None else answerer.calls,
    }
    canary_call_delta = {"metadata_api": 0, "cell_api": 0, "hcx_answer": 0}
    canary_manifest: dict[str, Any] = (
        {"enabled": False, "reason": "RELEASE_BOUND_POSTGRES_PROFILE"}
        if release_bound_mode else
        ({"enabled": False} if not include_technical_canary else {})
    )
    if include_technical_canary and not release_bound_mode:
        canary_path = (root / config["assets"]["technical_canary"]).resolve()
        canary_config = _read_json(canary_path)
        technical_canary = run_technical_canary(
            canary_config,
            profile_provider=profile_provider,
            cell_fetcher=cell_fetcher,
            hcx_answerer=answerer,
        )
        result["technical_canary"] = technical_canary
        canary_bytes = (
            json.dumps(technical_canary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
        ).encode("utf-8")
        canary_output = output_root / "technical_canary" / "result.json"
        canary_output.parent.mkdir(parents=True, exist_ok=True)
        if canary_output.exists():
            raise FileExistsError(f"refusing to overwrite technical canary: {canary_output}")
        canary_temporary = canary_output.with_suffix(".json.tmp")
        canary_temporary.write_bytes(canary_bytes)
        os.replace(canary_temporary, canary_output)
        canary_call_delta = {
            "metadata_api": profile_provider.metadata_api_calls - article_call_snapshot["metadata_api"],
            "cell_api": cell_fetcher.calls - article_call_snapshot["cell_api"],
            "hcx_answer": answerer.calls - article_call_snapshot["hcx_answer"],
        }
        canary_manifest = {
            "config_path": str(canary_path),
            "config_sha256": sha256_file(canary_path),
            "result_sha256": hashlib.sha256(canary_bytes).hexdigest(),
            "metric_inclusion": False,
        }
    calls = {
        "hcx_l2": sum(
            int(row.get("attempts") or 0)
            for row in result.get("l2", {}).get("article_runs") or []
        ),
        "official_search": official.calls,
        "bm25": bm25.calls,
        "query_encoder": encoder.calls,
        "qdrant_dense": dense.calls,
        "reranker": 0 if release_bound_mode else reranker.calls,
        "metadata_api": profile_provider.metadata_api_calls,
        "cell_api": cell_fetcher.calls,
        "hcx_answer": 0 if answerer is None else answerer.calls,
        "rag_reasoning": 0,
    }
    result["article_api_calls"] = {
        **calls,
        **article_call_snapshot,
    }
    result["technical_canary_api_calls"] = canary_call_delta
    result["ranking"] = {
        "mode": RRF_ONLY_ORDERING if release_bound_mode else "MODEL_RERANKER",
        "reason": RRF_ORDER_RERANKER_DISABLED if release_bound_mode else None,
    }
    result["runtime"] = {
        "status": "COMPLETE",
        "api_calls": calls,
        "profile_cache": profile_provider.audit(),
        "passage_store": (
            {"enabled": False, "reason": "RELEASE_BOUND_POSTGRES_PROFILE"}
            if release_bound_mode else
            {"calls": passage_store.calls, "rows_read": passage_store.rows_read}
        ),
        "release_runtime": dict(gate) if release_bound_mode else {"enabled": False},
        "preflight_blockers": [],
        "silent_fallback": False,
    }
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "mode": "audit_live_shadow" if not include_technical_canary else "live_shadow",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input": {"path": str(article_path), "sha256": sha256_file(article_path), "articles": len(articles)},
        "acquisition": dict(acquisition_receipt or {}),
        "config": {"path": str(config_path), "sha256": sha256_file(config_path)},
        "gpu_receipts": gate["gpu_receipts"],
        "services": service_urls,
        "qdrant": (
            {
                "collection": gate["qdrant"]["collection"],
                "vector_size": gate["qdrant"]["vector_size"],
                "receipt_sha256": gate["qdrant"]["receipt_sha256"],
            }
            if release_bound_mode else
            {
                "collection": config["services"]["qdrant_collection"],
                "points": gate["qdrant_health"]["points_count"],
                "indexed_vectors": gate["qdrant_health"]["indexed_vectors_count"],
            }
        ),
        "release_runtime": dict(gate) if release_bound_mode else {"enabled": False},
        "ranking_mode": RRF_ONLY_ORDERING if release_bound_mode else "MODEL_RERANKER",
        "release_sha256_by_channel": release_sha,
        "api_calls": calls,
        "article_api_calls": result["article_api_calls"],
        "technical_canary_api_calls": canary_call_delta,
        "technical_canary": canary_manifest,
        "authority": config["authority"],
        "secrets_persisted": False,
        "audit_phase": audit_phase,
        "role_aware_dimension_shadow": role_aware_dimension_shadow,
        "claim_query": claim_query,
        "deterministic_answer_only": deterministic_answer_only,
    }
    if release_bound_mode:
        manifest["seed_cache"] = {"enabled": False, "reason": "RELEASE_BOUND_POSTGRES_PROFILE"}
    elif not include_technical_canary or audit_run_id or operational_cache_override is not None or snapshot_root_override is not None:
        manifest.update({
            "audit_run_id": audit_run_id,
            "seed_cache_sha256_before": seed_cache_sha_before,
            "seed_cache_sha256_after": sha256_file(seed_cache_path),
        })
        if manifest["seed_cache_sha256_before"] != manifest["seed_cache_sha256_after"]:
            raise OperationalPipelineError("SEED_CACHE_MUTATED")
        if budget_ledger is not None:
            manifest["http_attempt_budget"] = {
                "path": str(Path(budget_ledger_override).resolve()),
                "limits": dict(budget_ledger.limits),
                "used": {key: budget_ledger.used(key) for key in sorted(budget_ledger.limits)},
            }
    if defer_manifest_finalization:
        # Audit-local supervision owns child shutdown, receipt sealing, and
        # public manifest publication.  The ordinary path remains byte and
        # behavior compatible because this branch is explicit opt-in only.
        return {**result, "output_root": str(output_root), "manifest": manifest, "deferred_manifest_finalization": True}
    write_live_outputs(output_root, result, manifest)
    return {**result, "output_root": str(output_root)}


def run_live_from_url(
    config_path: str | Path,
    article_url: str,
    output_root: str | Path,
) -> dict[str, Any]:
    """Freeze one supported URL before invoking the ordinary live file path."""
    output_root = Path(output_root).resolve()
    frozen_path, receipt = acquire_article_url(article_url, output_root)
    return run_live_from_files(
        config_path,
        frozen_path,
        output_root,
        allow_existing_output=True,
        acquisition_receipt=receipt,
    )


def run_canary_from_config(
    config_path: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    """Run only the sealed metadata/cell spine; GPU retrieval is not involved."""
    config_path = Path(config_path).resolve()
    output_root = Path(output_root).resolve()
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite technical canary output: {output_root}")
    config = _read_json(config_path)
    root = config_path.parent.parent
    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env", override=False)
    except ImportError:
        pass
    seed = (root / config["assets"]["profile_cache"]).resolve()
    if not seed.is_file() or sha256_file(seed) != config["assets"]["profile_cache_sha256"]:
        raise OperationalPipelineError("PROFILE_CACHE_CONTRACT_MISMATCH")
    if not os.getenv("KOSIS_API_KEY") or not os.getenv("NCP_CLOVASTUDIO_API_KEY"):
        raise OperationalPipelineError("CANARY_API_CREDENTIALS_UNAVAILABLE")
    provider = OperationalProfileProvider(
        seed,
        (root / config["assets"]["operational_profile_cache"]).resolve(),
        (root / config["assets"]["operational_profile_snapshots"]).resolve(),
        max_age_seconds=float(config["limits"]["profile_max_age_seconds"]),
        delay_seconds=float(config["limits"]["metadata_delay_seconds"]),
    )
    from src.news_verification.runtime.kosis_client import get_data_from_query
    cell_fetcher = FailClosedCellFetcher(get_data_from_query)
    answerer = CountingAnswerer(Hcx007AnswerClient(os.environ["NCP_CLOVASTUDIO_API_KEY"]))
    canary_path = (root / config["assets"]["technical_canary"]).resolve()
    result = run_technical_canary(
        _read_json(canary_path),
        profile_provider=provider,
        cell_fetcher=cell_fetcher,
        hcx_answerer=answerer,
    )
    output_root.mkdir(parents=True)
    result_bytes = (
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    ).encode("utf-8")
    manifest = {
        "contract": "operational-technical-cell-canary-run-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metric_inclusion": False,
        "config": {"path": str(config_path), "sha256": sha256_file(config_path)},
        "canary_config": {"path": str(canary_path), "sha256": sha256_file(canary_path)},
        "profile_cache": provider.audit(),
        "api_calls": {
            "metadata_api": provider.metadata_api_calls,
            "cell_api": cell_fetcher.calls,
            "hcx_answer": answerer.calls,
            "gpu": 0,
        },
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "secrets_persisted": False,
    }
    (output_root / "result.json").write_bytes(result_bytes)
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return {**result, "output_root": str(output_root), "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=("preflight", "replay", "live", "canary"), default="preflight")
    parser.add_argument("--output", type=Path)
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--articles", type=Path)
    input_group.add_argument("--article-url")
    parser.add_argument("--check-reranker-service", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.mode == "preflight":
            result = preflight(args.config, check_service=args.check_reranker_service)
        elif args.mode == "replay":
            if args.output is None:
                raise OperationalPipelineError("--output is required for replay")
            result = run_replay_v2(args.config, args.output)
        elif args.mode == "live":
            if args.output is None or (args.articles is None and not args.article_url):
                raise OperationalPipelineError("--articles or --article-url and --output are required for live")
            result = (
                run_live_from_url(args.config, args.article_url, args.output)
                if args.article_url
                else run_live_from_files(args.config, args.articles, args.output)
            )
        else:
            if args.output is None:
                raise OperationalPipelineError("--output is required for canary")
            result = run_canary_from_config(args.config, args.output)
    except (ArticleAcquisitionError, OperationalPipelineError, LiveAdapterError, FileExistsError) as exc:
        failure = safe_adapter_failure("OPERATIONAL_PIPELINE_BLOCKED", exc)
        print(json.dumps({
            "contract_version": CONTRACT_VERSION,
            "status": "BLOCKED",
            "error": failure["error_code"],
            "error_type": failure["error_type"],
            "silent_fallback": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OperationalPipelineError", "Top50Resolution", "assignment_for_resolution",
    "compare_official_cell", "fetch_exact_single_cell", "l2_service_assessment", "preflight",
    "resolve_top50", "run_live_from_files", "run_live_from_url", "run_new_articles_v2", "run_operational_l2",
    "run_canary_from_config", "run_replay_v2", "run_technical_canary",
]
