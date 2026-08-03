from src.develop.build_retrieval_gold_targets import (
    attach_candidates,
    select_targets,
    summarise,
)
from src.develop.kosis_lexical_search import LexicalIndex


def _routed(**overrides):
    row = {
        "article_idx": "1",
        "value_span_id": "s0:value_unit:0-3",
        "value_text": "9.2%",
        "value_unit": "%",
        "sentence_text": "노년 부양비는 9.2%다.",
        "source_subtype": "공식집계",
        "retrieval_fields": {
            "indicator": "노년 부양비", "measurement_type": "LEVEL",
            "period": "지난해", "period_absolute": "2024",
            "population": [], "item": ["노년"], "dimension": [],
        },
    }
    row.update(overrides)
    return row


def _gold(article="1", span="s0:value_unit:0-3", cls="KOSIS_CANDIDATE"):
    return {"article_idx": article, "value_span_id": span, "judged_class": cls}


def test_only_human_kosis_values_become_targets():
    """Adjudicating the model's routing would inherit its errors."""
    routed = [_routed(), _routed(value_span_id="s1:value_unit:0-3")]
    gold = [_gold(), _gold(span="s1:value_unit:0-3", cls="NOT_CLAIM")]

    targets = select_targets(routed, gold)

    assert [row["value_span_id"] for row in targets] == ["s0:value_unit:0-3"]


def test_the_same_indicator_and_period_is_adjudicated_once():
    routed = [
        _routed(),
        _routed(value_span_id="s1:value_unit:0-3"),
    ]
    gold = [_gold(), _gold(span="s1:value_unit:0-3")]

    assert len(select_targets(routed, gold)) == 1


def test_a_different_period_is_a_separate_target():
    other = _routed(value_span_id="s1:value_unit:0-3")
    other["retrieval_fields"] = {**other["retrieval_fields"], "period": "올해"}
    gold = [_gold(), _gold(span="s1:value_unit:0-3")]

    assert len(select_targets([_routed(), other], gold)) == 2


def test_a_value_without_an_indicator_cannot_be_searched_for():
    row = _routed()
    row["retrieval_fields"] = {**row["retrieval_fields"], "indicator": ""}

    assert select_targets([row], [_gold()]) == []


def test_per_article_cap_stops_one_article_dominating():
    routed = []
    gold = []
    for position in range(5):
        span = f"s{position}:value_unit:0-3"
        row = _routed(value_span_id=span)
        row["retrieval_fields"] = {
            **row["retrieval_fields"], "indicator": f"지표{position}",
        }
        routed.append(row)
        gold.append(_gold(span=span))

    assert len(select_targets(routed, gold, per_article=2)) == 2


def _index():
    return LexicalIndex([
        {"table_key": "101:A", "tbl_name": "노년부양비 및 노령화지수",
         "category_paths": ["인구"], "profile_present": True},
        {"table_key": "101:B", "tbl_name": "어가 인구",
         "category_paths": ["수산"], "profile_present": False},
    ])


def test_candidates_are_attached_with_ranks():
    rows = attach_candidates(select_targets([_routed()], [_gold()]), _index())

    assert rows[0]["candidates"][0]["rank"] == 1
    assert rows[0]["candidates"][0]["table_key"] == "101:A"


def test_human_columns_start_empty():
    """A prefilled answer would make the adjudication confirm the generator."""
    rows = attach_candidates(select_targets([_routed()], [_gold()]), _index())

    assert rows[0]["gold_table_key"] == ""
    assert rows[0]["gold_match_status"] == ""
    assert rows[0]["review_status"] == "미검토"


def test_summary_records_that_the_gold_is_not_adjudicated_yet():
    rows = attach_candidates(select_targets([_routed()], [_gold()]), _index())

    summary = summarise(rows)

    assert summary["gold_status"] == "unadjudicated"
    assert summary["candidate_source"] == "lexical_bigram_only"
    assert summary["contains_model_output"] is False
