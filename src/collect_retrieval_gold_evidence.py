"""통계표 gold 라벨링을 위한 KOSIS 검색 API 근거를 checkpoint 방식으로 수집한다.

검색 결과는 사람/모델 검토의 참고 근거일 뿐 gold 자체가 아니다. API key와
요청 URL은 산출물에 저장하지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

try:
    from .kosis_client import search_tables
except ImportError:
    from kosis_client import search_tables


ROOT = Path(__file__).resolve().parent.parent
KEEP_FIELDS = ("ORG_ID", "ORG_NM", "TBL_ID", "TBL_NM", "STAT_ID", "STAT_NM", "STRT_PRD_DE", "END_PRD_DE", "LINK_URL")


def read_annotation_rows(path: Path) -> list[dict[str, str]]:
    """검토 대상 중 아직 adjudication되지 않은 행만 읽는다."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("review_status") != "adjudicated"]


def read_completed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        str(json.loads(line).get("gold_eval_id") or "")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def compact_result(row: dict[str, Any]) -> dict[str, str]:
    """라벨 근거에 필요한 공식 식별자·표명·기간만 저장한다."""
    return {field: str(row.get(field) or "") for field in KEEP_FIELDS}


def collect(rows: list[dict[str, str]], output: Path, *, start: int, limit: int, result_count: int) -> dict[str, int]:
    """각 claim의 지표어로 KOSIS 표 검색 결과를 append해 중단 뒤 재개한다."""
    completed = read_completed_ids(output)
    target = rows[start:start + limit] if limit else rows[start:]
    written = skipped = errors = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for row in target:
            eval_id = str(row.get("gold_eval_id") or "")
            if not eval_id or eval_id in completed:
                skipped += 1
                continue
            query = str(row.get("indicator_raw") or row.get("claim_text") or "").strip()
            evidence: dict[str, Any] = {
                "gold_eval_id": eval_id,
                "claim_id": row.get("claim_id"),
                "query": query,
                "status": "OK",
                "results": [],
            }
            try:
                evidence["results"] = [compact_result(item) for item in search_tables(query, result_count=result_count)]
            except Exception as error:  # 개별 검색 실패는 라벨링 batch를 중단시키지 않는다.
                evidence["status"] = "ERROR"
                evidence["error_type"] = type(error).__name__
                evidence["error_message"] = str(error)
                errors += 1
            handle.write(json.dumps(evidence, ensure_ascii=False) + "\n")
            written += 1
    return {"target": len(target), "written": written, "skipped": skipped, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect KOSIS search evidence for retrieval-gold annotation")
    parser.add_argument("--input", type=Path, default=ROOT / "data/retrieval_gold_v1_annotation_20260724.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "data/retrieval_gold_v1_search_evidence_20260724.jsonl")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--result-count", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(collect(read_annotation_rows(args.input), args.output, start=args.start, limit=args.limit, result_count=args.result_count), ensure_ascii=False))


if __name__ == "__main__":
    main()
