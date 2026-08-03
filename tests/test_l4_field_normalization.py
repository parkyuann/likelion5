from src.develop.l4_field_normalization import (
    CHANGE_POINT,
    CHANGE_RATE,
    INDEX_LEVEL,
    LEVEL,
    absolute_period,
    compose_fields,
    dimension_terms,
    item_terms,
    measurement_type,
    population_terms,
)


def test_indicator_suffix_beats_the_unit():
    """`고용률 63.6%` is a level; `상승률 5.1%` is a rate."""
    assert measurement_type("고용률", "%", "고용률은 63.6%다.") == LEVEL
    assert measurement_type("소비자물가 상승률", "%", "5.1% 올랐다.") == CHANGE_RATE


def test_index_indicator_is_index_level():
    assert measurement_type("소비자물가지수", "", "지수는 114.2다.") == INDEX_LEVEL


def test_point_unit_is_change_point():
    assert measurement_type("고용률 증감률", "%포인트", "0.1%포인트 늘었다.") == CHANGE_POINT
    assert measurement_type("고용률", "%p", "0.1%p 늘었다.") == CHANGE_POINT


def test_bare_percentage_uses_the_predicate_after_the_value():
    sentence = "수출은 1223억달러로 5.1% 증가했다."
    end = sentence.find("5.1%") + len("5.1%")

    assert measurement_type("대기업 수출", "%", sentence, end) == CHANGE_RATE


def test_bare_percentage_without_a_change_predicate_is_a_level():
    sentence = "전체 근로자의 30.8%를 차지했다."
    end = sentence.find("30.8%") + len("30.8%")

    assert measurement_type("단기 근로자", "%", sentence, end) == LEVEL


def test_predicate_before_the_value_does_not_make_it_a_rate():
    sentence = "증가한 취업자 가운데 30.8%가 청년이다."
    end = sentence.find("30.8%") + len("30.8%")

    assert measurement_type("청년 취업자", "%", sentence, end) == LEVEL


def test_period_words_are_stripped_from_the_indicator():
    """No KOSIS table is named `이달 첫 주 휘발유 평균 판매가`."""
    from src.develop.l4_field_normalization import strip_period_from_indicator

    stripped, removed = strip_period_from_indicator("이달 첫 주 휘발유 평균 판매가")

    assert stripped == "휘발유 평균 판매가"
    assert removed


def test_stripping_keeps_the_measure_intact():
    from src.develop.l4_field_normalization import strip_period_from_indicator

    assert strip_period_from_indicator("전년 대비 근로소득세 수입 증가율")[0] == (
        "근로소득세 수입 증가율"
    )
    assert strip_period_from_indicator("지난해 근로소득세 수입")[0] == "근로소득세 수입"


def test_an_indicator_without_a_period_is_untouched():
    from src.develop.l4_field_normalization import strip_period_from_indicator

    stripped, removed = strip_period_from_indicator("단기 근로자 비율")

    assert stripped == "단기 근로자 비율"
    assert removed == []


def test_a_period_only_label_is_kept_rather_than_emptied():
    from src.develop.l4_field_normalization import strip_period_from_indicator

    assert strip_period_from_indicator("지난해")[0] == "지난해"


def test_stripped_period_fills_an_empty_period_field():
    composed = compose_fields(
        {"indicator_label": "지난해 근로소득세 수입", "period_raw": ""},
        published_at=None,
    )

    assert composed["retrieval_fields"]["indicator"] == "근로소득세 수입"
    assert composed["retrieval_fields"]["period"] == "지난해"


def test_an_existing_period_field_is_not_overwritten():
    composed = compose_fields(
        {"indicator_label": "이달 첫 주 휘발유 평균 판매가",
         "period_raw": "5월 셋째 주"},
        published_at=None,
    )

    assert composed["retrieval_fields"]["period"] == "5월 셋째 주"
    assert composed["retrieval_fields"]["indicator"] == "휘발유 평균 판매가"


def test_a_duration_span_is_a_period_too():
    """`5년 간 근로소득 증가율` retrieved nothing until a person typed 근로소득."""
    from src.develop.l4_field_normalization import strip_period_from_indicator

    stripped, removed = strip_period_from_indicator("5년 간 근로소득 증가율")

    assert stripped == "근로소득 증가율"
    assert removed == ["5년 간"]


def test_a_bare_year_is_not_read_as_a_span():
    from src.develop.l4_field_normalization import strip_period_from_indicator

    assert strip_period_from_indicator("2023년 근로소득")[0] == "근로소득"


