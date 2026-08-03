"""구조화 claim과 KOSIS 전체 registry 사이의 profile coverage를 보수적으로 감사한다.

registry-only 표는 표명·분류경로만 있으므로, 이 도구는 후보를 확정하거나 API를 호출하지
않는다. claim의 지표가 표명에 직접 나타나는 경우만 metadata 추가 수집 *검토* 후보로
기록하고, 부분 문구나 일반 토큰만 겹치는 경우는 자동 수집하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

try:
    from .claim_table_aligner import normalized
except ImportError:  # pragma: no cover - standalone CLI support
    from claim_table_aligner import normalized


MIN_TERM_LENGTH = 2


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def usable_term(value: object) -> str | None:
    text = str(value or "").strip()
    compact = normalized(text)
    if len(compact) < MIN_TERM_LENGTH:
        return None
    # 숫자만 있는 값은 통계 항목명이 아니라 수치 claim의 일부일 가능성이 높다.
    if re.fullmatch(r"[0-9.]+", compact):
        return None
    return text


def claim_terms(claim: dict[str, Any]) -> list[dict[str, str]]:
    """자동 지표와 사람이 확정한 문맥 대상을 출처와 함께 중복 제거한다."""
    candidates: list[tuple[str, object]] = [("indicator_raw", claim.get("indicator_raw"))]
    resolution = claim.get("context_resolution")
    if isinstance(resolution, dict) and resolution.get("adjudication_source") == "HUMAN":
        for value in resolution.get("resolved_terms", []):
            candidates.append(("human_context_term", value))
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, value in candidates:
        term = usable_term(value)
        compact = normalized(term)
        if not term or compact in seen:
            continue
        seen.add(compact)
        selected.append({"term": term, "normalized_term": compact, "source": source})
    return selected


def audit_coverage(
    claims: list[dict[str, Any]], registry: Iterable[dict[str, Any]], *, sample_limit: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return claim audit rows, human-review metadata seed candidates, and a manifest."""
    term_owners: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    audits: dict[str, dict[str, Any]] = {}
    for claim in claims:
        claim_id = str(claim.get("context_eval_id") or "")
        if not claim_id:
            continue
        terms = claim_terms(claim)
        audits[claim_id] = {
            "context_eval_id": claim_id,
            "article_idx": claim.get("article_idx"),
            "claim_text": claim.get("claim_text"),
            "mapping_eligibility": claim.get("mapping_eligibility"),
            "terms": terms,
            "registry_direct_match_count": 0,
            "registry_direct_matches": [],
        }
        for entry in terms:
            term_owners[entry["normalized_term"]].append((claim_id, entry["term"], entry["source"]))

    # 한 registry 행을 한 번만 정규화한 뒤 모든 claim 용어를 비교한다.
    for table in registry:
        title = str(table.get("tbl_name") or "")
        title_normalized = normalized(title)
        if not title_normalized:
            continue
        for term_normalized, owners in term_owners.items():
            if term_normalized not in title_normalized:
                continue
            for claim_id, term, source in owners:
                audit = audits[claim_id]
                audit["registry_direct_match_count"] += 1
                if len(audit["registry_direct_matches"]) < sample_limit:
                    audit["registry_direct_matches"].append({
                        "table_key": table.get("table_key"), "org_id": table.get("org_id"),
                        "tbl_id": table.get("tbl_id"), "tbl_name": title,
                        "category_paths": table.get("category_paths", []),
                        "profile_present": bool(table.get("profile_present")),
                        "metadata_status": table.get("metadata_status"),
                        "matched_term": term, "term_source": source,
                        "table_name_exact": title_normalized == term_normalized,
                    })

    review_seeds: list[dict[str, Any]] = []
    for audit in audits.values():
        matches = audit["registry_direct_matches"]
        if not audit["terms"]:
            audit["coverage_status"] = "NO_USABLE_STRUCTURED_TERM"
            continue
        if not matches:
            audit["coverage_status"] = "NO_REGISTRY_TITLE_MATCH"
            continue
        unique_match_keys = {str(match["table_key"] or "") for match in matches}
        audit["registry_direct_unique_table_count"] = len(unique_match_keys)
        # 표명이 여러 개면 현재 구조화 정보만으로 어느 표를 수집해야 할지 결정할 수 없다.
        # profile 유무보다 후보의 유일성을 먼저 판단해야 broad term을 coverage로 오해하지 않는다.
        if len(unique_match_keys) != 1:
            audit["coverage_status"] = "AMBIGUOUS_REGISTRY_TITLE_MATCH"
        elif matches[0]["profile_present"]:
            audit["coverage_status"] = "PROFILE_ALREADY_AVAILABLE"
        else:
            audit["coverage_status"] = "UNIQUE_TITLE_MATCH_REVIEW_REQUIRED"

        # 표명이 claim 지표와 정확히 같고, 그 표가 유일할 때만 수집 후보로 자동 제안한다.
        exact_missing = [
            match for match in matches
            if not match["profile_present"] and match["table_name_exact"] and match["term_source"] == "indicator_raw"
        ]
        unique_keys = {str(match["table_key"] or "") for match in exact_missing}
        if len(unique_keys) == 1:
            chosen = exact_missing[0]
            review_seeds.append({
                "context_eval_id": audit["context_eval_id"], "table_key": chosen["table_key"],
                "org_id": chosen["org_id"], "tbl_id": chosen["tbl_id"], "tbl_name": chosen["tbl_name"],
                "reason": "unique_exact_indicator_to_table_name", "matched_term": chosen["matched_term"],
                "review_required": True,
            })

    rows = [audits[claim_id] for claim_id in sorted(audits)]
    manifest = {
        "input_claims": len(claims), "audited_claims": len(rows),
        "status_counts": dict(sorted(Counter(row.get("coverage_status") for row in rows).items())),
        "claims_with_registry_title_match": sum(bool(row["registry_direct_matches"]) for row in rows),
        "metadata_review_seed_count": len(review_seeds),
        "policy": {
            "registry_match": "claim term must occur verbatim in tbl_name after whitespace/case normalization",
            "metadata_seed": "one registry-only table whose tbl_name exactly equals indicator_raw; review required",
            "not_automatic": "token overlap, partial table-name match, human-context term-only match",
        },
    }
    return rows, review_seeds, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="claim별 KOSIS profile coverage 감사")
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-seeds", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    rows, seeds, manifest = audit_coverage(list(read_jsonl(args.claims)), read_jsonl(args.registry))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    args.review_seeds.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in seeds), encoding="utf-8")
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
