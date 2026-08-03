import json

from src.develop.audit_r16i_layering import audit_r16i_run


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_audit_r16i_run_counts_scope_and_model_ambiguous_blocks(tmp_path):
    _write_jsonl(tmp_path / "scope_validation.jsonl", [
        {
            "scope_validation": {
                "claim_status": "BLOCKED",
                "errors": ["INDICATOR_ANCHOR_NOT_FOUND_IN_VALUE_SENTENCE"],
                "observations": [{
                    "status": "BLOCKED",
                    "errors": ["VALUE_OUTSIDE_INDICATOR_SCOPE"],
                }],
            },
        },
        {
            "scope_validation": {
                "claim_status": "PASS",
                "errors": [],
                "observations": [{"status": "PASS", "errors": []}],
            },
        },
    ])
    _write_jsonl(tmp_path / "semantic_validation.jsonl", [{
        "article_idx": "2703",
        "claim_index": 2,
        "semantic_claim_effective": {
            "classification_reason": "INSUFFICIENT_CONTEXT",
            "candidate_coverage_source": "HCX",
            "candidate_class_override": None,
            "target_value_span_ids": ["s1:value_unit:0-4"],
        },
        "semantic_validation": {
            "errors": ["CANDIDATE_CLASS_AMBIGUOUS"],
        },
    }])
    _write_jsonl(tmp_path / "gold_predictions.jsonl", [
        {
            "review_id": "2703-V1",
            "article_idx": "2703",
            "candidate_span_id": "s1:value_unit:0-4",
            "gold": {"eligibility": "KOSIS_CANDIDATE"},
            "automatic": {"action": "BLOCKED"},
            "prediction": {"detected": True, "claim_index": 2},
        },
        {
            "review_id": "2703-V2",
            "article_idx": "2703",
            "candidate_span_id": "s9:value_unit:0-4",
            "gold": {"eligibility": "OUT_OF_SCOPE"},
            "automatic": {"action": "BLOCKED"},
            "prediction": {"detected": True, "claim_index": 3},
        },
    ])
    retry_root = tmp_path / "retry"
    retry_root.mkdir()
    _write_jsonl(retry_root / "raw.jsonl", [{
        "article_idx": "2703",
        "semantic_prediction": {
            "claims": [{
                "candidate_class": "KOSIS_CANDIDATE",
                "candidate_coverage_source": "HCX",
                "target_value_span_ids": ["s1:value_unit:0-4"],
            }],
        },
    }])
    scope_rows = [
        json.loads(line)
        for line in (tmp_path / "scope_validation.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    scope_rows[0]["article_idx"] = "2703"
    scope_rows[0]["claim_index"] = 2
    scope_rows.append({
        "article_idx": "2703",
        "claim_index": 3,
        "scope_validation": {
            "claim_status": "BLOCKED",
            "errors": ["PRIVATE_SOURCE_CONTEXT_OUT_OF_SCOPE"],
            "observations": [],
        },
    })
    _write_jsonl(tmp_path / "scope_validation.jsonl", scope_rows)

    report = audit_r16i_run(
        tmp_path,
        gold_predictions_path=tmp_path / "gold_predictions.jsonl",
        retry_run_roots=[retry_root],
    )

    assert report["scope"]["rows"] == 3
    assert report["scope"]["blocked_rows"] == 2
    assert dict(report["scope"]["claim_errors"]) == {
        "INDICATOR_ANCHOR_NOT_FOUND_IN_VALUE_SENTENCE": 1,
        "PRIVATE_SOURCE_CONTEXT_OUT_OF_SCOPE": 1,
    }
    assert report["scope"]["observation_errors"] == [
        ("VALUE_OUTSIDE_INDICATOR_SCOPE", 1),
    ]
    assert report["ambiguous_hard_blocks"]["rows"] == 1
    assert report["ambiguous_hard_blocks"]["model_only_rows"] == 1
    attribution = {
        row["block_code"]: row
        for row in report["gold_attribution"]["by_block_code"]
    }
    assert attribution[
        "INDICATOR_ANCHOR_NOT_FOUND_IN_VALUE_SENTENCE"
    ]["gold_kosis_lost"] == 1
    assert attribution[
        "PRIVATE_SOURCE_CONTEXT_OUT_OF_SCOPE"
    ]["justified_non_kosis_block"] == 1
    assert report["ambiguous_retry_audit"]["resolved_non_ambiguous"] == 1
    assert report["ambiguous_retry_audit"]["still_ambiguous"] == 0
    assert report["ambiguous_retry_audit"]["missing_from_retry"] == 0
    assert report["ambiguous_retry_audit"][
        "current_candidate_class_ambiguous"
    ] == 0
