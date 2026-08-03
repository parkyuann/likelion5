from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.develop.apply_article_hcx_blind_review import (
    REVIEW_HEADERS,
    evaluate_records,
    parse_review_snapshot,
    validate_against_run,
)


def _matrix_row(
    review_id: str,
    *,
    action: str = "PASS",
    eligibility: str = "KOSIS_CANDIDATE",
    pairing: str = "YES",
    final_status: str = "CONFIRMED",
) -> list[object]:
    article_idx, suffix = review_id.split("-")
    return [
        review_id,
        article_idx,
        "기사 제목",
        int(suffix) - 1,
        "지난해 인구는 10명이었다.",
        "10명",
        "인구",
        "LEVEL",
        "지난해",
        None,
        None,
        None,
        "PASS",
        "PASS",
        action,
        action,
        eligibility,
        "인구",
        "LEVEL",
        "지난해",
        "없음",
        "없음",
        "없음",
        pairing,
        final_status,
        "검토 완료",
    ]


def _snapshot(rows: list[list[object]]) -> dict[str, object]:
    return {
        "workbook_path": "reviewed.xlsx",
        "workbook_sha256": "abc",
        "review_matrix": [
            ["title"] + [None] * 25,
            ["note"] + [None] * 25,
            [None] * 26,
            REVIEW_HEADERS,
            *rows,
        ],
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_parse_review_snapshot_requires_all_40_rows() -> None:
    with pytest.raises(ValueError, match="expected 40"):
        parse_review_snapshot(_snapshot([_matrix_row("1-001")]))


def test_evaluate_records_separates_routing_and_field_accuracy() -> None:
    matrix_rows = [_matrix_row(f"1-{index:03d}") for index in range(1, 41)]
    matrix_rows[0][16] = "OUT_OF_SCOPE"
    matrix_rows[0][24] = "REJECTED"
    matrix_rows[1][15] = "BLOCKED"
    matrix_rows[1][14] = "BLOCKED"
    matrix_rows[2][18] = "CHANGE_POINT"
    records = parse_review_snapshot(_snapshot(matrix_rows))

    report = evaluate_records(records)

    assert report["routing"]["true_positive"] == 38
    assert report["routing"]["false_positive"] == 1
    assert report["routing"]["false_negative"] == 1
    assert report["field_metrics_all_selected"]["measurement_type"]["correct"] == 39
    assert report["abstention"]["blocked_routable_after_correction"] == 1
    assert report["claim_detection_recall"]["status"] == "NOT_MEASURABLE"


def test_validate_against_run_checks_target_and_statuses(tmp_path: Path) -> None:
    records = parse_review_snapshot(
        _snapshot([_matrix_row(f"1-{index:03d}") for index in range(1, 41)])
    )
    _write_jsonl(
        tmp_path / "input.jsonl",
        [{
            "article_idx": "1",
            "title": "기사 제목",
            "article_text": "지난해 인구는 10명이었다.",
        }],
    )
    scope_rows = []
    semantic_rows = []
    candidate_rows = []
    validation_claims = []
    for claim_index in range(40):
        scope_rows.append({
            "article_idx": "1",
            "claim_index": claim_index,
            "scope_validation": {"claim_status": "PASS"},
        })
        semantic_rows.append({
            "article_idx": "1",
            "claim_index": claim_index,
            "semantic_validation": {"status": "PASS"},
        })
        candidate_rows.append({
            "article_idx": "1",
            "claim_index": claim_index,
            "candidates": [{"kind": "value_unit", "text": "10명"}],
        })
        validation_claims.append({
            "claim_index": claim_index,
            "validation": {"claim_status": "PASS"},
        })
    _write_jsonl(tmp_path / "scope_validation.jsonl", scope_rows)
    _write_jsonl(tmp_path / "semantic_validation.jsonl", semantic_rows)
    _write_jsonl(tmp_path / "span_candidates.jsonl", candidate_rows)
    _write_jsonl(
        tmp_path / "validation.jsonl",
        [{"article_idx": "1", "validation": {"claims": validation_claims}}],
    )

    result = validate_against_run(records, tmp_path)

    assert result == {
        "status": "PASS",
        "review_rows": 40,
        "unique_articles": 1,
        "grounded_target_rows": 40,
        "empty_target_rows": 0,
        "integrity_errors": 0,
    }
