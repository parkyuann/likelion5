from src.analyze_retrieval_gold_coverage import coverage_metrics


def test_coverage_metrics_separates_catalog_gap_from_ranking_failure():
    matches = [
        {"claim_id": "c1", "gold_table_key": "1:T1"},
        {"claim_id": "c2", "gold_table_key": "1:T2"},
    ]
    ranks = {"c1": {"1:T1": 2}, "c2": {"1:T2": 1}}
    result = coverage_metrics(matches, {"1:T1"}, ranks)
    assert result["catalog_coverage"] == 0.5
    assert result["end_to_end_recall_at"]["1"] == 0.0
    assert result["conditional_recall_at_when_gold_in_catalog"]["5"] == 1.0


def test_coverage_metrics_marks_ranking_unavailable_when_no_gold_is_indexed():
    matches = [{"claim_id": "c1", "gold_table_key": "1:T1"}]
    result = coverage_metrics(matches, set(), {"c1": {}})
    assert result["catalog_coverage"] == 0.0
    assert result["conditional_recall_at_when_gold_in_catalog"] is None
    assert result["ranking_metric_available"] is False
