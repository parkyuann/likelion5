import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from src.convert_claim_listform_to_schema import make_claim
from src.retrieval_schema import validate_claim


def test_auto_structured_fields_flow_to_claim_and_observation():
    row = pd.Series({
        "article_idx": "7",
        "claim_text": "economic claim",
        "value_list": "12.5",
        "unit_list": "%",
        "indicator_raw": "employment rate",
        "population_raw": "young workers",
        "dimension_json": json.dumps({"지역": [{"raw": "서울", "source_span": "서울"}]}, ensure_ascii=False),
        "time_ref": "2025",
        "change_type": "단순수치",
        "is_index": "False",
        "list_alignment_status": "SINGLE_VALUE",
    })
    claim = make_claim(row, 0)
    assert claim.indicator_raw == "employment rate"
    assert claim.population_raw == "young workers"
    assert claim.auto_indicator_raw == "employment rate"
    assert claim.observations[0].indicator_raw == "employment rate"
    assert claim.observations[0].dimension_json["지역"][0]["raw"] == "서울"
    assert validate_claim(claim) == []


def test_gold_fields_are_kept_separate_and_take_precedence_for_canonical_fields():
    row = pd.Series({
        "article_idx": "8",
        "claim_text": "claim",
        "value_list": "3",
        "unit_list": "%",
        "indicator_raw": "auto indicator",
        "population_raw": "auto population",
        "dimension_json": "{}",
        "gold_indicator_raw": "gold indicator",
        "gold_population": "gold population",
        "time_ref": "2025",
        "change_type": "단순수치",
        "is_index": "False",
    })
    claim = make_claim(row, 1)
    assert claim.indicator_raw == "gold indicator"
    assert claim.population_raw == "gold population"
    assert claim.auto_indicator_raw == "auto indicator"
    assert claim.auto_population_raw == "auto population"


def test_context_audit_and_contextual_query_survive_csv_schema_conversion():
    row = pd.Series({
        "article_idx": "9", "claim_text": "보험료는 3.1% 상승했다.", "value_list": "3.1", "unit_list": "%",
        "dimension_json": "{}", "context_resolution_json": json.dumps({"status": "RESOLVED", "resolved_terms": ["손해보험"]}, ensure_ascii=False),
        "retrieval_query_text": "보험료는 3.1% 상승했다.\n문맥 확정 대상: 손해보험",
    })
    claim = make_claim(row, 2)
    assert claim.context_resolution["resolved_terms"] == ["손해보험"]
    assert claim.retrieval_query_text.endswith("손해보험")
    assert validate_claim(claim) == []
