"""Bounded user-clarification and case-based retrieval correction policy.

The case library stores reusable recovery actions, never answer values or
table IDs.  A correction may add one deterministic query round; it cannot
override Late Binding or the strict cell validator.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
# This is a closed, shared wire contract.  It deliberately describes semantic
# constraints, never table/axis/value authority.  Keep the packaged copy and
# the repository source byte-identical (the closure test enforces that).
CLARIFICATION_ROLES = (
    "article_date", "period", "indicator", "item", "unit", "source", "population",
    "region", "sex", "age", "classification", "measurement_basis",
)
_CLARIFICATION_PRIORITY = CLARIFICATION_ROLES


@dataclass(frozen=True)
class SlotDiagnostic:
    role: str
    status: str
    table_key: str = ""
    profile_sha256: str | None = None
    axis_semantic_role: str | None = None
    axis_inventory_path: str | None = None
    option_inventory: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""


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
        # An incomplete profile cannot safely supply a user-selectable value.
        # Do not let one bad profile suppress options supported by complete
        # candidates; the caller records its exclusion in the plan receipt.
        if "PROFILE_INCOMPLETE" in tuple(getattr(projection, "hold_reasons", ()) or ()):
            continue
        for assignment in getattr(projection, "assignments", ()):
            for binding in getattr(assignment, "bindings", ()):
                evidence = getattr(binding, "evidence", {})
                label = str(evidence.get("profile_label") or "").strip() if isinstance(evidence, Mapping) else ""
                role = str(getattr(binding, "bound_atom", "") or "")
                if role and label:
                    labels_by_role.setdefault(role, set()).add(label)
    diagnostics: list[Mapping[str, Any]] = []
    for projection in projections:
        for diagnostic in getattr(projection, "slot_diagnostics", ()) or ():
            if isinstance(diagnostic, Mapping):
                diagnostics.append(diagnostic)
    option_roles: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for diagnostic in diagnostics:
        role = str(diagnostic.get("role") or "classification")
        if diagnostic.get("status") not in {"MISSING", "AMBIGUOUS"}:
            continue
        for item in diagnostic.get("option_inventory") or ():
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("label") or item.get("display_label") or "").strip()
            if label:
                option_roles.setdefault(role, {}).setdefault(label, []).append({
                    "table_key": diagnostic.get("table_key"),
                    "profile_sha256": diagnostic.get("profile_sha256"),
                    "axis_id": item.get("axis_id"), "value_id": item.get("value_id"),
                })
    differing = [(role, sorted(labels)) for role, labels in sorted(labels_by_role.items()) if len(labels) > 1]
    if not differing:
        differing = [(role, sorted(values)) for role, values in sorted(option_roles.items()) if values]
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
        "unit": "어떤 단위 기준인가요?", "item": "어떤 통계 항목 기준인가요?",
        "source": "어느 통계 작성기관 또는 조사 기준인지 알려주세요.",
        "sex": "어느 성별 기준인지 알려주세요.", "age": "어느 연령 기준인지 알려주세요.",
        "classification": "어떤 분류 기준인지 알려주세요.",
        "measurement_basis": "증가율과 증가량 중 어떤 기준인지 알려주세요.",
    }
    option_map = option_roles.get(role, {})
    option_objects = [
        {
            "id": "co-" + hashlib.sha256(f"{role}:{label}".encode("utf-8")).hexdigest()[:24],
            "label": label,
            "description": f"현재 후보 통계표 {len(option_map.get(label) or [])}개에 적용 가능",
            "applicable_candidate_count": len(option_map.get(label) or []),
            "applicability": option_map.get(label) or [],
        }
        for label in labels
    ]
    return {
        "question_id": f"clarify-{role}", "role": role,
        "prompt": prompts.get(role, "어떤 통계 기준을 의미하는지 선택해 주세요."),
        "input_mode": "OPTIONS" if len(labels) <= 20 else "SEARCHABLE_OPTIONS",
        "allow_direct_input": False if option_objects else True,
        "options": option_objects, "answer": None, "options_complete": True,
        "internal_ids_exposed": False, "model_prefill": False,
    }


def build_post_binding_clarification_plan(top50: Any, *, target_id: str) -> dict[str, Any] | None:
    """Build an option-backed plan only from complete candidate profiles."""
    resolution = getattr(top50, "resolution", None)
    projections = tuple(getattr(top50, "projections", ()) or ())
    if not projections or getattr(resolution, "outcome", None) == "QUERY_READY":
        return None
    complete = tuple(
        projection for projection in projections
        if "PROFILE_INCOMPLETE" not in tuple(getattr(projection, "hold_reasons", ()) or ())
    )
    if not complete:
        return None
    question = _question_for_multiple(complete)
    if not question.get("options"):
        return None
    sealed_target_id = str(target_id or "").strip()
    if not sealed_target_id:
        raise ValueError("BINDING_CONTINUATION_TARGET_REQUIRED")
    membership = sorted({str(value) for value in getattr(top50, "candidate_membership", ()) or ()})
    question_id = str(question.get("question_id") or "")
    plan = {
        "contract_version": "clarification-plan-v2",
        "reason": str(getattr(resolution, "hold_reason", None) or "CLARIFICATION_REQUIRED"),
        "question": question,
        "candidate_membership_sha256": _sha(membership),
        "profile_bundle_sha256": _sha([
            {"table_key": projection.table_key, "canonical_sha256": projection.canonical_sha256}
            for projection in complete
        ]),
        "profile_exclusions": [
            {"table_key": str(projection.table_key), "reason": "PROFILE_INCOMPLETE"}
            for projection in projections
            if "PROFILE_INCOMPLETE" in tuple(getattr(projection, "hold_reasons", ()) or ())
        ],
        "speculative": False,
        "binding_continuation": {
            "contract_version": "binding-continuation-v1",
            "target_ids": [sealed_target_id],
            "target_scope_sha256": _sha([sealed_target_id]),
            "candidate_membership": membership,
            "candidate_membership_sha256": _sha(membership),
            "raw_profiles": {str(key): dict(value) for key, value in getattr(top50, "pinned_raw_profiles", {}).items()},
            "projection_profiles": {str(key): dict(value) for key, value in getattr(top50, "pinned_projection_profiles", {}).items()},
        },
    }
    plan["binding_continuation"]["profile_bundle_sha256"] = _sha([
        {"table_key": key, "profile_sha256": str(value.get("profile_sha256") or ""), "release_id": str(value.get("release_id") or "")}
        for key, value in sorted(plan["binding_continuation"]["raw_profiles"].items())
    ])
    plan["binding_continuation"]["projection_bundle_sha256"] = _sha(
        plan["binding_continuation"]["projection_profiles"]
    )
    release_ids = sorted({
        str(value.get("release_id") or "")
        for value in plan["binding_continuation"]["raw_profiles"].values()
        if isinstance(value, Mapping)
    })
    if len(release_ids) != 1 or not release_ids[0]:
        raise ValueError("BINDING_CONTINUATION_RELEASE_REQUIRED")
    plan["binding_continuation"]["release_id"] = release_ids[0]
    question["id"] = "cq-" + hashlib.sha256(f"{question_id}:{plan['candidate_membership_sha256']}".encode()).hexdigest()[:24]
    bundle = {
        "contract_version": "clarification-option-bundle-v2", "question_id": question["id"],
        "role": question["role"], "candidate_membership_sha256": plan["candidate_membership_sha256"],
        "profile_bundle_sha256": plan["profile_bundle_sha256"], "options": question["options"],
    }
    plan["question"]["options"] = bundle["options"]
    plan["option_bundle"] = bundle
    return plan


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
    "CASE_LIBRARY", "CLARIFICATION_ROLES", "CONTRACT_VERSION", "SlotDiagnostic", "build_post_binding_clarification_plan",
    "corrective_claim_query", "merge_candidate_rounds", "plan_failure_recovery",
]
