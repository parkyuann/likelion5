from src.validate_kosis_catalog import validate


def test_catalog_validation_reports_duplicate_and_key_mismatch():
    rows = [
        {"table_key": "1:T1", "org_id": "1", "tbl_id": "T1", "tbl_name": "A", "catalog_version": "v3",
         "doc_meta_text": "A", "doc_item_index": "A", "items": [], "dimensions": [], "period_types": []},
        {"table_key": "bad", "org_id": "1", "tbl_id": "T1", "tbl_name": "B", "catalog_version": "v3",
         "doc_meta_text": "B", "doc_item_index": "B", "items": [], "dimensions": [], "period_types": []},
        {"table_key": "1:T1", "org_id": "1", "tbl_id": "T1", "tbl_name": "C", "catalog_version": "v3",
         "doc_meta_text": "C", "doc_item_index": "C", "items": [], "dimensions": [], "period_types": []},
    ]
    manifest, failures = validate(rows, [])
    assert manifest["duplicate_table_keys"] == 1
    assert any(item["reason"] == "table_key_mismatch" for item in failures)
    assert any(item["reason"] == "duplicate_table_key" for item in failures)
