from pathlib import Path

from src.develop.article_hcx_gold_fixture import (
    load_jsonl,
    load_saved_articles,
    compatible_measurement_types_for_value_span,
    validate_gold_fixture,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "develop" / "article_hcx_calibration" / "evaluation" / "기사단위_HCX_claim_skeleton_gold_20260729.jsonl"
SAVED_RUN_ROOT = ROOT / "data" / "develop" / "article_hcx_calibration" / "runs" / "기사단위_HCX_spanID_사람검토반영_측정단위_20260729"


def test_source_unit_limits_measurement_types_without_misclassifying_rate_levels():
    assert compatible_measurement_types_for_value_span({"unit": "지수"}) == {"INDEX_LEVEL"}
    assert compatible_measurement_types_for_value_span({"unit": "%"}) == {"LEVEL", "CHANGE_RATE"}
    assert compatible_measurement_types_for_value_span({"unit": "%p"}) == {"CHANGE_POINT"}
    assert compatible_measurement_types_for_value_span({"unit": "원"}) == {"LEVEL"}


def test_five_article_gold_fixture_is_fully_source_grounded():
    result = validate_gold_fixture(load_jsonl(FIXTURE), load_saved_articles(SAVED_RUN_ROOT))

    assert result["fixture_rows"] == 24
    assert result["passed_rows"] == 24
    assert result["invalid_rows"] == 0
    assert result["by_eligibility"] == {"KOSIS_CANDIDATE": 23, "EXCLUDED_SOURCE_SCOPE": 1}
    assert result["by_measurement_type"] == {
        "CHANGE_POINT": 0,
        "CHANGE_RATE": 19,
        "INDEX_LEVEL": 1,
        "LEVEL": 4,
    }


def test_gold_fixture_rejects_a_value_not_extracted_as_source_candidate():
    rows = [{
        "fixture_id": "broken", "article_idx": "1", "eligibility": "KOSIS_CANDIDATE",
        "measurement_type": "CHANGE_RATE", "value_text": "99.9%", "value_sentence_id": 0,
        "period": None, "comparison_terms": [],
    }]
    articles = {"1": {"title": "", "article_text": "고용률은 45.1%였다."}}

    result = validate_gold_fixture(rows, articles)

    assert result["invalid_rows"] == 1
    assert result["reports"][0]["errors"] == ["VALUE_SPAN_NOT_CANDIDATED"]
