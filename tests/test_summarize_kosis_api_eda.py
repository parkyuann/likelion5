from src.summarize_kosis_api_eda import summarize


def test_api_eda_summary_reports_cardinality_unit_and_endpoint_statuses():
    profiles = [{
        "profile_status": "READY",
        "items": [{"unit_name": "%"}, {"unit_name": ""}],
        "dimensions": [
            {"dimension_name": "성별", "values": [{}, {}]},
            {"dimension_name": "지역", "values": [{}] * 300},
        ],
        "periods": [{"PRD_SE": "월"}],
    }]
    raw = [
        {"endpoint": "ITM", "status": "OK", "latency_ms": 100.5},
        {"endpoint": "SOURCE", "status": "ERROR", "latency_ms": 200.2},
    ]
    result = summarize(profiles, raw)
    assert result["item_unit_missing"] == 1
    assert result["dimension_cardinality_bins"] == {"1_32": 1, "33_256": 0, "over_256": 1}
    assert result["endpoint_status_counts"]["SOURCE:ERROR"] == 1
