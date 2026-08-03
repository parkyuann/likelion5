"""사람이 확정한 문맥 claim을 v4 profile Top-K와 보수적으로 연결한다.

이 도구는 검색 gold나 최종 KOSIS 판정을 만들지 않는다. ``context_expanded``로
승격된 claim만 lexical 후보를 만들고, 후보 표의 API profile 안에서 exact 항목·차원
값·명시 시점이 모두 맞을 때에만 ``ALIGNED``를 기록한다.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .claim_table_aligner import align_profile, normalized
except ImportError:  # pragma: no cover - standalone CLI support
    from claim_table_aligner import align_profile, normalized


TOKEN_RE = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9·_-]{1,}|\d+(?:\.\d+)?")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tokens(text: object) -> list[str]:
    return [value.lower() for value in TOKEN_RE.findall(str(text or ""))]


def profile_text(profile: dict[str, Any]) -> str:
    return " ".join(str(profile.get(field) or "") for field in ("tbl_name", "doc_meta_text", "doc_item_index"))


def idf_for_profiles(profiles: list[dict[str, Any]]) -> dict[str, float]:
    frequency: Counter[str] = Counter()
    for profile in profiles:
        frequency.update(set(tokens(profile_text(profile))))
    count = max(1, len(profiles))
    return {term: math.log((count + 1) / (value + 1)) + 1.0 for term, value in frequency.items()}


def lexical_score(query: str, profile: dict[str, Any], idf: dict[str, float]) -> float:
    query_terms = set(tokens(query))
    document_terms = set(tokens(profile_text(profile)))
    overlap = query_terms & document_terms
    if not overlap:
        return 0.0
    value = sum(idf.get(term, 1.0) for term in overlap)
    # 표명 일치는 item/value 대용이 아니며, 후보 생성에서만 소폭 가산한다.
    title = normalized(profile.get("tbl_name"))
    value += 0.5 * sum(1 for term in overlap if len(term) >= 3 and term in title)
    return round(value, 6)


def structured_dimension_terms(claim: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """canonical claim에서 나온 단일 차원값만 사용한다.

    원문 검색은 후보 생성에만 사용한다. 여기서 원문 부분 문자열을 차원값으로 재해석하면
    ``계``·단위·일반명사가 우연히 맞아 셀을 잘못 선택할 수 있다.
    """
    direct = claim.get("dimension_terms")
    if isinstance(direct, dict):
        return {str(name): str(value) for name, value in direct.items() if str(name) and str(value)}, None
    raw = claim.get("dimension_json")
    if not isinstance(raw, dict):
        return {}, None
    selected: dict[str, str] = {}
    for name, values in raw.items():
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            if isinstance(values, list) and len(values) > 1:
                return selected, f"claim_dimension_ambiguous:{name}"
            continue
        value = str(values[0].get("normalized") or values[0].get("raw") or "")
        if value:
            selected[str(name)] = value
    return selected, None


def align_candidate(profile: dict[str, Any], claim: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """canonical 구조화 필드가 있는 경우에만 profile 정렬기로 넘긴다."""
    item_term = str(claim.get("indicator_raw") or "").strip() or None
    period = str(claim.get("period") or "").strip() or None
    period_type = str(claim.get("period_type") or "").strip() or None
    dimensions, dimension_reason = structured_dimension_terms(claim)
    hints = {
        "item_term": item_term,
        "dimension_terms": dimensions, "dimension_reason": dimension_reason,
        "period": period, "period_type": period_type,
    }
    if not item_term:
        return {"align_status": "ITEM_AMBIGUOUS", "reason": "claim_indicator_missing", "matched_dimensions": {}}, hints
    if dimension_reason:
        return {"align_status": "DIM_MISSING", "reason": dimension_reason, "matched_dimensions": {}}, hints
    if not period or not period_type:
        return {"align_status": "PERIOD_MISMATCH", "reason": "claim_period_missing", "matched_dimensions": {}}, hints
    return align_profile(profile, item_term=item_term, dimension_terms=dimensions, period=period, period_type=period_type), hints


def candidate_records(
    claims: list[dict[str, Any]], profiles: list[dict[str, Any]], *, top_k: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    enriched = [profile for profile in profiles if profile.get("meta_status") == "enriched"]
    idf = idf_for_profiles(enriched)
    output: list[dict[str, Any]] = []
    eligible_values = {"CONTEXT_EXPANDED", "CLAIM_ONLY_SAFE"}
    # gate가 있는 새 입력은 해당 상태를 최우선으로 사용한다. legacy 입력에만 기존
    # context-expanded 정책을 적용해 재현성을 보존한다.
    selected = [
        claim for claim in claims
        if (claim.get("mapping_eligibility") in eligible_values)
        or (claim.get("mapping_eligibility") is None and claim.get("context_resolution", {}).get("retrieval_policy") == "context_expanded")
    ]
    no_candidate = 0
    for claim in selected:
        query = str(claim.get("retrieval_query_text") or claim.get("claim_text") or "")
        ranked = sorted(
            ((lexical_score(query, profile, idf), profile) for profile in enriched),
            key=lambda pair: (-pair[0], str(pair[1].get("table_key") or "")),
        )
        ranked = [(score, profile) for score, profile in ranked if score > 0][:top_k]
        if not ranked:
            no_candidate += 1
        for rank, (score, profile) in enumerate(ranked, start=1):
            alignment, hints = align_candidate(profile, claim)
            output.append({
                "context_eval_id": claim.get("context_eval_id"), "article_idx": claim.get("article_idx"),
                "sentence_index": claim.get("sentence_index"), "claim_text": claim.get("claim_text"),
                "retrieval_query_text": query, "retrieval_stage": "v4_lexical_context_topk",
                "candidate_rank": rank, "candidate_score": score,
                "table_key": profile.get("table_key"), "org_id": profile.get("org_id"),
                "tbl_id": profile.get("tbl_id"), "tbl_name": profile.get("tbl_name"),
                "category_paths": profile.get("category_paths", []), "alignment_hints": hints,
                "alignment": alignment,
            })
    manifest = {
        "input_claims": len(claims), "eligible_claims": len(selected),
        "context_expanded_claims": sum(
            claim.get("mapping_eligibility") == "CONTEXT_EXPANDED"
            or (claim.get("mapping_eligibility") is None and claim.get("context_resolution", {}).get("retrieval_policy") == "context_expanded")
            for claim in selected
        ),
        "claim_only_safe_claims": sum(claim.get("mapping_eligibility") == "CLAIM_ONLY_SAFE" for claim in selected),
        "alignment_blocked_claims": len(claims) - len(selected), "enriched_profiles": len(enriched),
        "top_k": top_k, "claims_without_lexical_candidate": no_candidate,
        "candidate_records": len(output),
        "candidate_claims": len({str(record["context_eval_id"]) for record in output}),
        "alignment_status_counts": dict(sorted(Counter(str(record["alignment"].get("align_status")) for record in output).items())),
    }
    return output, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="사람 문맥 판정 claim의 v4 Top-K·profile 정렬 감사 기록 생성")
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()
    records, manifest = candidate_records(read_jsonl(args.claims), read_jsonl(args.catalog), top_k=args.top_k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
