"""Derive an auditable L2 sentence-label scaffold from adjudicated claim gold."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _matrix_rows(snapshot: dict[str, Any], name: str) -> list[list[Any]]:
    matrix = snapshot.get("matrices", {}).get(name, [])
    rows = []
    for row in matrix:
        if isinstance(row, list):
            rows.append(row)
        elif isinstance(row, dict) and isinstance(row.get("value"), list):
            rows.append(row["value"])
    if not rows:
        raise ValueError(f"snapshot matrix is empty: {name}")
    return rows


def _records(rows: list[list[Any]]) -> list[dict[str, Any]]:
    header = [str(value) for value in rows[0]]
    return [
        dict(zip(header, [*row, *([None] * max(0, len(header) - len(row)))]))
        for row in rows[1:]
    ]


def _unique_text(values: list[Any], *, exclude: set[str] | None = None) -> list[str]:
    excluded = exclude or set()
    result = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in excluded or text in result:
            continue
        result.append(text)
    return result


def build_l2_sentence_gold(snapshot_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    sentence_rows = _records(_matrix_rows(snapshot, "sentence_review"))
    claim_rows = _records(_matrix_rows(snapshot, "claim_gold"))
    claims_by_sentence: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for claim in claim_rows:
        article_idx = str(claim.get("article_idx") or "")
        sentence_id = claim.get("sentence_id")
        if article_idx and isinstance(sentence_id, int):
            claims_by_sentence[(article_idx, sentence_id)].append(claim)

    output = []
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    for sentence in sentence_rows:
        article_idx = str(sentence.get("article_idx") or "")
        sentence_id = sentence.get("sentence_id")
        if not article_idx or not isinstance(sentence_id, int):
            continue
        claims = claims_by_sentence.get((article_idx, sentence_id), [])
        kosis = [
            claim for claim in claims
            if str(claim.get("검증대상 gold") or "") == "KOSIS_CANDIDATE"
        ]
        out_of_scope = [
            claim for claim in claims
            if str(claim.get("검증대상 gold") or "") == "OUT_OF_SCOPE"
        ]
        not_claim = [
            claim for claim in claims
            if str(claim.get("검증대상 gold") or "") == "NOT_CLAIM"
        ]
        indicators = _unique_text(
            [claim.get("indicator gold") for claim in kosis],
            exclude={"없음"},
        )
        periods = _unique_text(
            [claim.get("period gold") for claim in kosis],
            exclude={"없음"},
        )
        populations = _unique_text(
            [claim.get("population gold") for claim in kosis],
            exclude={"없음"},
        )
        if kosis and out_of_scope:
            source_region = "MIXED"
        elif kosis:
            source_region = "OFFICIAL_AGGREGATE"
        elif out_of_scope:
            source_region = "OUT_OF_SCOPE_UNSPECIFIED"
        elif not_claim:
            source_region = "NO_VERIFIABLE_NUMERIC_CLAIM"
        else:
            source_region = "UNLABELED_CONTEXT"

        if len(indicators) > 1:
            derivation_status = "MULTI_INDICATOR_EXPLICIT"
        elif len(indicators) == 1:
            derivation_status = "SINGLE_INDICATOR_EXPLICIT"
        elif claims:
            derivation_status = "NO_KOSIS_INDICATOR_EXPLICIT"
        else:
            derivation_status = "REGION_INHERITANCE_REVIEW_REQUIRED"
        status_counts[derivation_status] += 1
        source_counts[source_region] += 1
        output.append({
            "sentence_review_id": sentence.get("문장검토ID"),
            "article_idx": article_idx,
            "sentence_id": sentence_id,
            "title": sentence.get("기사제목"),
            "text": sentence.get("원문 문장"),
            "value_candidates_text": sentence.get("자동 value candidates"),
            "sentence_review_status": sentence.get("문장검토상태"),
            "claim_gold_count": len(claims),
            "kosis_claim_count": len(kosis),
            "out_of_scope_claim_count": len(out_of_scope),
            "not_claim_count": len(not_claim),
            "indicator_scope_gold": indicators,
            "period_context_gold": periods,
            "population_context_gold": populations,
            "source_region_gold": source_region,
            "derivation_status": derivation_status,
            "requires_human_region_review": derivation_status in {
                "MULTI_INDICATOR_EXPLICIT",
                "REGION_INHERITANCE_REVIEW_REQUIRED",
            },
        })

    report = {
        "snapshot_path": str(snapshot_path),
        "artifact_status": snapshot.get("artifact_status"),
        "sentence_rows": len(output),
        "claim_gold_rows": len(claim_rows),
        "derivation_status_counts": dict(status_counts),
        "source_region_counts": dict(source_counts),
        "single_indicator_explicit_coverage": (
            status_counts["SINGLE_INDICATOR_EXPLICIT"] / len(output)
            if output else 0.0
        ),
        "human_region_review_rows": sum(
            row["requires_human_region_review"] for row in output
        ),
        "limitations": [
            "Claim gold labels only sentences containing adjudicated numeric candidates.",
            "Empty/context sentences require article-region inheritance adjudication.",
            "OUT_OF_SCOPE claim gold does not encode the specific source-region subtype.",
            "Multiple KOSIS indicators in one sentence invalidate a single-label assumption.",
        ],
    }
    return output, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rows, report = build_l2_sentence_gold(args.snapshot)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.output_jsonl.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
