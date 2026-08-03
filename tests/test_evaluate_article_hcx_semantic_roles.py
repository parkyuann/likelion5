from __future__ import annotations

import json
from pathlib import Path

from src.develop.evaluate_article_hcx_semantic_roles import evaluate_semantic_roles


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_evaluate_semantic_roles_reports_raw_and_validated_metrics(tmp_path: Path) -> None:
    gold = [
        {
            "fixture_id": "f1",
            "article_idx": "1",
            "eligibility": "KOSIS_CANDIDATE",
            "value_text": "1.0%",
            "value_sentence_id": 0,
            "semantic_role_gold": {
                "adjudication_status": "CONFIRMED",
                "target_value_candidate_ids": ["v1"],
                "population_evidence_candidate_ids": ["p1"],
                "item_evidence_candidate_ids": ["i1"],
                "dimension_candidate_ids": ["d1"],
            },
        },
        {
            "fixture_id": "f2",
            "article_idx": "1",
            "eligibility": "KOSIS_CANDIDATE",
            "value_text": "2.0%",
            "value_sentence_id": 1,
            "semantic_role_gold": {
                "adjudication_status": "CONFIRMED",
                "target_value_candidate_ids": ["v2"],
                "population_evidence_candidate_ids": [],
                "item_evidence_candidate_ids": [],
                "dimension_candidate_ids": [],
            },
        },
    ]
    _write_jsonl(tmp_path / "span_candidates.jsonl", [{
        "article_idx": "1",
        "claim_index": 0,
        "candidate_filter": {"target_value_span_ids": ["v1"]},
    }])
    _write_jsonl(tmp_path / "bindings.jsonl", [{
        "article_idx": "1",
        "claim_index": 0,
        "binding": {
            "population_evidence_span_ids": ["p1"],
            "item_evidence_span_ids": ["wrong_item"],
            "observations": [{
                "value_span_id": "v1",
                "dimension_span_ids": ["d1"],
            }],
        },
    }])
    _write_jsonl(tmp_path / "pass_observations.jsonl", [{
        "article_idx": "1",
        "validation": {
            "value_span": {"span_id": "v1"},
            "dimension_spans": [],
        },
        "semantic_role_evidence": {
            "population_evidence_spans": [{"span_id": "p1"}],
            "item_evidence_spans": [{"span_id": "i1"}],
        },
    }])

    result = evaluate_semantic_roles(tmp_path, gold)

    raw = result["stages"]["raw_binding"]
    assert raw["covered_rows"] == 1
    assert raw["end_to_end"]["roles"]["population"]["precision"] == 1.0
    assert raw["end_to_end"]["roles"]["item"]["true_positive"] == 0
    assert raw["end_to_end"]["roles"]["item"]["false_positive"] == 1
    assert raw["end_to_end"]["roles"]["item"]["false_negative"] == 1
    assert raw["end_to_end"]["roles"]["dimension"]["recall"] == 1.0

    validated = result["stages"]["validated_pass"]
    assert validated["covered_rows"] == 1
    assert validated["end_to_end"]["roles"]["item"]["f1"] == 1.0
    assert validated["end_to_end"]["roles"]["dimension"]["false_negative"] == 1
