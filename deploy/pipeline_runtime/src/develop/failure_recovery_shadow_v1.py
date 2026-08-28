"""Bounded user-clarification and case-based retrieval correction policy.

The case library stores reusable recovery actions, never answer values or
table IDs.  A correction may add one deterministic query round; it cannot
override Late Binding or the strict cell validator.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

CONTRACT_VERSION = "failure-recovery-shadow-v1"

CASE_LIBRARY = (
    {
        "case_id": "ITEM_INDICATOR_LEXICAL_GAP_V1",
        "failure_signature": "CANDIDATE_MISSING_SUSPECTED",
        "action": "QUERY_EXPLICIT_ITEM_TERMS",
        "evidence": "docs/고도화/설계도_CRAG_보정_검색_20260811.md",
    },
    {
        "case_id": "PERIOD_FREQUENCY_LEXICAL_GAP_V1",
        "failure_signature": "CANDIDATE_MISSING_SUSPECTED",
        "action": "QUERY_INDICATOR_WITH_EXPLICIT_FREQUENCY",
        "evidence": "docs/고도화/설계도_CRAG_보정_검색_20260811.md",
    },
)

_CLARIFY_REASONS = frozenset({
    "MULTIPLE_COMPATIBLE_SERIES", "REGION_UNBOUND", "POPULATION_UNBOUND",
    "PERIOD_UNKNOWN", "PERIOD_INVALID",
})
_CLARIFICATION_PRIORITY = ("article_date", "period", "region", "population", "indicator", "unit")


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _fields(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("retrieval_fields")
    return value if isinstance(value, Mapping) else {}


def _retrieval_safe_text(value: Any) -> str:
    """Keep semantic words while excluding asserted values from case replay."""
    text = re.sub(
        r"[+-]?\d[\d,.]*(?:조|억|천만|백만|만|천)?(?:원|달러|명|가구|개|건|%p|%)?",
        " ", str(value or ""),
    )
    return re.sub(r"\s+", " ", text).strip()


def _relative_period_without_article_date(row: Mapping[str, Any]) -> bool:
    fields = _fields(row)
    raw = " ".join(str(fields.get(key) or row.get(key) or "") for key in ("period_raw", "period"))
    return bool(re.search(r"(?:지난|작년|지난해|올해|이번|다음)\s*(?:\d{1,2}\s*)?(?:월|분기)|\b\d{1,2}월\b", raw)) and not str(row.get("article_date") or "").strip()


def _question_for_missing(reason: str, row: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if reason in {"PERIOD_UNKNOWN", "PERIOD_INVALID"} and row is not None and _relative_period_without_article_date(row):
        return {
            "question_id": "clarify-article_date", "role": "article_date",
            "prompt": "기사에서 말한 상대 시점의 연도를 정하려면 기사 발행일을 알려주세요.",
            "input_mode": "DATE", "options": [], "answer": None,
            "internal_ids_exposed": False, "model_prefill": False,
        }
    prompts = {
        "REGION_UNBOUND": ("region", "어느 지역 기준인지 알려주세요."),
        "POPULATION_UNBOUND": ("population", "어떤 대상 집단 기준인지 알려주세요."),
        "PERIOD_UNKNOWN": ("period", "확인할 기준 시점과 통계 주기를 알려주세요."),
        "PERIOD_INVALID": ("period", "확인할 기준 시점과 통계 주기를 다시 알려주세요."),
    }
    role, prompt = prompts[reason]
    return {
        "question_id": f"clarify-{role}", "role": role, "prompt": prompt,
        "input_mode": "FREE_TEXT", "options": [], "answer": None,
        "internal_ids_exposed": False, "model_prefill": False,
    }


def _question_for_multiple(projections: Sequence[Any]) -> dict[str, Any]:
    labels_by_role: dict[str, set[str]] = {}
    for projection in projections:
        for assignment in getattr(projection, "assignments", ()):
            for binding in getattr(assignment, "bindings", ()):
                evidence = getattr(binding, "evidence", {})
                label = str(evidence.get("profile_label") or "").strip() if isinstance(evidence, Mapping) else ""
                role = str(getattr(binding, "bound_atom", "") or "")
                if role and label:
                    labels_by_role.setdefault(role, set()).add(label)
    differing = [(role, sorted(labels)) for role, labels in sorted(labels_by_role.items()) if len(labels) > 1]
    if not differing:
        return {
            "question_id": "clarify-indicator", "role": "indicator", "prompt": "어떤 통계 지표를 확인할까요?",
            "input_mode": "FREE_TEXT", "options": [], "answer": None,
            "internal_ids_exposed": False, "model_prefill": False,
        }
    role, labels = differing[0]
    prompts = {
        "indicator": "어떤 통계 지표를 확인할까요?", "region": "어느 지역 기준인가요?",
        "population": "어떤 대상 집단 기준인가요?", "period": "어떤 시점·주기 기준인가요?",
        "unit": "어떤 단위 기준인가요?",
    }
    return {
        "question_id": f"clarify-{role}", "role": role,
        "prompt": prompts.get(role, "어떤 통계 기준을 의미하는지 선택해 주세요."),
        "input_mode": "OPTIONS" if len(labels) <= 20 else "SEARCHABLE_OPTIONS",
        "options": labels, "answer": None, "options_complete": True,
        "internal_ids_exposed": False, "model_prefill": False,
    }


def _item_miss_ratio(projections: Sequence[Any]) -> float:
    if not projections:
        return 0.0
    misses = 0
    for projection in projections:
        abstained = getattr(projection, "abstained", ())
        if any(str(kind) == "ITEM" and "NO_" in str(reason) for kind, reason in abstained):
            misses += 1
    return misses / len(projections)


def _correction_terms(row: Mapping[str, Any]) -> list[dict[str, str]]:
    fields = _fields(row)
    indicator = _retrieval_safe_text(fields.get("indicator"))
    raw_items = fields.get("item") if isinstance(fields.get("item"), list) else []
    terms: list[dict[str, str]] = []
    for item in raw_items:
        text = _retrieval_safe_text(item)
        if text and text != indicator:
            terms.append({"case_id": "ITEM_INDICATOR_LEXICAL_GAP_V1", "role": "corrective_item", "text": text})
    granularity = str(row.get("field_provenance", {}).get("period_granularity") or "").lower() if isinstance(row.get("field_provenance"), Mapping) else ""
    frequency = {"year": "연간", "month": "월간", "quarter": "분기"}.get(granularity, "")
    if indicator and frequency and frequency not in indicator:
        terms.append({"case_id": "PERIOD_FREQUENCY_LEXICAL_GAP_V1", "role": "corrective_frequency", "text": f"{frequency} {indicator}"})
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for term in terms:
        unique.setdefault((term["role"], term["text"]), term)
    return list(unique.values())


def plan_failure_recovery(row: Mapping[str, Any], top50: Any | None) -> dict[str, Any]:
    """Return SKIP, ASK_USER, CORRECTIVE_RETRIEVAL, or STOP."""
    resolution = getattr(top50, "resolution", None)
    reason = str(getattr(resolution, "hold_reason", None) or "NO_CANDIDATES")
    projections = tuple(getattr(top50, "projections", ()) or ())
    candidate_membership = tuple(getattr(top50, "candidate_membership", ()) or ())
    if getattr(resolution, "outcome", None) == "QUERY_READY":
        result = {"action": "SKIP", "reason": "QUERY_READY", "retry_budget": {"used": 0, "limit": 1}}
    elif reason in _CLARIFY_REASONS:
        question = _question_for_multiple(projections) if reason == "MULTIPLE_COMPATIBLE_SERIES" else _question_for_missing(reason, row)
        result = {"action": "ASK_USER", "reason": reason, "question": question, "retry_budget": {"used": 0, "limit": 1}}
    else:
        candidate_missing = not candidate_membership or (
            reason == "NO_COMPATIBLE_SERIES" and _item_miss_ratio(projections) >= 0.8
        )
        terms = _correction_terms(row) if candidate_missing else []
        if candidate_missing and terms:
            result = {
                "action": "CORRECTIVE_RETRIEVAL", "reason": "CANDIDATE_MISSING_SUSPECTED",
                "case_ids": sorted({term["case_id"] for term in terms}),
                "corrective_terms": terms, "retry_budget": {"used": 0, "limit": 1},
                "round0_preserved": True, "cell_values_used": False,
            }
        else:
            result = {"action": "STOP", "reason": reason or "LAYER_SPECIFIC_FAILURE", "retry_budget": {"used": 0, "limit": 1}}
    result = {"contract_version": CONTRACT_VERSION, **result}
    result["sha256"] = _sha(result)
    return result


def corrective_claim_query(base: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    if plan.get("action") != "CORRECTIVE_RETRIEVAL" or not plan.get("corrective_terms"):
        raise ValueError("CORRECTIVE_RETRIEVAL_NOT_PLANNED")
    return {"_corrective_only": True, "corrective_terms": list(plan["corrective_terms"])}


def merge_candidate_rounds(
    round0: Sequence[Any], round1: Sequence[Any], *, limit: int = 100,
) -> tuple[Any, ...]:
    """Losslessly union two candidate rounds and recompute flat RRF."""
    # Imported lazily to keep the policy module independent from the retrieval
    # module that calls it during orchestration.
    from src.develop.operational_retrieval_v2 import RRF_K, RrfCandidate

    grouped: dict[str, dict[tuple[str, str, str], Any]] = {}
    for candidate in tuple(round0) + tuple(round1):
        table_hits = grouped.setdefault(candidate.table_key, {})
        for hit in candidate.hits:
            table_hits.setdefault((hit.query_id, hit.channel, hit.record_id), hit)
    candidates = []
    for table_key, hit_map in grouped.items():
        hits = tuple(sorted(hit_map.values(), key=lambda hit: (hit.query_id, hit.channel, hit.rank, hit.record_id)))
        candidates.append(RrfCandidate(
            table_key=table_key,
            rrf_score=sum(1.0 / (RRF_K + hit.rank) for hit in hits),
            best_rank=min(hit.rank for hit in hits),
            hits=hits,
        ))
    return tuple(sorted(candidates, key=lambda row: (-row.rrf_score, row.best_rank, row.table_key))[:limit])


__all__ = [
    "CASE_LIBRARY", "CONTRACT_VERSION", "corrective_claim_query", "merge_candidate_rounds",
    "plan_failure_recovery",
]
