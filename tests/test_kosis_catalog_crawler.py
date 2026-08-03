import json

import pytest

from src.kosis_catalog_crawler import (
    KosisOpenAPI, canonical_seed, crawl_progress, endpoint_record, get_api_key, latest_records,
    make_catalog_record, make_value_side_index, read_jsonl, split_itm_rows,
)


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return []


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _Response()


def test_metadata_endpoint_uses_get_meta_not_list_method():
    api = KosisOpenAPI("test-key", timeout=1, retries=0, backoff_seconds=0)
    fake = _Session()
    api.session = fake
    api.get_meta("101", "DT_TEST", "ITM")
    assert fake.calls[0]["params"]["method"] == "getMeta"
    api.list_children("A")
    assert fake.calls[1]["params"]["method"] == "getList"


def test_api_key_falls_back_to_dotenv_without_external_package(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# desktop settings\nKOSIS_API_KEY='from-dotenv'\n", encoding="utf-8")
    monkeypatch.delenv("KOSIS_API_KEY", raising=False)
    assert get_api_key(None, env_file) == "from-dotenv"


def test_api_itm_rows_become_items_and_structured_dimension_values():
    items, dimensions = split_itm_rows([
        {"OBJ_ID": "ITEM", "ITM_ID": "T1", "ITM_NM": "취업자", "UNIT_NM": "천명"},
        {"OBJ_ID": "B", "OBJ_NM": "성별", "ITM_ID": "0", "ITM_NM": "계"},
        {"OBJ_ID": "B", "OBJ_NM": "성별", "ITM_ID": "2", "ITM_NM": "남자"},
    ])
    assert items[0]["itm_id"] == "T1"
    assert dimensions[0]["obj_id"] == "B"
    assert dimensions[0]["values"][1]["value_id"] == "2"


def test_catalog_record_keeps_values_out_of_bm25_document_and_in_side_index():
    seed = {"table_key": "101:DT_TEST", "org_id": "101", "tbl_id": "DT_TEST", "tbl_name": "기존 표", "stat_id": "", "category_path": ["노동"], "sample_source": "test"}
    results = {
        "TBL": {"status": "OK", "response": [{"TBL_NM": "성별 고용"}]},
        "ITM": {"status": "OK", "response": [
            {"OBJ_ID": "ITEM", "ITM_ID": "T1", "ITM_NM": "취업자", "UNIT_NM": "천명"},
            {"OBJ_ID": "B", "OBJ_NM": "성별", "ITM_ID": "2", "ITM_NM": "남자"},
        ]},
        "PRD": {"status": "OK", "response": [{"PRD_SE": "월", "END_PRD_DE": "202606"}]},
        "SOURCE": {"status": "OK", "response": [{"STAT_ID": "1962", "JOSA_NM": "경제활동인구조사"}]},
        "NCD": {"status": "OK", "response": {"latest_send_date": "2026-07-02"}},
    }
    record = make_catalog_record(seed, results)
    assert record["meta_status"] == "enriched"
    assert "남자" not in record["doc_item_index"]
    side_index = make_value_side_index(record)
    assert side_index[0]["normalized_value"] == "남자"
    assert side_index[0]["value_id"] == "2"
    assert record["metadata_readiness"] == {"cell_query_ready": True, "missing": []}


def test_catalog_record_does_not_mark_empty_required_metadata_as_enriched():
    seed = {"table_key": "101:DT_TEST", "org_id": "101", "tbl_id": "DT_TEST", "tbl_name": "", "stat_id": "", "category_path": [], "sample_source": "test"}
    results = {endpoint: {"status": "OK", "response": []} for endpoint in ("TBL", "ITM", "PRD", "SOURCE")}
    results["NCD"] = {"status": "OK", "response": {}}
    record = make_catalog_record(seed, results)
    assert record["meta_status"] == "partial"
    assert set(record["metadata_readiness"]["missing"]) == {"table_name", "items", "dimensions", "periods"}


def test_checkpoint_error_redacts_api_key_and_query_parameter():
    class _FailingAPI:
        api_key = "secret-value"

        def get_meta(self, *_):
            raise ConnectionError("https://kosis.kr/?apiKey=secret-value&tblId=DT_TEST")

    record = endpoint_record({"table_key": "101:DT_TEST", "org_id": "101", "tbl_id": "DT_TEST"}, "ITM", _FailingAPI())
    assert record["status"] == "ERROR"
    assert "secret-value" not in record["error_message"]
    assert "apiKey=***" in record["error_message"]


def test_seed_key_mismatch_and_duplicate_checkpoint_order_are_detected(tmp_path):
    with pytest.raises(ValueError, match="table_key mismatch"):
        canonical_seed({"table_key": "101:WRONG", "org_id": "101", "tbl_id": "RIGHT"})
    latest = latest_records([
        {"table_key": "101:T", "endpoint": "ITM", "status": "OK", "retrieved_at": "2026-07-24T10:00:00+00:00"},
        {"table_key": "101:T", "endpoint": "ITM", "status": "ERROR", "retrieved_at": "2026-07-24T09:00:00+00:00"},
    ])
    assert latest[("101:T", "ITM")]["status"] == "OK"
    raw = tmp_path / "raw.jsonl"
    raw.write_text('{"table_key":"1:T"}\n{"table_key":', encoding="utf-8")
    assert read_jsonl(raw) == [{"table_key": "1:T"}]


def test_progress_uses_latest_raw_endpoint_record_and_counts_errors(tmp_path):
    seeds = tmp_path / "seeds.jsonl"
    raw = tmp_path / "raw.jsonl"
    seed_rows = [
        {"org_id": "101", "tbl_id": "FIRST"},
        {"org_id": "101", "tbl_id": "SECOND"},
    ]
    raw_rows = [
        {"table_key": "101:FIRST", "endpoint": endpoint, "status": "OK"}
        for endpoint in ("TBL", "ITM", "PRD", "SOURCE", "NCD")
    ] + [
        {"table_key": "101:SECOND", "endpoint": "TBL", "status": "ERROR"},
        {"table_key": "101:SECOND", "endpoint": "TBL", "status": "OK"},
        {"table_key": "101:SECOND", "endpoint": "ITM", "status": "ERROR"},
    ]
    seeds.write_text("".join(json.dumps(row) + "\n" for row in seed_rows), encoding="utf-8")
    raw.write_text("".join(json.dumps(row) + "\n" for row in raw_rows), encoding="utf-8")

    progress = crawl_progress(seeds_path=seeds, raw_output=raw)

    assert progress["tables"] == {
        "total": 2, "enriched": 1, "partial": 0, "error": 1, "not_started": 0, "completion_percent": 50.0,
    }
    assert progress["endpoint_status_counts"]["TBL"] == {"OK": 2}
    assert progress["endpoint_status_counts"]["ITM"] == {"ERROR": 1, "OK": 1}
