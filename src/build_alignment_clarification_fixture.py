"""정렬 실패 claim을 근거 제한 재질의 fixture로 변환한다.

최종 사용자 흐름에서 재질의는 사용자에게 먼저 전달된다. 응답자는 기사 근거 문장 index를
제시하거나, 기사 밖의 보충 정보라면 ``user_provided``임을 명시해야 한다. 근거가 없으면
``NO_EVIDENCE``를 기록하고 검색을 계속하지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from .apply_context_referent_adjudications import read_fixture_rows
except ImportError:  # pragma: no cover - standalone CLI support
    from apply_context_referent_adjudications import read_fixture_rows


OUTPUT_FIELDS = [
    "context_eval_id", "article_idx", "sentence_index", "article_title", "claim_text", "context_window_json",
    "mapping_eligibility", "auto_indicator_raw", "auto_dimension_json", "auto_period", "auto_period_type",
    "missing_slots_json", "candidate_summaries_json", "clarification_questions_json", "clarification_request_status",
    "user_response_json", "user_response_notes", "user_response_status",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def slots_for_claim(claim: dict[str, Any], candidates: list[dict[str, Any]]) -> list[str]:
    reasons = {str(row.get("alignment", {}).get("reason") or "") for row in candidates}
    slots: list[str] = []
    if not claim.get("indicator_raw") or "item_exact_match_required" in reasons:
        slots.append("indicator")
    if not claim.get("period") or "claim_period_missing" in reasons:
        slots.append("period")
    for reason in reasons:
        if reason.startswith("claim_dimension_ambiguous:"):
            slots.append(f"dimension:{reason.split(':', 1)[1]}")
    if not candidates:
        slots.append("kosis_scope_or_indicator")
    return sorted(set(slots))


def questions(slots: list[str]) -> list[dict[str, str]]:
    prompt = {
        "indicator": "기사 문맥에 명시된 검증 지표명과 그 근거 문장 index를 제시하세요. 없으면 NO_EVIDENCE.",
        "period": "검증값의 기준 시점(연·월·분기)과 그 근거 문장 index를 제시하세요. 없으면 NO_EVIDENCE.",
        "kosis_scope_or_indicator": "이 claim이 KOSIS 집계통계로 검증 가능한 지표인지, 기사 근거 문장 index와 함께 판단하세요. 근거가 없으면 NO_EVIDENCE.",
    }
    result = []
    for slot in slots:
        if slot.startswith("dimension:"):
            dimension = slot.split(":", 1)[1]
            text = f"기사 문맥에서 {dimension} 차원값 하나와 근거 문장 index를 제시하세요. 하나로 확정할 수 없으면 NO_EVIDENCE."
        else:
            text = prompt[slot]
        result.append({"slot": slot, "question": text, "response_contract": "article_sentence_index_or_user_provided_or_NO_EVIDENCE"})
    return result


def build_rows(claims: list[dict[str, Any]], alignment_rows: list[dict[str, Any]], reviewed_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in alignment_rows:
        candidates[str(row.get("context_eval_id") or "")].append(row)
    review_by_id = {str(row.get("context_eval_id") or ""): row for row in reviewed_rows}
    output: list[dict[str, str]] = []
    for claim in claims:
        context_eval_id = str(claim.get("context_eval_id") or "")
        claim_candidates = sorted(candidates.get(context_eval_id, []), key=lambda row: int(row.get("candidate_rank") or 999))
        slots = slots_for_claim(claim, claim_candidates)
        if not slots:
            continue
        review = review_by_id.get(context_eval_id, {})
        summaries = [{
            "rank": row.get("candidate_rank"), "table_key": row.get("table_key"), "tbl_name": row.get("tbl_name"),
            "alignment_status": row.get("alignment", {}).get("align_status"), "reason": row.get("alignment", {}).get("reason"),
        } for row in claim_candidates[:3]]
        output.append({
            "context_eval_id": context_eval_id, "article_idx": str(claim.get("article_idx") or ""),
            "sentence_index": str(claim.get("sentence_index") or ""), "article_title": str(review.get("article_title") or ""),
            "claim_text": str(claim.get("claim_text") or ""), "context_window_json": str(review.get("context_window_json") or "[]"),
            "mapping_eligibility": str(claim.get("mapping_eligibility") or ""),
            "auto_indicator_raw": str(claim.get("indicator_raw") or ""),
            "auto_dimension_json": json.dumps(claim.get("dimension_json") or {}, ensure_ascii=False),
            "auto_period": str(claim.get("period") or ""), "auto_period_type": str(claim.get("period_type") or ""),
            "missing_slots_json": json.dumps(slots, ensure_ascii=False),
            "candidate_summaries_json": json.dumps(summaries, ensure_ascii=False),
            "clarification_questions_json": json.dumps(questions(slots), ensure_ascii=False), "clarification_request_status": "USER_REQUIRED",
            "user_response_json": "", "user_response_notes": "", "user_response_status": "pending",
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="정렬 실패 슬롯별 근거 제한 재질의 fixture 생성")
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--reviewed-context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = build_rows(read_jsonl(args.claims), read_jsonl(args.alignment), read_fixture_rows(args.reviewed_context))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"clarification_rows": len(rows), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
