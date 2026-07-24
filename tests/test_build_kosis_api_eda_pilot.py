from src.build_kosis_api_eda_pilot import build_pilot


def test_build_pilot_selects_unique_deterministic_tables_per_category():
    tree = {
        "A": {"top_nm": "인구", "leaves": [
            {"org_id": "1", "tbl_id": "T1", "tbl_nm": "첫 표"},
            {"org_id": "1", "tbl_id": "T2", "tbl_nm": "둘 표"},
        ]},
        "B": {"top_nm": "노동", "leaves": [
            {"org_id": "1", "tbl_id": "T2", "tbl_nm": "중복 표"},
            {"org_id": "2", "tbl_id": "T3", "tbl_nm": "셋 표"},
        ]},
    }
    first_rows, first_manifest = build_pilot(tree, per_category=2)
    second_rows, second_manifest = build_pilot(tree, per_category=2)
    assert first_rows == second_rows
    assert {row["table_key"] for row in first_rows} == {"1:T1", "1:T2", "2:T3"}
    assert first_manifest == second_manifest
    assert first_manifest["category_selected_counts"] == {"A": 2, "B": 1}
