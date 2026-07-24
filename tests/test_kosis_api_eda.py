from pathlib import Path

from src.kosis_api_eda import META_ENDPOINTS, build_profile, collect_profiles, read_jsonl


def test_build_profile_separates_item_ids_from_dimension_value_ids():
    seed = {"table_key": "101:DT_TEST", "org_id": "101", "tbl_id": "DT_TEST", "tbl_name": "테스트", "sample_source": "test"}
    results = {
        "TBL": {"status": "OK", "response": [{"TBL_NM": "실제 표명"}]},
        "ITM": {"status": "OK", "response": [
            {"OBJ_ID": "ITEM", "ITM_ID": "T10", "ITM_NM": "취업자", "UNIT_NM": "천명"},
            {"OBJ_ID": "B", "OBJ_NM": "성별", "ITM_ID": "0", "ITM_NM": "계"},
            {"OBJ_ID": "B", "OBJ_NM": "성별", "ITM_ID": "2", "ITM_NM": "남자"},
        ]},
        "PRD": {"status": "OK", "response": [{"PRD_SE": "월", "STRT_PRD_DE": "202001", "END_PRD_DE": "202601"}]},
        "SOURCE": {"status": "OK", "response": [{"JOSA_NM": "조사"}]},
        "NCD": {"status": "OK", "response": [{"SEND_DE": "2026-07-02"}]},
    }
    profile = build_profile(seed, results)
    assert profile["items"] == [{"item_id": "T10", "item_name": "취업자", "unit_name": "천명", "item_name_eng": ""}]
    assert profile["dimensions"][0]["dimension_id"] == "B"
    assert profile["dimensions"][0]["values"][1]["value_id"] == "2"
    assert profile["latest_change_date"] == "2026-07-02"
    assert profile["profile_status"] == "READY"


def test_collection_records_endpoint_errors_and_reuses_successful_checkpoint(tmp_path: Path):
    calls: list[str] = []

    def fake_get_meta(org_id: str, tbl_id: str, endpoint: str):
        calls.append(endpoint)
        if endpoint == "SOURCE":
            raise RuntimeError("temporary API failure")
        if endpoint == "ITM":
            return [
                {"OBJ_ID": "ITEM", "ITM_ID": "T1", "ITM_NM": "항목", "UNIT_NM": "명"},
                {"OBJ_ID": "A", "OBJ_NM": "지역", "ITM_ID": "11", "ITM_NM": "서울"},
            ]
        if endpoint == "PRD":
            return [{"PRD_SE": "년", "STRT_PRD_DE": "2020", "END_PRD_DE": "2025"}]
        return []

    paths = {
        "raw_output": tmp_path / "raw.jsonl",
        "profile_output": tmp_path / "profiles.jsonl",
        "manifest_output": tmp_path / "manifest.json",
    }
    seeds = [{"org_id": "101", "tbl_id": "DT_TEST", "tbl_name": "테스트"}]
    manifest = collect_profiles(seeds, get_meta_fn=fake_get_meta, pause_seconds=0, **paths)
    assert manifest["new_api_calls"] == len(META_ENDPOINTS)
    assert manifest["endpoint_status_counts"]["SOURCE:ERROR"] == 1
    assert len(read_jsonl(paths["raw_output"])) == len(META_ENDPOINTS)

    calls.clear()
    resumed = collect_profiles(seeds, get_meta_fn=fake_get_meta, pause_seconds=0, **paths)
    assert calls == ["SOURCE"]
    assert resumed["new_api_calls"] == 1
