"""Source-grounded gold fixture utilities for the five-article HCX calibration.

The fixture does not encode model output.  It records only adjudicated target
observations and source references, so prompt and contract variants can be
measured against a stable, auditable target.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .article_claim_pipeline import build_span_candidates, sentence_offset_map


MEASUREMENT_TYPES = frozenset({"INDEX_LEVEL", "LEVEL", "CHANGE_RATE", "CHANGE_POINT"})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_saved_articles(run_root: Path) -> dict[str, dict[str, str]]:
    """Load the immutable five-article inputs retained by a completed run."""
    articles: dict[str, dict[str, str]] = {}
    for directory in sorted(run_root.iterdir()):
        input_path = directory / "input.jsonl"
        if not directory.is_dir() or not input_path.exists():
            continue
        rows = load_jsonl(input_path)
        if len(rows) != 1:
            raise ValueError(f"expected one article input record: {input_path}")
        row = rows[0]
        article_idx = str(row.get("article_idx") or "")
        text = str(row.get("article_text") or "")
        if not article_idx or not text:
            raise ValueError(f"article input is incomplete: {input_path}")
        if article_idx in articles:
            raise ValueError(f"duplicate article input: {article_idx}")
        articles[article_idx] = {"title": str(row.get("title") or ""), "article_text": text}
    return articles


def compatible_measurement_types_for_value_span(value_span: dict[str, Any]) -> frozenset[str]:
    """Return the measurement types compatible with a source value unit.

    Percent values deliberately allow both ``LEVEL`` and ``CHANGE_RATE``:
    ``11.7% 연체율`` is a rate level whereas ``전월 대비 0.6% 증가`` is a
    change rate.  The unit therefore constrains the semantic decision but does
    not replace it.
    """
    unit = value_span.get("unit")
    if unit == "지수":
        return frozenset({"INDEX_LEVEL"})
    if unit == "%":
        return frozenset({"LEVEL", "CHANGE_RATE"})
    if unit in {"%p", "포인트"}:
        return frozenset({"CHANGE_POINT"})
    if isinstance(unit, str) and unit:
        return frozenset({"LEVEL"})
    return frozenset()


def _source_text(sentences: dict[int, dict[str, Any]], reference: object, *, field: str, errors: list[str]) -> None:
    if not isinstance(reference, dict):
        errors.append(f"{field}_INVALID")
        return
    text = reference.get("text")
    sentence_id = reference.get("sentence_id")
    sentence = sentences.get(sentence_id) if isinstance(sentence_id, int) else None
    if not isinstance(text, str) or not text:
        errors.append(f"{field}_TEXT_MISSING")
    elif sentence is None:
        errors.append(f"{field}_SENTENCE_UNKNOWN")
    elif text not in sentence["text"]:
        errors.append(f"{field}_NOT_IN_SOURCE_SENTENCE")


def validate_gold_fixture(rows: list[dict[str, Any]], articles: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Validate every gold row against source text and deterministic candidates."""
    reports: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        errors: list[str] = []
        fixture_id = row.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            errors.append("FIXTURE_ID_MISSING")
        elif fixture_id in seen_ids:
            errors.append("FIXTURE_ID_DUPLICATE")
        else:
            seen_ids.add(fixture_id)
        article_idx = str(row.get("article_idx") or "")
        article = articles.get(article_idx)
        if article is None:
            errors.append("ARTICLE_NOT_FOUND")
            reports.append({"fixture_id": fixture_id, "article_idx": article_idx, "status": "INVALID", "errors": errors})
            continue
        measurement_type = row.get("measurement_type")
        if measurement_type not in MEASUREMENT_TYPES:
            errors.append("MEASUREMENT_TYPE_INVALID")
        eligibility = row.get("eligibility")
        if eligibility not in {"KOSIS_CANDIDATE", "EXCLUDED_SOURCE_SCOPE"}:
            errors.append("ELIGIBILITY_INVALID")
        sentences = {item["sentence_id"]: item for item in sentence_offset_map(article["article_text"])}
        value_text = row.get("value_text")
        value_sentence_id = row.get("value_sentence_id")
        sentence = sentences.get(value_sentence_id) if isinstance(value_sentence_id, int) else None
        if not isinstance(value_text, str) or not value_text:
            errors.append("VALUE_TEXT_MISSING")
        elif sentence is None:
            errors.append("VALUE_SENTENCE_UNKNOWN")
        else:
            candidates = build_span_candidates(article["article_text"], [value_sentence_id])
            matches = [item for item in candidates if item.get("kind") == "value_unit" and item.get("text") == value_text]
            if not matches:
                errors.append("VALUE_SPAN_NOT_CANDIDATED")
            elif not any(measurement_type in compatible_measurement_types_for_value_span(item) for item in matches):
                errors.append("MEASUREMENT_TYPE_UNIT_MISMATCH")
        period = row.get("period")
        if period is not None:
            _source_text(sentences, period, field="PERIOD", errors=errors)
        comparisons = row.get("comparison_terms", [])
        if not isinstance(comparisons, list):
            errors.append("COMPARISON_TERMS_INVALID")
        else:
            for comparison in comparisons:
                _source_text(sentences, comparison, field="COMPARISON", errors=errors)
        for dimension in row.get("dimension_texts", []) if isinstance(row.get("dimension_texts", []), list) else []:
            if not isinstance(dimension, str) or not dimension:
                errors.append("DIMENSION_TEXT_INVALID")
            elif sentence is None or dimension not in sentence["text"]:
                errors.append("DIMENSION_NOT_IN_VALUE_SENTENCE")
        reports.append({
            "fixture_id": fixture_id,
            "article_idx": article_idx,
            "status": "PASS" if not errors else "INVALID",
            "errors": errors,
            "eligibility": eligibility,
            "measurement_type": measurement_type,
        })
    return {
        "fixture_rows": len(rows),
        "passed_rows": sum(item["status"] == "PASS" for item in reports),
        "invalid_rows": sum(item["status"] != "PASS" for item in reports),
        "by_eligibility": {
            eligibility: sum(item.get("eligibility") == eligibility for item in reports)
            for eligibility in ("KOSIS_CANDIDATE", "EXCLUDED_SOURCE_SCOPE")
        },
        "by_measurement_type": {
            measurement_type: sum(item.get("measurement_type") == measurement_type for item in reports)
            for measurement_type in sorted(MEASUREMENT_TYPES)
        },
        "reports": reports,
    }
