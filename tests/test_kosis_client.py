from unittest.mock import Mock, patch

from src import kosis_client


def test_get_data_uses_official_table_selection_endpoint_and_period():
    response = Mock()
    response.json.return_value = [{"DT": "123"}]
    response.raise_for_status.return_value = None
    with patch.object(kosis_client.requests, "get", return_value=response) as get:
        result = kosis_client.get_data("101", "DT_TEST", "11", "T1", prd_se="M", start_prd_de="202605", end_prd_de="202605", objL2="0")
    assert result == [{"DT": "123"}]
    assert get.call_args.args[0] == "https://kosis.kr/openapi/Param/statisticsParameterData.do"
    params = get.call_args.kwargs["params"]
    assert params["objL1"] == "11"
    assert params["objL2"] == "0"
    assert params["objL8"] == ""
    assert params["startPrdDe"] == "202605"


def test_get_data_from_query_forwards_alignment_query_without_api_key():
    query = {
        "org_id": "101", "tbl_id": "DT_TEST", "itm_id": "T1", "prd_se": "M",
        "start_prd_de": "202605", "end_prd_de": "202605",
        "obj_levels": {"objL1": "11", "objL2": "0"},
    }
    with patch.object(kosis_client, "get_data", return_value=[]) as get_data:
        assert kosis_client.get_data_from_query(query) == []
    assert get_data.call_args.args == ("101", "DT_TEST", "11", "T1")
    assert get_data.call_args.kwargs["objL2"] == "0"
