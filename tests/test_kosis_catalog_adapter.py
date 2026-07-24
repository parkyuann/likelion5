from src.kosis_catalog_adapter import adapt_record
from src.retrieval_schema import validate_table


def test_v3_record_adapts_dense_sparse_and_dimension_metadata():
    table = adapt_record({
        "table_key": "101:DT_1", "org_id": "101", "org_name": "Statistics", "tbl_id": "DT_1", "tbl_name": "Table",
        "catalog_version": "kosis-catalog-enriched", "meta_status": "enriched",
        "doc_meta_text": "dense text", "doc_item_index": "sparse text",
        "category_paths": [["A", "B"]], "items": [{"itm_id": "T1", "itm_nm": "Item"}],
        "dimensions": [{"obj_id": "A", "obj_nm": "Region", "value_count": 2}],
        "units": ["persons"], "period_types": ["year"],
    })
    assert validate_table(table) == []
    assert table.doc_meta_text == "dense text"
    assert table.doc_item_index == "sparse text"
    assert table.dimensions[0].dimension_id == "A"
    assert table.value_parse_status == "metadata_only"


def test_v4_record_preserves_source_category_path_and_endpoint_provenance():
    table = adapt_record({
        "table_key": "101:DT_V4", "org_id": "101", "tbl_id": "DT_V4", "tbl_name": "V4 table",
        "catalog_version": "kosis-api-catalog-v4", "meta_status": "partial",
        "doc_meta_text": "meta", "doc_item_index": "items", "category_paths": [],
        "category_path_status": "not_found_in_discovery_tree",
        "source_metadata": [{"JOSA_NM": "Official survey"}],
        "api_status": {"TBL": "OK", "SOURCE": "ERROR"},
        "items": [], "dimensions": [{"obj_id": "A", "obj_nm": "Region", "values": [{"value_id": "11", "value_name": "Seoul"}]}],
    })
    assert validate_table(table) == []
    assert table.source_name == "Official survey"
    assert table.category_path_status == "not_found_in_discovery_tree"
    assert table.api_status == {"TBL": "OK", "SOURCE": "ERROR"}
    assert table.value_parse_status == "structured"
