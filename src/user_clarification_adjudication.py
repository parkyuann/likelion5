"""사용자 재질의 응답의 최소 계약.

온라인 흐름의 사용자 답변은 검토 대기 대상이 아니다. 형식·출처를 검증한 뒤 검색
조건으로 사용할 수 있으며, 사용자가 제공한 값은 반드시 audit source로 남긴다.
"""

from __future__ import annotations

from typing import Any


ANSWER_BASES = {"article_evidence", "user_provided"}


def validate_user_response(required_slots: list[str], response: dict[str, Any]) -> list[str]:
    """모든 요구 슬롯이 기사 근거·사용자 보충·NO_EVIDENCE 중 하나로 답됐는지 검사한다."""
    errors: list[str] = []
    for slot in required_slots:
        answer = response.get(slot)
        if answer == "NO_EVIDENCE":
            continue
        if not isinstance(answer, dict) or not str(answer.get("value") or "").strip():
            errors.append(f"missing response for {slot}")
            continue
        basis = str(answer.get("basis") or "")
        if basis not in ANSWER_BASES:
            errors.append(f"invalid basis for {slot}")
            continue
        if basis == "article_evidence" and not isinstance(answer.get("evidence_sentence_index"), int):
            errors.append(f"article evidence index is required for {slot}")
    return errors


def search_allowed(required_slots: list[str], response: dict[str, Any]) -> bool:
    """NO_EVIDENCE가 하나라도 있으면 해당 claim 검색은 재개하지 않는다."""
    return not validate_user_response(required_slots, response) and all(response.get(slot) != "NO_EVIDENCE" for slot in required_slots)
