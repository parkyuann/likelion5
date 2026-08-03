"""문맥 referent 판정 결과를 검증하고 검색 입력에 안전하게 반영한다."""

from __future__ import annotations

from typing import Any

try:
    from .claim_context_resolver import build_contextual_query
except ImportError:  # pragma: no cover - standalone CLI support
    from claim_context_resolver import build_contextual_query


ADJUDICATION_STATUSES = {"RESOLVED", "AMBIGUOUS", "NO_CONTEXT", "SKIP"}


def validate_adjudication(fixture: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    """근거 없는 모델/사람 확정을 차단한다."""
    errors: list[str] = []
    status = str(decision.get("adjudication_status") or "")
    if status not in ADJUDICATION_STATUSES:
        return ["invalid adjudication_status"]
    candidate_terms = {str(value) for value in fixture.get("candidate_terms", [])}
    if status == "RESOLVED":
        referent = str(decision.get("selected_referent") or "")
        if not referent:
            errors.append("selected_referent is required for RESOLVED")
        elif referent not in candidate_terms:
            errors.append("selected_referent must be one of candidate_terms")
        evidence_index = decision.get("evidence_sentence_index")
        evidence_indices = {
            entry.get("sentence_index") for entry in fixture.get("evidence", [])
            if isinstance(entry, dict) and entry.get("term") == referent
        }
        if evidence_index not in evidence_indices:
            errors.append("evidence_sentence_index must support selected_referent")
    elif decision.get("selected_referent"):
        errors.append("selected_referent is allowed only for RESOLVED")
    return errors


def apply_adjudication(
    claim_text: str, fixture: dict[str, Any], decision: dict[str, Any],
) -> dict[str, Any]:
    """유효 판정만 RESOLVED로 승격하고 contextual query를 만든다."""
    errors = validate_adjudication(fixture, decision)
    if errors:
        raise ValueError("; ".join(errors))
    status = str(decision["adjudication_status"])
    base = {
        "adjudication_status": status,
        "adjudication_source": str(decision.get("adjudication_source") or "human_or_hcx"),
        "fixture_id": fixture.get("context_eval_id"),
    }
    if status == "RESOLVED":
        referent = str(decision["selected_referent"])
        evidence = [
            entry for entry in fixture.get("evidence", [])
            if isinstance(entry, dict) and entry.get("term") == referent
            and entry.get("sentence_index") == decision.get("evidence_sentence_index")
        ]
        resolution = {
            **base, "status": "RESOLVED", "resolved_terms": [referent], "evidence": evidence,
            "retrieval_policy": "context_expanded",
        }
    elif status == "AMBIGUOUS":
        resolution = {**base, "status": "REFERENT_AMBIGUOUS", "resolved_terms": [],
                      "candidate_terms": fixture.get("candidate_terms", []), "evidence": fixture.get("evidence", []),
                      "retrieval_policy": "claim_only_alignment_blocked"}
    else:
        resolution = {**base, "status": "CONTEXT_MISSING", "resolved_terms": [], "evidence": [],
                      "retrieval_policy": "claim_only_alignment_blocked"}
    return {"context_resolution": resolution, "retrieval_query_text": build_contextual_query(claim_text, resolution)}
