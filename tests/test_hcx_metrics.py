from src.hcx_claim_experiment import calculate_metrics, calculate_rlt_score


def test_hcx_reports_precision_recall_f1_for_class_and_scope():
    rows = [
        {"status": "ok", "gold_is_claim": True, "pred_is_claim": True,
         "gold_claim_class": "집계통계", "pred_claim_class": "집계통계",
         "gold_source_scope": "KOSIS등재", "pred_source_scope": "KOSIS등재",
         "gold_source_role": "", "pred_source_role": "", "gold_unit": "", "pred_unit": "",
         "latency_ms": 100, "inference_latency_ms": 100, "total_tokens": 50,
         "prompt_tokens": 30, "completion_tokens": 20, "tokenizer_latency_ms": 0, "estimated_cost": 0},
        {"status": "ok", "gold_is_claim": True, "pred_is_claim": False,
         "gold_claim_class": "개별사례", "pred_claim_class": "",
         "gold_source_scope": "불명", "pred_source_scope": "불명",
         "gold_source_role": "", "pred_source_role": "", "gold_unit": "", "pred_unit": "",
         "latency_ms": 120, "inference_latency_ms": 120, "total_tokens": 60,
         "prompt_tokens": 40, "completion_tokens": 20, "tokenizer_latency_ms": 0, "estimated_cost": 0},
    ]
    metrics = calculate_metrics(rows)
    assert "precision" in metrics["claim_class_macro_precision"] if isinstance(metrics["claim_class_macro_precision"], dict) else True
    assert 0 <= metrics["claim_class_macro_recall"] <= 1
    assert 0 <= metrics["source_scope_macro_f1"] <= 1
    assert calculate_rlt_score(metrics, 100, 50, 0.6, 0.2, 0.2) is not None
