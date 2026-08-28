"""Lazy compatibility seam for experimental shadow stages.

Phase 2 does not promote these implementations.  Keeping imports lazy makes
the boundary explicit and prevents the runtime package from owning shadow
implementation code.
"""

from __future__ import annotations

from typing import Any


def annual_requery_api() -> Any:
    from src.develop.annual_requery_shadow_v1 import AnnualRequeryError, verify_annual_requery

    return AnnualRequeryError, verify_annual_requery


def failure_recovery_api() -> Any:
    from src.develop.failure_recovery_shadow_v1 import corrective_claim_query, merge_candidate_rounds, plan_failure_recovery

    return corrective_claim_query, merge_candidate_rounds, plan_failure_recovery


def user_intent_api() -> Any:
    from src.develop.user_intent_router_shadow_v1 import route_user_intent

    return route_user_intent


def role_aware_dimension_api() -> Any:
    from src.develop.role_aware_dimension_shadow_v1 import (
        extract_source_terms,
        infer_profile_units,
        reranker_query,
        select_query_target,
        source_sentence,
    )

    return extract_source_terms, infer_profile_units, reranker_query, select_query_target, source_sentence


__all__ = ["annual_requery_api", "failure_recovery_api", "role_aware_dimension_api", "user_intent_api"]
