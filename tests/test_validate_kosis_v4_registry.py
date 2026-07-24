from src.validate_kosis_v4_registry import validate_registry


def test_registry_validation_requires_profile_and_value_index_keys_to_be_preserved():
    registry = [
        {"table_key": "1:T1", "org_id": "1", "tbl_id": "T1", "profile_present": True, "metadata_status": "enriched", "category_path_status": "tree_and_profile"},
        {"table_key": "2:T2", "org_id": "2", "tbl_id": "T2", "profile_present": False, "metadata_status": "registry_only", "category_path_status": "tree_registry"},
    ]
    profiles = [{"table_key": "1:T1"}]
    index = [{"table_key": "1:T1"}]

    report = validate_registry(registry, profiles, index)

    assert report["quality_gate"] == "PASS"
    assert report["metadata_status_counts"] == {"enriched": 1, "registry_only": 1}


def test_registry_validation_rejects_orphan_profile_or_duplicate_key():
    registry = [{"table_key": "1:T1", "org_id": "1", "tbl_id": "T1", "profile_present": False}]
    report = validate_registry(registry, [{"table_key": "1:T1"}], [{"table_key": "9:ORPHAN"}])

    assert report["quality_gate"] == "FAIL"
    assert report["blocking_failure_count"] == 2
