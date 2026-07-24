from src.validate_kosis_catalog_v4 import validate_v4


def _catalog(table_key="1:T1", status="enriched"):
    return {
        "table_key": table_key, "meta_status": status, "category_path_status": "present",
        "category_paths": [["A"]],
        "dimensions": [{"obj_id": "REGION", "values": [{"value_id": "11", "value_name": "Seoul"}]}],
    }


def _raw(table_key="1:T1", source_status="OK"):
    return [
        {"table_key": table_key, "endpoint": endpoint, "status": source_status if endpoint == "SOURCE" else "OK"}
        for endpoint in ("TBL", "ITM", "PRD", "SOURCE", "NCD")
    ]


def test_v4_quality_gate_allows_source_no_data_but_requires_complete_core_artifacts():
    report = validate_v4(
        [{"table_key": "1:T1", "sample_source": "provisional_gold_coverage_gap"}],
        [_catalog()], _raw(source_status="ERROR"),
        [{"table_key": "1:T1", "obj_id": "REGION", "value_id": "11"}],
    )
    assert report["quality_gate"] == "PASS"
    assert report["known_optional_source_gap_count"] == 1
    assert report["priority_coverage"]["enriched_tables"] == 1


def test_v4_quality_gate_rejects_missing_side_index_and_checkpoint():
    report = validate_v4(
        [{"table_key": "1:T1"}], [_catalog()], _raw()[:-1], [],
    )
    assert report["quality_gate"] == "FAIL"
    assert report["blocking_failure_count"] == 2
    assert report["side_index"]["missing_values"] == 1
