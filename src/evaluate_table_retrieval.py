"""사람이 adjudication한 gold가 있을 때만 통계표 검색 지표를 계산한다."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def load_adjudicated_matches(path: Path, *, allow_model_adjudication: bool = False) -> dict[str, str]:
    """빈 gold·pending·모델 전용 행을 최종 성능 평가에서 엄격히 제외한다.

    모델 adjudication은 catalog coverage 진단에는 유용하지만 최종 retrieval
    성능을 확정하는 human gold와 구분한다. 탐색 목적이면 호출자가 명시적으로
    ``allow_model_adjudication=True``를 전달해야 한다.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    matches = {
        str(row.get("claim_id") or ""): str(row.get("gold_table_key") or "")
        for row in rows
        if row.get("review_status") == "adjudicated"
        and row.get("gold_match_status") == "MATCH"
        and row.get("gold_table_key")
        and (allow_model_adjudication or not str(row.get("reviewer") or "").startswith("codex_"))
    }
    if not matches:
        raise ValueError("human-adjudicated MATCH gold rows are required before final Recall@K or MRR")
    return matches


def evaluate_candidates(gold: dict[str, str], candidate_rows: list[dict[str, Any]], cutoffs: tuple[int, ...] = (1, 5, 10, 20)) -> dict[str, Any]:
    """후보 JSONL의 table_key 순위만 사용해 재현 가능한 Recall@K와 MRR을 계산한다."""
    ranks: list[int | None] = []
    by_claim = {str(row.get("claim_id") or row.get("eval_claim_id") or ""): row for row in candidate_rows}
    for claim_id, table_key in gold.items():
        row = by_claim.get(claim_id, {})
        candidates = row.get("selected_tables") or row.get("candidates") or []
        rank = next((index for index, candidate in enumerate(candidates, start=1) if candidate.get("table_key") == table_key), None)
        ranks.append(rank)
    total = len(ranks)
    return {
        "evaluated_claims": total,
        "recall_at": {str(k): round(sum(rank is not None and rank <= k for rank in ranks) / total, 6) for k in cutoffs},
        "mrr": round(sum(1 / rank for rank in ranks if rank is not None) / total, 6),
        "missing_candidate_rows": sum(claim_id not in by_claim for claim_id in gold),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
