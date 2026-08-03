"""문맥 판정과 사람이 라벨링한 mapping eligibility를 검색 입력에 적용한다."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .apply_context_referent_adjudications import read_fixture_rows
    from .mapping_eligibility_adjudication import normalize_mapping_eligibility, validate_eligibility_decision
except ImportError:  # pragma: no cover - standalone CLI support
    from apply_context_referent_adjudications import read_fixture_rows
    from mapping_eligibility_adjudication import normalize_mapping_eligibility, validate_eligibility_decision


DEFAULT_BY_CONTEXT_DECISION = {
    "RESOLVED": "CONTEXT_EXPANDED",
    "AMBIGUOUS": "CONTEXT_REQUIRED_UNRESOLVED",
    "NO_CONTEXT": None,
    "SKIP": "OUT_OF_SCOPE",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def adjudicated_decisions(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for row in rows:
        errors = validate_eligibility_decision(row)
        if errors:
            raise ValueError(f"invalid mapping eligibility for {row.get('context_eval_id')}: {'; '.join(errors)}")
        context_eval_id = str(row.get("context_eval_id") or "")
        if not context_eval_id or context_eval_id in output:
            raise ValueError(f"duplicate or missing context_eval_id: {context_eval_id}")
        output[context_eval_id] = row
    return output


def apply_eligibilities(base_rows: list[dict[str, Any]], review_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    decisions = adjudicated_decisions(review_rows)
    seen_reviews: set[str] = set()
    output: list[dict[str, Any]] = []
    for base in base_rows:
        record = json.loads(json.dumps(base, ensure_ascii=False))
        context = record.get("context_resolution") if isinstance(record.get("context_resolution"), dict) else {}
        context_status = str(context.get("adjudication_status") or "")
        default = DEFAULT_BY_CONTEXT_DECISION.get(context_status)
        context_eval_id = str(record.get("context_eval_id") or "")
        if context_status == "NO_CONTEXT":
            decision = decisions.get(context_eval_id)
            if decision is None:
                raise ValueError(f"NO_CONTEXT without adjudicated eligibility: {context_eval_id}")
            seen_reviews.add(context_eval_id)
            raw = str(decision["mapping_eligibility"])
            eligibility = normalize_mapping_eligibility(raw)
            if eligibility is None:  # guarded by adjudicated_decisions; keep type-safe here
                raise ValueError(f"invalid normalized eligibility: {context_eval_id}")
            audit = {
                "raw_mapping_eligibility": raw, "mapping_eligibility_notes": decision["mapping_eligibility_notes"],
                "eligibility_review_status": decision["eligibility_review_status"], "source": "HUMAN",
            }
        elif default:
            eligibility = default
            audit = {"source": "context_adjudication", "context_adjudication_status": context_status}
        else:
            raise ValueError(f"unsupported context adjudication status: {context_status}")
        record["mapping_eligibility"] = eligibility
        record["mapping_eligibility_audit"] = audit
        output.append(record)
    extra = set(decisions) - seen_reviews
    if extra:
        raise ValueError(f"eligibility reviews do not match NO_CONTEXT inputs: {sorted(extra)}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="사람 mapping eligibility 판정을 context 적용 JSONL에 반영")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--reviewed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = apply_eligibilities(read_jsonl(args.input), read_fixture_rows(args.reviewed))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "mapping_eligibility_counts": dict(sorted(Counter(row["mapping_eligibility"] for row in rows).items())), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
