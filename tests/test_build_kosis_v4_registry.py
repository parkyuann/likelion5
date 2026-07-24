import json

from src.build_kosis_v4_registry import build_registry


def test_registry_merges_tree_and_profile_and_keeps_profile_only_table(tmp_path):
    tree = {
        "A": {"leaves": [
            {"org_id": "1", "tbl_id": "TREE", "tbl_nm": "Tree name", "stat_id": "S1", "path": ["Top", "Branch"]},
        ]},
    }
    profiles = [
        {"table_key": "1:TREE", "org_id": "1", "tbl_id": "TREE", "tbl_name": "API name", "meta_status": "enriched",
         "category_paths": [["Profile path"]], "items": [{"itm_id": "I"}], "dimensions": [], "api_status": {"ITM": "OK"}},
        {"table_key": "2:ONLY", "org_id": "2", "tbl_id": "ONLY", "tbl_name": "Supplement only", "meta_status": "partial",
         "category_paths": [], "category_path_status": "not_found_in_discovery_tree", "items": [], "dimensions": []},
    ]
    output = tmp_path / "registry.jsonl"

    manifest = build_registry(tree, profiles, [{"table_key": "1:TREE"}], output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert manifest["registry_tables"] == 2
    assert manifest["profiles_merged_with_tree"] == 1
    assert manifest["profile_only_tables"] == ["2:ONLY"]
    assert rows[0]["metadata_status"] == "enriched"
    assert rows[0]["category_paths"] == [["Top", "Branch"], ["Profile path"]]
    assert rows[1]["tree_presence"] is False
    assert rows[1]["category_path_status"] == "not_found_in_discovery_tree"