def test_domestic_scope_is_dropped_from_the_head():
    from src.develop.l4_field_normalization import strip_leading_modifiers

    stripped, removed = strip_leading_modifiers("한국의 초기 1인당 국민소득")

    assert stripped == "1인당 국민소득"
    assert removed["scope"] == ["한국의"]
    assert removed["temporal"] == ["초기"]


def test_foreign_scope_is_never_dropped():
    """Removing 독일 would make a claim KOSIS cannot hold look retrievable."""
    from src.develop.l4_field_normalization import strip_leading_modifiers

    assert strip_leading_modifiers("독일 소득세 최고 세율")[0] == "독일 소득세 최고 세율"
    assert strip_leading_modifiers("OECD 평균 GDP 대비 소득세 비율")[0] == (
        "OECD 평균 GDP 대비 소득세 비율"
    )


def test_a_projection_marker_is_stripped_but_reported():
    from src.develop.l4_field_normalization import strip_leading_modifiers

    stripped, removed = strip_leading_modifiers("미래 한국 노년 부양비")

    assert stripped == "노년 부양비"
    assert removed["projection"] == ["미래"]
    assert removed["scope"] == ["한국"]


def test_modifiers_are_matched_as_whole_tokens():
    """`국내총생산` is one token; substring stripping would gut it."""
    from src.develop.l4_field_normalization import strip_leading_modifiers

    assert strip_leading_modifiers("국내총생산 증가율")[0] == "국내총생산 증가율"
    assert strip_leading_modifiers("한국은행 기준금리")[0] == "한국은행 기준금리"


def test_a_modifier_only_indicator_is_kept_whole():
    from src.develop.l4_field_normalization import strip_leading_modifiers

    assert strip_leading_modifiers("미래 한국")[0] == "미래 한국"


def test_compose_reports_the_projection_marker(tmp_path=None):
    composed = compose_fields(
        {"indicator_label": "미래 한국 노년 부양비"}, published_at=None,
    )

    assert composed["retrieval_fields"]["indicator"] == "노년 부양비"
    assert composed["field_provenance"]["indicator_projection_marker"] is True


def test_an_ordinary_indicator_carries_no_projection_marker():
    composed = compose_fields({"indicator_label": "취업자 수"}, published_at=None)

    assert composed["field_provenance"]["indicator_projection_marker"] is False


def test_period_is_absolutised_against_publication_date():
    assert absolute_period("지난달", "2025-10-09").startswith("2025")


def test_period_without_publication_date_keeps_raw_text():
    assert absolute_period("지난달", None) == "지난달"


def test_empty_period_stays_empty():
    assert absolute_period("", "2025-10-09") == ""


def test_population_prefers_the_indicator_over_the_sentence():
    assert population_terms("단기 근로자 비율", "기업 조사 결과다.") == ["근로자"]


def test_population_falls_back_to_the_sentence():
    assert population_terms("수출액", "취업자는 2909만명이다.") == ["취업자"]


def test_dimension_terms_read_business_size():
    assert dimension_terms("대기업 수출액", "") == ["대기업"]


def test_item_matches_gold_granularity():
    """Gold keeps the measure noun and the population inside the item."""
    assert item_terms("대기업 수출액 증가율") == ["수출액"]
    assert item_terms("단기 근로자 비율") == ["단기 근로자"]


def test_item_is_empty_when_the_label_is_only_a_metric():
    assert item_terms("비율") == []


def test_compose_fields_builds_all_six():
    assignment = {
        "indicator_label": "대기업 수출액 증가율",
        "sentence_text": "대기업 수출액은 1223억달러로 5.1% 증가했다.",
        "value_unit": "%",
        "value_char_end": 22,
        "period_raw": "지난달",
        "indicator_source": "LOCAL",
        "period_source": "LOCAL",
        "source_subtype": "공식집계",
    }

    composed = compose_fields(assignment, published_at="2025-10-09")

    fields = composed["retrieval_fields"]
    assert set(fields) == {
        "indicator", "measurement_type", "period", "period_absolute",
        "population", "item", "dimension",
    }
    assert fields["measurement_type"] == CHANGE_RATE
    assert fields["dimension"] == ["대기업"]
    assert composed["field_provenance"]["indicator"] == "LOCAL"


def test_period_keeps_the_article_wording_and_adds_the_absolute_form():
    """Gold compares against the article's own wording, not the resolved date."""
    composed = compose_fields(
        {"indicator_label": "취업자 수", "period_raw": "지난달"},
        published_at="2025-10-09",
    )

    fields = composed["retrieval_fields"]
    assert fields["period"] == "지난달"
    assert fields["period_absolute"].startswith("2025")
