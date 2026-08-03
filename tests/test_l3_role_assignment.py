from src.develop.l3_role_assignment import (
    assign_roles,
    assignment_summary,
    resolve_region_chain,
)

ARTICLE = (
    "16일 통계청은 취업자가 2909만명이라고 밝혔다. "
    "제조업 취업자는 8만3000명 줄었다. "
    "김 교수는 저임금 일자리 위주라고 했다."
)


def _layout(**overrides):
    layout = {
        0: {
            "sentence_id": 0,
            "indicator_scopes": [{"indicator_label": "취업자 수",
                                  "source_span_text": "취업자"}],
            "source_region": {"opens_region": True,
                              "governing_sentence_id": 0,
                              "source_subtype": "공식집계"},
        },
        1: {
            "sentence_id": 1,
            "indicator_scopes": [],
            "source_region": {"opens_region": False,
                              "governing_sentence_id": 0},
        },
        2: {
            "sentence_id": 2,
            "indicator_scopes": [],
            "source_region": {"opens_region": False,
                              "governing_sentence_id": None},
        },
    }
    layout.update(overrides)
    return list(layout.values())


def test_region_chain_reaches_the_opening_sentence():
    layout = {row["sentence_id"]: row for row in _layout()}

    resolved = resolve_region_chain(layout, 1)

    assert resolved["region_sentence_id"] == 0
    assert resolved["source_subtype"] == "공식집계"


def test_region_chain_returns_nothing_for_ungoverned_sentence():
    layout = {row["sentence_id"]: row for row in _layout()}

    assert resolve_region_chain(layout, 2) is None


def test_region_chain_survives_a_pointer_cycle():
    layout = {
        0: {"sentence_id": 0, "source_region": {"opens_region": False,
                                                "governing_sentence_id": 1}},
        1: {"sentence_id": 1, "source_region": {"opens_region": False,
                                                "governing_sentence_id": 0}},
    }

    assert resolve_region_chain(layout, 0) is None


def test_value_inherits_indicator_from_earlier_sentence():
    assignments = assign_roles(ARTICLE, _layout())
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second
    assert second[0]["indicator_label"] == "취업자 수"
    assert second[0]["indicator_source"] == "INHERITED"
    assert second[0]["source_subtype"] == "공식집계"


def test_local_indicator_beats_inheritance():
    layout = _layout()
    layout[1]["indicator_scopes"] = [
        {"indicator_label": "제조업 취업자 수", "source_span_text": "제조업 취업자"}
    ]

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] == "제조업 취업자 수"
    assert second[0]["indicator_source"] == "LOCAL"


def test_a_measure_without_a_subject_is_a_fragment():
    """`성장률` names how something is counted, not what."""
    from src.develop.l3_role_assignment import indicator_is_fragment

    assert indicator_is_fragment("성장률")
    assert indicator_is_fragment("비율")
    assert indicator_is_fragment("지수")


def test_a_comparison_basis_is_not_a_subject():
    """`원화 기준` says what the figure is denominated in, not what was measured."""
    from src.develop.l3_role_assignment import indicator_is_fragment

    assert indicator_is_fragment("원화 기준 증가율")
    assert indicator_is_fragment("전년 대비 증가율")


def test_a_period_prefix_does_not_make_a_label_complete():
    """`1분기 성장률` still says nothing about what grew."""
    from src.develop.l3_role_assignment import indicator_is_fragment

    assert indicator_is_fragment("1분기 성장률")
    assert indicator_is_fragment("지난해 증가율")


def test_composition_keeps_only_the_measure_noun():
    """Splicing a basis into the parent builds a string no table carries."""
    layout = _layout()
    layout[0]["indicator_scopes"] = [
        {"indicator_label": "1인당 국민소득", "source_span_text": "취업자"}
    ]
    layout[1]["indicator_scopes"] = [
        {"indicator_label": "원화 기준 증가율", "source_span_text": "줄었다"}
    ]

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] == "1인당 국민소득 증가율"
    assert second[0]["indicator_local_fragment"] == "원화 기준 증가율"


def test_a_complete_indicator_is_not_a_fragment():
    from src.develop.l3_role_assignment import indicator_is_fragment

    assert not indicator_is_fragment("법인세 비율")
    assert not indicator_is_fragment("노년 부양비")
    assert not indicator_is_fragment("소비자물가지수")
    assert not indicator_is_fragment("")


def test_a_local_fragment_takes_its_subject_from_inheritance():
    layout = _layout()
    layout[1]["indicator_scopes"] = [
        {"indicator_label": "증가율", "source_span_text": "줄었다"}
    ]

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] == "취업자 수 증가율"
    assert second[0]["indicator_source"] == "LOCAL_COMPOSED"
    assert second[0]["indicator_local_fragment"] == "증가율"


def test_composition_does_not_repeat_what_the_parent_already_says():
    layout = _layout()
    layout[0]["indicator_scopes"] = [
        {"indicator_label": "취업자 증가율", "source_span_text": "취업자"}
    ]
    layout[1]["indicator_scopes"] = [
        {"indicator_label": "증가율", "source_span_text": "줄었다"}
    ]

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] == "취업자 증가율"


def test_a_fragment_stands_alone_when_the_parent_is_also_a_fragment():
    """Composing two fragments would invent an indicator neither one states."""
    layout = _layout()
    layout[0]["indicator_scopes"] = [
        {"indicator_label": "비율", "source_span_text": "취업자"}
    ]
    layout[1]["indicator_scopes"] = [
        {"indicator_label": "증가율", "source_span_text": "줄었다"}
    ]

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] == "증가율"
    assert second[0]["indicator_source"] == "LOCAL"


