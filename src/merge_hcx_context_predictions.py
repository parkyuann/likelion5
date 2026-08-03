"""HCX 문맥 지시어 예측을 사람 검토용 fixture에 병합한다.

이 모듈은 HCX 예측을 ``adjudication_*`` 열에 반영하지 않는다. 따라서 결과
파일은 사람이 검토한 뒤에만 ``apply_context_referent_adjudications.py``의 입력이
될 수 있다.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


PREDICTION_COLUMNS = [
    "hcx_run_status",
    "hcx_adjudication_status",
    "hcx_selected_referent",
    "hcx_evidence_sentence_index",
    "hcx_adjudication_notes",
    "hcx_latency_ms",
    "hcx_validation_error",
]


def read_results(path: Path) -> dict[str, dict[str, Any]]:
    """동일 ID가 있으면 재시도 후의 마지막 결과를 사용한다."""
    results: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return results
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        context_eval_id = str(row.get("context_eval_id") or "")
        if context_eval_id:
            results[context_eval_id] = row
    return results


def prediction_fields(result: dict[str, Any] | None) -> dict[str, str]:
    empty = {column: "" for column in PREDICTION_COLUMNS}
    if result is None:
        return empty
    prediction = result.get("prediction") if isinstance(result.get("prediction"), dict) else {}
    return {
        "hcx_run_status": str(result.get("status") or ""),
        "hcx_adjudication_status": str(prediction.get("adjudication_status") or ""),
        "hcx_selected_referent": str(prediction.get("selected_referent") or ""),
        "hcx_evidence_sentence_index": "" if prediction.get("evidence_sentence_index") is None else str(prediction["evidence_sentence_index"]),
        "hcx_adjudication_notes": str(prediction.get("adjudication_notes") or ""),
        "hcx_latency_ms": "" if result.get("latency_ms") is None else str(result["latency_ms"]),
        "hcx_validation_error": str(result.get("error") or ""),
    }


def merge_rows(rows: list[dict[str, str]], results: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """예측 열만 추가하고, 사람 판정 및 review_status는 원본 그대로 보존한다."""
    merged: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        item.update(prediction_fields(results.get(str(row.get("context_eval_id") or ""))))
        merged.append(item)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="HCX 문맥 예측을 사람 검토용 CSV로 병합")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with args.fixture.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    merged = merge_rows(rows, read_results(args.results))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames + [column for column in PREDICTION_COLUMNS if column not in fieldnames])
        writer.writeheader()
        writer.writerows(merged)
    counts = {"fixture_rows": len(merged), "predicted_rows": sum(bool(row["hcx_run_status"]) for row in merged)}
    print(json.dumps({**counts, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
