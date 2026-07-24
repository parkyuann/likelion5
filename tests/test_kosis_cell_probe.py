from src.kosis_cell_probe import probe_profiles


PROFILE = {
    "table_key": "101:DT_TEST", "meta_status": "enriched", "period_types": ["년"],
    "items": [{"itm_id": "T1", "itm_nm": "취업자 수"}],
    "dimensions": [{"obj_id": "A", "obj_nm": "성별", "values": [{"value_id": "0", "value_name": "계"}]}],
    "periods": [{"PRD_SE": "년", "END_PRD_DE": "2025"}],
}


def test_probe_records_query_and_only_safe_response_fields():
    records = probe_profiles([PROFILE], max_probes=1, fetcher=lambda query: [{"DT": "123", "UNIT_NM": "명", "SECRET": "ignore"}])
    assert records[0]["api_status"] == "OK"
    assert records[0]["query"]["obj_levels"] == {"objL1": "0"}
    assert records[0]["first_row"] == {"DT": "123", "UNIT_NM": "명"}


def test_probe_keeps_api_error_as_auditable_status():
    def fail(_: dict):
        raise ConnectionError("network hidden")

    records = probe_profiles([PROFILE], max_probes=1, fetcher=fail)
    assert records[0]["api_status"] == "ERROR"
    assert records[0]["error_type"] == "ConnectionError"
    assert "api_error" not in records[0]


def test_probe_preserves_safe_kosis_runtime_error_detail():
    def fail(_: dict):
        raise RuntimeError("KOSIS API 오류: {'err': 'no data'}")

    record = probe_profiles([PROFILE], max_probes=1, fetcher=fail)[0]
    assert record["api_error"] == "KOSIS API 오류: {'err': 'no data'}"
