from src.develop.evaluate_six_fields import compare_field, evaluate


def test_absent_marker_is_an_answer_not_a_blank():
    assert compare_field("dimension", "없음", []) == "BOTH_ABSENT"
    assert compare_field("dimension", "없음", ["대기업"]) == "WRONG"


def test_blank_against_a_real_value_is_wrong_not_agreement():
    assert compare_field("item", "단기 근로자", []) == "WRONG"
    assert compare_field("item", "", ["단기 근로자"]) == "WRONG"


def test_measurement_enum_has_no_partial_credit():
    assert compare_field("measurement", "LEVEL", "LEVEL") == "EXACT"
    assert compare_field("measurement", "LEVEL", "CHANGE_RATE") == "WRONG"


def test_free_text_fields_allow_relaxed_matching():
    assert compare_field("indicator", "단기 근로자 비율", "단기 근로자 비율") == "EXACT"
    # a strict subset is reported as SUBSUMED, a partial overlap as RELAXED
    assert compare_field("indicator", "단기 근로자 비율", "근로자 비율") == "SUBSUMED"
    assert compare_field("indicator", "단기 근로자 비율", "단기 근로자 인구") == "RELAXED"
    assert compare_field("indicator", "단기 근로자 비율", "수출액") == "WRONG"


def test_a_more_specific_label_is_subsumed_not_wrong():
    """`제조업 취업자 수` queries the same table as `취업자 수`."""
    assert compare_field("indicator", "취업자 수", "제조업 취업자 수") == "SUBSUMED"
    assert compare_field("indicator", "제조업 취업자 수", "취업자 수") == "SUBSUMED"


def test_subsumption_needs_a_real_subset():
    assert compare_field("indicator", "취업자 수", "수출액 증가율") == "WRONG"


def test_list_prediction_is_flattened_before_comparison():
    assert compare_field("population", "근로자", ["근로자"]) == "EXACT"


def _gold(**overrides):
    row = {
        "article_idx": "380",
        "candidate span ID": "s0:value_unit:53-56",
        "claim 여부": "YES",
        "검증대상 gold": "KOSIS_CANDIDATE",
        "indicator gold": "단기 근로자 비율",
        "measurement gold": "LEVEL",
        "period gold": "지난해",
        "population gold": "근로자",
        "item gold": "단기 근로자",
        "dimension gold": "주당 36시간 미만",
    }
    row.update(overrides)
    return row


def _pred(**fields):
    base = {
        "indicator": "단기 근로자 비율",
        "measurement_type": "LEVEL",
        "period": "지난해",
        "population": ["근로자"],
        "item": ["단기 근로자"],
        "dimension": ["주당 36시간 미만"],
    }
    base.update(fields)
    return [{
        "article_idx": "380",
        "value_span_id": "s0:value_unit:53-56",
        "retrieval_fields": base,
    }]


def test_all_six_exact_counts_once():
    result = evaluate([_gold()], _pred())

    assert result["scored"] == 1
    assert result["joint_six_exact"] == 1.0
    assert result["joint_five_exact_without_dimension"] == 1.0


def test_three_field_metric_ignores_the_post_retrieval_axes():
    """population/item/dimension are resolved against table metadata later."""
    result = evaluate([_gold()], _pred(
        population=["수출기업"], item=["수출액"], dimension=["대기업"]
    ))

    assert result["joint_six_exact"] == 0.0
    assert result["joint_three_exact"] == 1.0


def test_three_field_metric_reports_which_field_blocked():
    result = evaluate([_gold()], _pred(period="2024"))

    assert result["joint_three_relaxed"] == 0.0
    assert result["three_field_blocking_combination"] == {"period": 1}


def test_dimension_miss_is_isolated_by_the_five_field_metric():
    result = evaluate([_gold()], _pred(dimension=["대기업"]))

    assert result["joint_six_exact"] == 0.0
    assert result["joint_five_exact_without_dimension"] == 1.0


def test_period_is_compared_against_article_wording():
    """An absolutised period must not be scored against gold's raw wording."""
    result = evaluate([_gold()], _pred(period="2024"))

    assert result["per_field"]["period"]["exact_accuracy"] == 0.0


def test_unjoined_gold_rows_are_reported_not_dropped():
    result = evaluate([_gold(**{"candidate span ID": ""})], _pred())

    assert result["scored"] == 0
    assert result["gold_rows_without_matching_prediction"] == 1


def test_scope_filter_excludes_out_of_scope_rows():
    result = evaluate([_gold(**{"검증대상 gold": "OUT_OF_SCOPE"})], _pred())

    assert result["scored"] == 0
