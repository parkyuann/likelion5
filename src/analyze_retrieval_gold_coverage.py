"""adjudication 결과로 catalog coverage와 순위 성능을 분리해 측정한다.

정답 표가 catalog에 없으면 reranker는 정답을 상위로 올릴 수 없다. 따라서
end-to-end Recall@K와 catalog 내 조건부 Recall@K를 같은 수치로 섞지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_adjudicated_matches(path: Path) -> list[dict[str, str]]:
    """MATCH·adjudicated·table key가 모두 있는 행만 coverage의 정답으로 사용한다."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        row for row in rows
        if row.get("review_status") == "adjudicated"
        and row.get("gold_match_status") == "MATCH"
        and row.get("gold_table_key")
    ]


def table_keys_from_catalog(path: Path) -> set[str]:
    return {str(row.get("table_key") or "") for row in read_jsonl(path) if row.get("table_key")}


def candidate_ranks(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """hybrid 결과의 표 순위를 claim별 table_key→rank 형태로 만든다."""
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        claim_id = str(row.get("claim_id") or "")
        if not claim_id:
            continue
        candidates = row.get("selected_tables") or row.get("candidates") or []
        result[claim_id] = {
            str(candidate.get("table_key")): rank
            for rank, candidate in enumerate(candidates, start=1)
            if candidate.get("table_key")
        }
    return result


def coverage_metrics(matches: list[dict[str, str]], catalog_keys: set[str], ranks: dict[str, dict[str, int]], cutoffs: tuple[int, ...] = (1, 5, 10, 20)) -> dict[str, Any]:
    """coverage 0과 ranking 0을 구분해 수치와 해석을 함께 반환한다."""
    total = len(matches)
    if not total:
        raise ValueError("at least one adjudicated MATCH row is required")
    covered = [row for row in matches if row["gold_table_key"] in catalog_keys]

    def hit(row: dict[str, str], cutoff: int) -> bool:
        # 현재 catalog에 없는 정답은 이 후보 공간에서 원리상 회수할 수 없다.
        # 외부 후보 파일이 우연히 같은 키를 포함하더라도 coverage 지표를 오염시키지 않는다.
        if row["gold_table_key"] not in catalog_keys:
            return False
        return ranks.get(str(row.get("claim_id") or ""), {}).get(row["gold_table_key"], cutoff + 1) <= cutoff

    end_to_end = {str(k): round(sum(hit(row, k) for row in matches) / total, 6) for k in cutoffs}
    conditional = (
        {str(k): round(sum(hit(row, k) for row in covered) / len(covered), 6) for k in cutoffs}
        if covered else None
    )
    return {
        "adjudicated_match_rows": total,
        "catalog_covered_match_rows": len(covered),
        "catalog_coverage": round(len(covered) / total, 6),
        "end_to_end_recall_at": end_to_end,
        "conditional_recall_at_when_gold_in_catalog": conditional,
        "ranking_metric_available": bool(covered),
        "coverage_gap_table_keys": sorted({row["gold_table_key"] for row in matches if row["gold_table_key"] not in catalog_keys}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Separate KOSIS catalog coverage from retrieval ranking")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/kosis_catalog_v3.jsonl")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gap-seeds", type=Path, required=True)
    args = parser.parse_args()
    matches = read_adjudicated_matches(args.gold)
    metrics = coverage_metrics(matches, table_keys_from_catalog(args.catalog), candidate_ranks(read_jsonl(args.candidates)))
    reviewers = Counter(str(row.get("reviewer") or "") for row in matches)
    metrics["adjudication_reviewer_counts"] = dict(sorted(reviewers.items()))
    if args.evidence:
        evidence_rows = read_jsonl(args.evidence)
        metrics["search_evidence_status_counts"] = dict(sorted(Counter(str(row.get("status") or "") for row in evidence_rows).items()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    gap_rows = []
    seen_gap_keys: set[str] = set()
    for row in matches:
        table_key = row["gold_table_key"]
        if table_key not in set(metrics["coverage_gap_table_keys"]) or table_key in seen_gap_keys:
            continue
        seen_gap_keys.add(table_key)
        gap_rows.append({
            "table_key": table_key,
            "org_id": row.get("gold_org_id") or table_key.split(":", 1)[0],
            "tbl_id": row.get("gold_tbl_id") or table_key.split(":", 1)[-1],
            "tbl_name": row.get("gold_tbl_name") or "",
            "stat_id": row.get("gold_stat_id") or "",
            "sample_source": "provisional_gold_coverage_gap",
            "official_evidence_url": row.get("official_evidence_url") or "",
        })
    args.gap_seeds.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in gap_rows), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
