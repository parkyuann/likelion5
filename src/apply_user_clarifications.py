"""사용자 재질의 답변을 검증해 즉시 검색 가능한 claim 입력으로 만든다."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .apply_context_referent_adjudications import read_fixture_rows
    from .user_clarification_adjudication import search_allowed, validate_user_response
except ImportError:  # pragma: no cover - standalone CLI support
    from apply_context_referent_adjudications import read_fixture_rows
    from user_clarification_adjudication import search_allowed, validate_user_response


PERIOD_RE = re.compile(r"^(?P<year>19\d{2}|20\d{2})(?:-(?P<month>0[1-9]|1[0-2])|-Q(?P<quarter>[1-4]))?$")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def json_array(value: str) -> list[str]:
    parsed = json.loads(value or "[]")
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("missing_slots_json must be an array of strings")
    return parsed


def json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("user_response_json must be an object")
    return parsed


def normalized_period(value: str) -> tuple[str, str]:
    match = PERIOD_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("period response must be YYYY, YYYY-MM, or YYYY-QN")
    if match["month"]:
        return value.strip(), "월"
    if match["quarter"]:
        return value.strip(), "분기"
    return value.strip(), "년"


def apply_response(claim: dict[str, Any], slots: list[str], response: dict[str, Any]) -> dict[str, Any]:
    """사용자 답변을 구조화 입력에 병합한다. 호출자는 search_allowed를 먼저 확인한다."""
    item = json.loads(json.dumps(claim, ensure_ascii=False))
    dimensions = dict(item.get("dimension_terms") or {})
    audit: dict[str, Any] = {}
    for slot in slots:
        answer = response[slot]
        if not isinstance(answer, dict):  # search_allowed() precondition
            raise ValueError(f"non-value response cannot be applied: {slot}")
        value = str(answer["value"]).strip()
        audit[slot] = {"value": value, "basis": answer["basis"], "evidence_sentence_index": answer.get("evidence_sentence_index")}
        if slot == "indicator":
            item["indicator_raw"] = value
        elif slot == "period":
            item["period"], item["period_type"] = normalized_period(value)
        elif slot.startswith("dimension:"):
            dimensions[slot.split(":", 1)[1]] = value
        elif slot == "kosis_scope_or_indicator":
            item["user_kosis_scope_or_indicator"] = value
    if dimensions:
        item["dimension_terms"] = dimensions
    item["user_clarification_audit"] = {"source": "USER", "responses": audit}
    return item


def apply_rows(claims: list[dict[str, Any]], fixture_rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id = {str(row.get("context_eval_id") or ""): row for row in fixture_rows}
    applied: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for claim in claims:
        context_eval_id = str(claim.get("context_eval_id") or "")
        row = by_id.get(context_eval_id)
        if row is None:
            audit.append({"context_eval_id": context_eval_id, "status": "USER_REQUIRED", "reason": "clarification_fixture_missing"})
            continue
        response_status = str(row.get("user_response_status") or "pending")
        if response_status != "answered":
            audit.append({"context_eval_id": context_eval_id, "status": "USER_REQUIRED", "reason": f"response_status:{response_status}"})
            continue
        try:
            slots = json_array(str(row.get("missing_slots_json") or "[]"))
            response = json_object(str(row.get("user_response_json") or "{}"))
            errors = validate_user_response(slots, response)
            if errors:
                audit.append({"context_eval_id": context_eval_id, "status": "USER_REQUIRED", "reason": "; ".join(errors)})
                continue
            if not search_allowed(slots, response):
                audit.append({"context_eval_id": context_eval_id, "status": "UNVERIFIABLE", "reason": "user_reported_no_evidence"})
                continue
            applied.append(apply_response(claim, slots, response))
            audit.append({"context_eval_id": context_eval_id, "status": "SEARCH_RESUMED", "reason": "validated_user_response"})
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            audit.append({"context_eval_id": context_eval_id, "status": "USER_REQUIRED", "reason": str(error)})
    return applied, audit


def main() -> None:
    parser = argparse.ArgumentParser(description="사용자 재질의 답변을 검증해 검색 입력으로 적용")
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    applied, audit = apply_rows(read_jsonl(args.claims), read_fixture_rows(args.responses))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in applied), encoding="utf-8")
    args.audit_output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in audit), encoding="utf-8")
    print(json.dumps({"input_claims": len(audit), "search_resumed": len(applied), "audit_status_counts": dict(sorted(Counter(row["status"] for row in audit).items()))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