def test_a_fragment_without_a_region_is_left_alone():
    """An unbounded backward walk attaches whatever indicator came last."""
    layout = _layout()
    layout[1]["indicator_scopes"] = [
        {"indicator_label": "증가율", "source_span_text": "줄었다"}
    ]
    layout[1]["source_region"] = {"opens_region": False,
                                  "governing_sentence_id": None}

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] == "증가율"
    assert second[0]["indicator_source"] == "LOCAL"


def test_a_fragment_does_not_cross_a_region_boundary_to_find_a_subject():
    """Composition must obey the same boundary inheritance does."""
    layout = _layout()
    layout[1]["indicator_scopes"] = [
        {"indicator_label": "증가율", "source_span_text": "줄었다"}
    ]
    layout[1]["source_region"] = {
        "opens_region": True,
        "governing_sentence_id": 1,
        "source_subtype": "민간조사",
    }

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] == "증가율"
    assert second[0]["indicator_source"] == "LOCAL"


def test_inheritance_stops_at_the_region_boundary():
    """A figure must not inherit an indicator from a different publisher."""
    layout = _layout()
    layout[1]["source_region"] = {
        "opens_region": True,
        "governing_sentence_id": 1,
        "source_subtype": "민간조사",
    }

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["indicator_label"] is None
    assert second[0]["source_subtype"] == "민간조사"


def test_nearest_scope_measures_distance_in_both_directions():
    """A level indicator precedes its figure; a change indicator follows it."""
    from src.develop.l3_role_assignment import _nearest_scope

    article = "대기업 수출액은 1223억달러로 5.1% 증가했다."
    scopes = [
        {"indicator_label": "대기업 수출액",
         "source_char_start": article.find("수출액"),
         "source_char_end": article.find("수출액") + 3},
        {"indicator_label": "대기업 수출액 증가율",
         "source_char_start": article.find("증가"),
         "source_char_end": article.find("증가") + 2},
    ]

    level, _ = _nearest_scope(scopes, 9, 16)     # 1223억달러
    rate, _ = _nearest_scope(scopes, 18, 22)     # 5.1%

    assert level["indicator_label"] == "대기업 수출액"
    assert rate["indicator_label"] == "대기업 수출액 증가율"


def test_nearest_scope_declines_when_spans_swallow_the_value():
    """Whole-clause spans give every candidate the same zero gap."""
    from src.develop.l3_role_assignment import _nearest_scope

    scopes = [
        {"indicator_label": "A", "source_char_start": 0, "source_char_end": 80},
        {"indicator_label": "B", "source_char_start": 0, "source_char_end": 80},
    ]

    assert _nearest_scope(scopes, 30, 35) is None


def test_multiple_values_pair_positionally_with_multiple_scopes():
    article = "대기업 수출액은 1223억달러로 5.1% 증가했다."
    layout = [{
        "sentence_id": 0,
        "indicator_scopes": [
            {"indicator_label": "대기업 수출액", "source_span_text": "수출액"},
            {"indicator_label": "대기업 수출액 증가율", "source_span_text": "증가"},
        ],
        "source_region": {"opens_region": True, "governing_sentence_id": 0,
                          "source_subtype": "공식집계"},
    }]

    assignments = assign_roles(article, layout)

    labels = [a["indicator_label"] for a in assignments]
    assert labels == ["대기업 수출액", "대기업 수출액 증가율"]
    assert {a["indicator_pairing"] for a in assignments} == {"POSITIONAL"}


def test_count_mismatch_reuses_last_scope_instead_of_dropping_values():
    article = "대기업 수출액은 1223억달러로 5.1% 증가했다."
    layout = [{
        "sentence_id": 0,
        "indicator_scopes": [
            {"indicator_label": "대기업 수출액", "source_span_text": "수출액"}
        ],
        "source_region": {"opens_region": True, "governing_sentence_id": 0,
                          "source_subtype": "공식집계"},
    }]

    assignments = assign_roles(article, layout)

    assert all(a["indicator_label"] == "대기업 수출액" for a in assignments)
    assert assignments[-1]["indicator_pairing"] == "COUNT_MISMATCH_FALLBACK"


def test_period_is_carried_forward_like_the_indicator():
    layout = _layout()
    layout[0]["period_context"] = {"period_raw": "지난달"}

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["period_raw"] == "지난달"
    assert second[0]["period_source"] == "INHERITED"


def test_local_period_beats_inheritance():
    layout = _layout()
    layout[0]["period_context"] = {"period_raw": "지난달"}
    layout[1]["period_context"] = {"period_raw": "지난해"}

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["period_raw"] == "지난해"
    assert second[0]["period_source"] == "LOCAL"


def test_period_does_not_cross_a_source_region_boundary():
    layout = _layout()
    layout[0]["period_context"] = {"period_raw": "지난달"}
    layout[1]["source_region"] = {
        "opens_region": True,
        "governing_sentence_id": 1,
        "source_subtype": "민간조사",
    }

    assignments = assign_roles(ARTICLE, layout)
    second = [a for a in assignments if a["article_sentence_id"] == 1]

    assert second[0]["period_raw"] == ""
    assert second[0]["period_source"] == "NONE"


def test_summary_counts_coverage():
    summary = assignment_summary(assign_roles(ARTICLE, _layout()))

    assert summary["values"] >= 2
    assert summary["with_indicator"] >= 2
    assert summary["indicator_source"]["INHERITED"] >= 1
