from src.claim_extractor import extract_from_sentence


def test_age_range_is_dimension_not_extra_observation_value():
    row = extract_from_sentence("15~29\uC138 \uCCAD\uB144\uCE35 \uACE0\uC6A9\uB960\uC740 \uC804\uAD6D\uC5D0\uC11C 45.1%\uC600\uB2E4.")
    assert row is not None
    assert row["value_list"] == "45.1"
    assert row["indicator_raw"] == "\uACE0\uC6A9\uB960"
    assert row["dimension_json"]["\uC5F0\uB839"][0]["raw"] == "15~29\uC138"
    assert row["dimension_json"]["\uC9C0\uC5ED"][0]["raw"] == "\uC804\uAD6D"


def test_region_gender_and_population_keep_source_spans():
    row = extract_from_sentence("\uC11C\uC6B8 \uB0A8\uC131 \uCDE8\uC5C5\uC790 \uC218\uB294 120\uBA85\uC73C\uB85C \uC9D1\uACC4\uB410\uB2E4.")
    assert row is not None
    assert row["indicator_raw"] == "\uCDE8\uC5C5\uC790 \uC218"
    assert row["population_raw"] == "\uCDE8\uC5C5\uC790"
    assert row["dimension_json"]["\uC9C0\uC5ED"][0]["source_span"] == "\uC11C\uC6B8"
    assert row["dimension_json"]["\uC131\uBCC4"][0]["source_span"] == "\uB0A8\uC131"
