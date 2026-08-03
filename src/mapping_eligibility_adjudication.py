"""문맥 referent와 별개인 KOSIS 매핑 가능성 판정 계약."""

from __future__ import annotations

from typing import Any

try:
    from .retrieval_schema import MAPPING_ELIGIBILITIES
except ImportError:  # pragma: no cover - standalone CLI support
    from retrieval_schema import MAPPING_ELIGIBILITIES


ELIGIBILITY_REVIEW_STATUSES = {"pending", "adjudicated"}
# 검토자가 사용한 AMBIGUOUS는 표·셀 매핑을 허용하지 않는다는 점에서
# CONTEXT_REQUIRED_UNRESOLVED의 입력 별칭이다. 원문 값은 audit에 보존한다.
MAPPING_ELIGIBILITY_ALIASES = {"AMBIGUOUS": "CONTEXT_REQUIRED_UNRESOLVED"}


def normalize_mapping_eligibility(value: object) -> str | None:
    raw = str(value or "")
    if raw in MAPPING_ELIGIBILITY_ALIASES:
        return MAPPING_ELIGIBILITY_ALIASES[raw]
    return raw if raw in MAPPING_ELIGIBILITIES else None


def validate_eligibility_decision(row: dict[str, Any]) -> list[str]:
    """재분류 행은 상태와 사람 검토 완료 여부를 함께 가져야 한다."""
    errors: list[str] = []
    eligibility = normalize_mapping_eligibility(row.get("mapping_eligibility"))
    review_status = str(row.get("eligibility_review_status") or "")
    if eligibility is None:
        errors.append("invalid mapping_eligibility")
    if review_status not in ELIGIBILITY_REVIEW_STATUSES:
        errors.append("invalid eligibility_review_status")
    if review_status != "adjudicated":
        errors.append("eligibility review must be adjudicated")
    if not str(row.get("mapping_eligibility_notes") or "").strip():
        errors.append("mapping_eligibility_notes is required")
    return errors
