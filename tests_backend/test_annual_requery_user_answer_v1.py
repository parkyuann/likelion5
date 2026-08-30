from __future__ import annotations

from decimal import Decimal
import sys
import types
from pathlib import Path


RUNTIME_ROOT = Path(__file__).parents[1].resolve() / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

_operational_stub = types.ModuleType("src.develop.run_pipeline_operational_v2")
_operational_stub.compare_official_cell = lambda *args, **kwargs: {
    "verdict": "VERIFIED", "reason": "MATCH",
}


def _fetch_stub(query_plan, fetcher):
    response = fetcher(dict(query_plan))
    return {
        "status": "CELL_RESOLVED",
        "query": dict(query_plan),
        "cell": dict(response[0]),
    }


_operational_stub.fetch_exact_single_cell = _fetch_stub
sys.modules["src.develop.run_pipeline_operational_v2"] = _operational_stub

from src.develop.annual_requery_shadow_v1 import verify_annual_requery


def test_annual_requery_answer_is_official_data_explanation_without_machine_verdict():
    rows = [
        {
            "value_span_id": "level-1",
            "value_text": "254341",
            "value_unit": "명",
            "retrieval_fields": {
                "indicator": "출생아 수",
                "measurement_type": "LEVEL",
                "period": {"measurement": {"absolute": "2025"}, "baseline": {"absolute": ""}},
            },
        },
        {
            "value_span_id": "change-1",
            "value_text": "6.7",
            "value_unit": "%",
            "retrieval_fields": {
                "indicator": "출생아 수 증가율",
                "measurement_type": "CHANGE_RATE",
                "value_direction": "INCREASE",
                "period": {
                    "measurement": {"absolute": "2025"},
                    "baseline": {"absolute": "2024"},
                },
            },
        },
    ]

    result = verify_annual_requery(
        rows=rows,
        current_plan={
            "org_id": "org",
            "tbl_id": "table",
            "itm_id": "T10",
            "prd_se": "Y",
            "start_prd_de": "2025",
            "end_prd_de": "2025",
            "obj_levels": {},
        },
        current_cell_result={
            "status": "CELL_RESOLVED",
            "cell": {"DT": "254341", "ITM_NM": "출생건수 (명)"},
        },
        current_target_id="level-1",
        cell_fetcher=lambda _plan: [{"DT": "238317", "ITM_NM": "출생건수 (명)"}],
        official_unit="명",
    )

    assert result["verdict"] == "VERIFIED"
    assert result["answer"] == (
        "KOSIS 공식 통계에서 2025년 출생아 수는 254,341명이고, "
        "2024년 238,317명보다 16,024명(약 6.7%) 증가했습니다."
    )
    assert "VERIFIED" not in result["answer"]
    assert "Decimal" not in result["answer"]
