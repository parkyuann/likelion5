import pytest

from src.claim_table_aligner import align_profile, build_cell_query, build_probe_alignment


PROFILE = {
    "org_id": "101", "tbl_id": "DT_TEST", "period_types": ["월", "년"],
    "items": [{"itm_id": "T1", "itm_nm": "취업자", "unit_nm": "명"}],
    "dimensions": [
        {"obj_id": "A", "obj_nm": "지역", "values": [{"value_id": "11", "value_name": "서울"}, {"value_id": "00", "value_name": "전국"}]},
        {"obj_id": "B", "obj_nm": "성별", "values": [{"value_id": "0", "value_name": "계"}, {"value_id": "1", "value_name": "남자"}]},
    ],
    "periods": [{"PRD_SE": "월", "END_PRD_DE": "2026.05"}],
}


def test_alignment_uses_exact_dimension_value_and_safe_total_default():
    alignment = align_profile(PROFILE, item_term="취업자", dimension_terms={"지역": "서울"}, period="2026-05", period_type="월")
    assert alignment["align_status"] == "ALIGNED"
    assert alignment["matched_dimensions"] == {"A": "11", "B": "0"}
    assert alignment["defaulted_dimensions"] == {"B": "계"}
    assert build_cell_query(PROFILE, alignment)["obj_levels"] == {"objL1": "11", "objL2": "0"}


def test_alignment_stops_when_dimension_is_not_explicit_or_safely_defaultable():
    profile = {**PROFILE, "dimensions": [{"obj_id": "A", "obj_nm": "지역", "values": [{"value_id": "11", "value_name": "서울"}, {"value_id": "26", "value_name": "부산"}]}]}
    result = align_profile(profile, item_term="취업자", period="2026-05", period_type="월")
    assert result["align_status"] == "DIM_MISSING"
    with pytest.raises(ValueError):
        build_cell_query(profile, result)


def test_probe_alignment_uses_latest_supported_period():
    alignment = build_probe_alignment(PROFILE)
    assert alignment["align_status"] == "ALIGNED"
    assert alignment["matched_period"] == "202605"
