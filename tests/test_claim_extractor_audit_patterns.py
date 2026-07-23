from src.claim_extractor import extract_from_sentence


def test_duration_days_are_extracted_but_calendar_days_are_not():
    row = extract_from_sentence("출산휴가 기간이 2월 23일부터 10일에서 20일로 확대된다.")
    assert row is not None
    assert row["value_list"] == "23;10;20"
    assert row["unit_list"] == "일;일;일"


def test_oov_units_and_quarter_sequence_are_extracted():
    row = extract_from_sentence("물김 2400t과 데이터센터 100MW, 10분기 연속 감소가 관측됐다.")
    assert row is not None
    assert "t" in row["unit_list"]
    assert "MW" in row["unit_list"]
    assert "분기" in row["unit_list"]


def test_unitless_fraction_and_index_are_extracted():
    row = extract_from_sentence("소비는 GDP의 3분의 2이고 코스피는 3000선을 넘었다.")
    assert row is not None
    assert "비율" in row["unit_list"]
    assert "지수" in row["unit_list"]


def test_calendar_and_scaffolding_labels_are_not_extracted():
    row = extract_from_sentence("31일 발표했고 1분기 계획으로 41MW를 건설한다.")
    assert row is not None
    assert row["value_list"] == "31;1;41"
    assert row["unit_list"] == "일;분기;MW"


def test_week_anniversary_is_not_a_week_count():
    row = extract_from_sentence("창립 50주년 행사를 개최했다.")
    assert row is None
