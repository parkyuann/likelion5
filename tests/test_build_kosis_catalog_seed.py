from src.build_kosis_catalog_seed import build_seed


def test_priority_seed_hydrates_missing_category_path_from_discovery_tree():
    tree = {
        "A": {"top_nm": "Top", "leaves": [
            {"org_id": "1", "tbl_id": "P", "tbl_nm": "Priority", "path": ["Top", "Branch"]},
        ]},
    }
    rows, manifest = build_seed(tree, [{"org_id": "1", "tbl_id": "P", "tbl_name": "Priority"}], 0)

    assert rows[0]["category_path"] == ["Top", "Branch"]
    assert manifest["priority_paths_hydrated"] == 1


def test_seed_prioritizes_coverage_gap_and_adds_unique_category_samples():
    tree = {
        "A": {"top_nm": "인구", "leaves": [
            {"org_id": "1", "tbl_id": "P", "tbl_nm": "우선 표"},
            {"org_id": "1", "tbl_id": "A", "tbl_nm": "인구 표"},
        ]},
        "B": {"top_nm": "노동", "leaves": [{"org_id": "2", "tbl_id": "B", "tbl_nm": "노동 표"}]},
    }
    rows, manifest = build_seed(tree, [{"org_id": "1", "tbl_id": "P", "tbl_name": "우선 표"}], 1)
    assert rows[0]["table_key"] == "1:P"
    assert len({row["table_key"] for row in rows}) == 3
    assert manifest["category_selected_counts"] == {"A": 1, "B": 1}
