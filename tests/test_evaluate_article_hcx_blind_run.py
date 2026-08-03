import json

from src.develop.evaluate_article_hcx_blind_run import evaluate_run


def test_evaluate_run_uses_validated_roles_and_normalized_period(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    validation = {
        "article_idx": "1",
        "validation": {
            "claims": [{
                "claim_index": 0,
                "semantic_claim": {"indicator_norm": "농가 수 감소율"},
                "semantic_validation": {"status": "PASS"},
                "validation": {
                    "claim_status": "PASS",
                    "semantic_role_evidence": {
                        "population_evidence_spans": [
                            {"text": "농가"},
                        ],
                        "item_evidence_spans": [],
                    },
                    "observations": [{
                        "value_span": {"text": "2.5%"},
                        "measurement_type": "CHANGE_RATE",
                        "period_span": {"text": "전년"},
                        "period_normalized": "2024년",
                        "dimension_spans": [],
                    }],
                },
                "scope_validation": {"claim_status": "PASS"},
            }],
        },
    }
    (run_dir / "validation.jsonl").write_text(
        json.dumps(validation, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    gold = [{
        "review_id": "1-001",
        "article_idx": "1",
        "claim_index": 0,
        "target_value": "2.5%",
        "automatic": {},
        "gold": {
            "eligibility": "KOSIS_CANDIDATE",
            "indicator": "농가 수 감소율",
            "measurement_type": "CHANGE_RATE",
            "period": "2024년",
            "population": ["농가"],
            "item": [],
            "dimension": [],
            "value_pairing": "YES",
            "final_status": "CONFIRMED",
        },
    }]

    predictions, report = evaluate_run(gold, run_dir)

    assert predictions[0]["automatic"]["period"] == "2024년"
    assert predictions[0]["automatic"]["population"] == ["농가"]
    assert report["evaluation"]["complete_record"]["six_field_exact_rows"] == 1
    assert report["evaluation"]["routing"]["true_positive"] == 1
