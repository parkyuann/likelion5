"""사람이 NO_CONTEXT로 판정한 claim의 claim-only 안전성 재분류 fixture를 만든다."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

try:
    from .apply_context_referent_adjudications import read_fixture_rows
except ImportError:  # pragma: no cover - standalone CLI support
    from apply_context_referent_adjudications import read_fixture_rows


OUTPUT_FIELDS = [
    "context_eval_id", "article_idx", "sentence_index", "article_title", "claim_text",
    "rule_context_status", "human_context_status", "human_context_notes",
    "value_list", "unit_list", "time_ref", "source_org_raw", "change_type",
    "mapping_eligibility", "mapping_eligibility_notes", "eligibility_review_status",
]


def load_claim_rows(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            (str(row.get("article_idx") or ""), str(row.get("sentence_index") or "")): row
            for row in csv.DictReader(handle)
        }


def build_rows(reviewed_rows: list[dict[str, str]], claim_rows: dict[tuple[str, str], dict[str, str]]) -> list[dict[str, str]]:
    """NO_CONTEXT만 별도 queue로 내보내며 기존 판정은 수정하지 않는다."""
    output: list[dict[str, str]] = []
    for reviewed in reviewed_rows:
        if reviewed.get("adjudication_status") != "NO_CONTEXT":
            continue
        key = (str(reviewed.get("article_idx") or ""), str(reviewed.get("sentence_index") or ""))
        claim = claim_rows.get(key)
        if claim is None:
            raise ValueError(f"claim_listform row missing for {key}")
        output.append({
            "context_eval_id": str(reviewed.get("context_eval_id") or ""),
            "article_idx": key[0], "sentence_index": key[1],
            "article_title": str(reviewed.get("article_title") or ""),
            "claim_text": str(reviewed.get("claim_text") or ""),
            "rule_context_status": str(reviewed.get("rule_context_status") or ""),
            "human_context_status": "NO_CONTEXT",
            "human_context_notes": str(reviewed.get("adjudication_notes") or ""),
            "value_list": str(claim.get("value_list") or ""),
            "unit_list": str(claim.get("unit_list") or ""),
            "time_ref": str(claim.get("time_ref") or ""),
            "source_org_raw": str(claim.get("source_org_raw") or ""),
            "change_type": str(claim.get("change_type") or ""),
            "mapping_eligibility": "", "mapping_eligibility_notes": "",
            "eligibility_review_status": "pending",
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="NO_CONTEXT claim-only 안전성 재분류 fixture 생성")
    parser.add_argument("--reviewed", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = build_rows(read_fixture_rows(args.reviewed), load_claim_rows(args.claims))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print({"no_context_rows": len(rows), "output": str(args.output)})


if __name__ == "__main__":
    main()
