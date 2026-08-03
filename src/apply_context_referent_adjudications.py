"""완료된 문맥 판정 fixture를 검색 입력용 context resolution JSONL로 변환한다."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

try:
    from .context_referent_adjudication import apply_adjudication
except ImportError:  # pragma: no cover - standalone CLI support
    from context_referent_adjudication import apply_adjudication


def decode_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as error:
        raise ValueError("invalid fixture JSON column") from error
    if not isinstance(parsed, list):
        raise ValueError("fixture JSON column must be an array")
    return parsed


def read_fixture_rows(path: Path) -> list[dict[str, str]]:
    """UTF-8 CSV와 Excel이 저장한 CP949 TSV를 모두 읽는다.

    사람 검토 파일은 Excel을 거치면 BOM 없는 CP949 탭 구분 형식이 될 수 있다.
    판정 내용을 수정하지 않고 형식만 판별해 읽는다.
    """
    raw = path.read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in text.splitlines()[0] else csv.excel
    return list(csv.DictReader(text.splitlines(), dialect=dialect))


def apply_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("review_status") or "") != "adjudicated":
            continue
        fixture = {
            "context_eval_id": row.get("context_eval_id"),
            "candidate_terms": decode_list(str(row.get("candidate_terms_json") or "[]")),
            "evidence": decode_list(str(row.get("evidence_json") or "[]")),
        }
        decision = {
            "adjudication_status": row.get("adjudication_status"),
            "selected_referent": row.get("selected_referent"),
            "evidence_sentence_index": int(row["evidence_sentence_index"]) if str(row.get("evidence_sentence_index") or "").strip() else None,
            "adjudication_source": row.get("adjudication_source"),
        }
        applied = apply_adjudication(str(row.get("claim_text") or ""), fixture, decision)
        output.append({
            "context_eval_id": row.get("context_eval_id"), "article_idx": row.get("article_idx"),
            "sentence_index": row.get("sentence_index"), "claim_text": row.get("claim_text"), **applied,
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="adjudicated 문맥 fixture를 검색 입력 JSONL로 적용")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = read_fixture_rows(args.fixture)
    applied = apply_rows(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in applied), encoding="utf-8")
    print(json.dumps({"fixture_rows": len(rows), "adjudicated_rows": len(applied), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
