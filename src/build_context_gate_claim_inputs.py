"""문맥·매핑 게이트를 통과한 claim에 규칙 구조화 힌트를 다시 붙인다.

출력의 ``indicator_raw``/``dimension_json``/``period``은 자동 추출값이다. 사람
gold나 최종 셀 선택값이 아니며, Top-K 후보 profile 정렬의 입력·감사 용도로만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .claim_extractor import TIME_RE, extract_from_sentence, extract_structured_context
    from .claim_normalizer import normalize_time_ref
    from .apply_context_referent_adjudications import read_fixture_rows
except ImportError:  # pragma: no cover - standalone CLI support
    from claim_extractor import TIME_RE, extract_from_sentence, extract_structured_context
    from claim_normalizer import normalize_time_ref
    from apply_context_referent_adjudications import read_fixture_rows


ELIGIBLE = {"CONTEXT_EXPANDED", "CLAIM_ONLY_SAFE"}
YEAR_MONTH_RE = re.compile(r"(?P<year>19\d{2}|20\d{2})[년.\-/ ]+(?P<month>\d{1,2})월?")
YEAR_RE = re.compile(r"(?P<year>19\d{2}|20\d{2})년?")
QUARTER_RE = re.compile(r"(?P<quarter>[1-4])분기")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalized_period(raw: str | None, published_at: str | None) -> tuple[str | None, str | None]:
    if not raw or not published_at:
        return None, None
    absolute = normalize_time_ref(raw, published_at)
    if not absolute:
        return None, None
    quarter = QUARTER_RE.search(absolute)
    if quarter:
        year = YEAR_RE.search(absolute)
        return (f"{year.group('year')}-Q{quarter['quarter']}", "분기") if year else (None, None)
    month = YEAR_MONTH_RE.search(absolute)
    if month:
        return f"{month['year']}-{int(month['month']):02d}", "월"
    year = YEAR_RE.search(absolute)
    return (year.group("year"), "년") if year else (None, None)


def context_sources(record: dict[str, Any], context_row: dict[str, str] | None) -> list[tuple[int, str, str]]:
    """claim 외 제목·직전 문장을 근거 후보로 보존한다."""
    if not context_row:
        return []
    sources: list[tuple[int, str, str]] = []
    title = str(context_row.get("article_title") or "")
    if title:
        sources.append((-1, title, "article_title"))
    try:
        window = json.loads(str(context_row.get("context_window_json") or "[]"))
    except json.JSONDecodeError:
        window = []
    claim_index = str(record.get("sentence_index") or "")
    for entry in window:
        if not isinstance(entry, dict) or str(entry.get("sentence_index")) == claim_index:
            continue
        text = str(entry.get("text") or "")
        if text:
            sources.append((int(entry.get("sentence_index", -1)), text, "article_context"))
    return sources


def unique_context_value(candidates: list[tuple[str, dict[str, Any]]]) -> tuple[str | None, list[dict[str, Any]]]:
    usable = [(value, evidence) for value, evidence in candidates if value]
    values = {value for value, _ in usable}
    return (next(iter(values)) if len(values) == 1 else None), [evidence for _, evidence in usable]


def enrich_records(
    records: list[dict[str, Any]], article_dates: dict[str, str], context_rows: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """eligible claim만 규칙 구조화 힌트를 붙이고, 다른 게이트는 출력하지 않는다."""
    output: list[dict[str, Any]] = []
    for record in records:
        if record.get("mapping_eligibility") not in ELIGIBLE:
            continue
        item = json.loads(json.dumps(record, ensure_ascii=False))
        extracted = extract_from_sentence(str(item.get("claim_text") or "")) or {}
        article_idx = str(item.get("article_idx") or "")
        period, period_type = normalized_period(extracted.get("time_ref"), article_dates.get(article_idx))
        sources = context_sources(item, (context_rows or {}).get(str(item.get("context_eval_id") or "")))
        context_structures = [(index, source, extract_structured_context(text), text) for index, text, source in sources]
        indicator = extracted.get("indicator_raw")
        indicator_evidence: list[dict[str, Any]] = []
        if not indicator:
            indicator, indicator_evidence = unique_context_value([
                (str(structure.get("indicator_raw") or ""), {"sentence_index": index, "source": source, "text": text})
                for index, source, structure, text in context_structures
            ])
        dimensions = extracted.get("dimension_json") if isinstance(extracted.get("dimension_json"), dict) else {}
        dimensions = dict(dimensions)
        dimension_evidence: dict[str, list[dict[str, Any]]] = {}
        for key in ("지역", "성별", "연령"):
            if dimensions.get(key):
                continue
            candidates: list[tuple[str, dict[str, Any]]] = []
            for index, source, structure, text in context_structures:
                for value in structure.get("dimension_json", {}).get(key, []):
                    if isinstance(value, dict):
                        candidates.append((str(value.get("normalized") or value.get("raw") or ""), {"sentence_index": index, "source": source, "text": text}))
            chosen, evidence = unique_context_value(candidates)
            if chosen:
                dimensions[key] = [{"raw": chosen, "normalized": chosen, "source_span": chosen, "start": -1, "end": -1}]
                dimension_evidence[key] = evidence
        period_evidence: list[dict[str, Any]] = []
        if not period:
            candidates = []
            for index, source, _, text in context_structures:
                for raw_time in TIME_RE.findall(text):
                    normalized, normalized_type = normalized_period(raw_time, article_dates.get(article_idx))
                    if normalized and normalized_type:
                        candidates.append((f"{normalized}|{normalized_type}", {"sentence_index": index, "source": source, "text": text, "raw_time": raw_time}))
            chosen, period_evidence = unique_context_value(candidates)
            if chosen:
                period, period_type = chosen.split("|", 1)
        item.update({
            "indicator_raw": indicator,
            "population_raw": extracted.get("population_raw"),
            "dimension_json": dimensions,
            "period": period, "period_type": period_type,
            "auto_structure_audit": {
                "source": "claim_extractor_rule_reextraction_with_context",
                "time_ref_raw": extracted.get("time_ref"), "published_at": article_dates.get(article_idx),
                "context_indicator_evidence": indicator_evidence, "context_period_evidence": period_evidence,
                "context_dimension_evidence": dimension_evidence, "is_human_gold": False,
            },
        })
        output.append(item)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="문맥 gate 통과 claim에 자동 구조화 힌트 연결")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--reviewed-context", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    articles = pd.read_csv(args.articles, dtype=str, keep_default_na=False)
    article_dates = {str(index): str(row.get("작성일") or "") for index, row in articles.iterrows()}
    context_rows = None
    if args.reviewed_context:
        context_rows = {str(row.get("context_eval_id") or ""): row for row in read_fixture_rows(args.reviewed_context)}
    rows = enrich_records(read_jsonl(args.input), article_dates, context_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({
        "eligible_rows": len(rows), "with_indicator": sum(bool(row.get("indicator_raw")) for row in rows),
        "with_period": sum(bool(row.get("period")) for row in rows),
        "output": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
