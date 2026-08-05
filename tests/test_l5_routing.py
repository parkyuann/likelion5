from src.develop.l5_routing import (
    KOSIS_CANDIDATE,
    NOT_CLAIM,
    OUT_OF_SCOPE,
    evaluate_routing,
    route_all,
    route_value,
    routing_summary,
)


def test_official_region_routes_to_kosis():
    decision = route_value(
        {"source_subtype": "공식집계", "indicator_label": "취업자 수"}
    )

    assert decision["routing_class"] == KOSIS_CANDIDATE
    assert decision["confidence"] == 1.0


def test_out_of_scope_subtypes_block_with_full_confidence():
    for subtype in ("민간조사", "정책목표", "잠정추산", "법정기준"):
        decision = route_value(
            {"source_subtype": subtype, "indicator_label": "무엇"}
        )
        assert decision["routing_class"] == OUT_OF_SCOPE
        assert decision["reason"].endswith(subtype)


def test_indicator_without_region_routes_with_low_confidence():
    """Dropping a measurable value is final; routing it costs UNVERIFIABLE."""
    decision = route_value({"source_subtype": "", "indicator_label": "취업자 수"})

    assert decision["routing_class"] == KOSIS_CANDIDATE
    assert decision["confidence"] == 0.5


def test_no_indicator_and_no_region_is_not_a_claim():
    decision = route_value({"source_subtype": "", "indicator_label": ""})

    assert decision["routing_class"] == NOT_CLAIM


def test_value_repeated_inside_indicator_is_a_dimension_not_an_observation():
    """`하위 20%` describes the group; the observed income is another value."""
    row = {
        "source_subtype": "공식집계",
        "indicator_label": "소득 하위 20% 가구의 월평균 소득 비율",
        "value_text": "20%",
        "retrieval_fields": {"indicator": "소득 하위 20% 가구의 월평균 소득 비율"},
    }

    decision = route_value(row)

    assert decision["routing_class"] == NOT_CLAIM
    assert decision["reason"] == "VALUE_REPEATED_INSIDE_INDICATOR"


def test_a_different_observed_value_is_not_blocked_by_the_dimension_rule():
    row = {
        "source_subtype": "공식집계",
        "indicator_label": "소득 하위 20% 가구의 월평균 소득",
        "value_text": "114만원",
        "retrieval_fields": {"indicator": "소득 하위 20% 가구의 월평균 소득"},
    }

    assert route_value(row)["routing_class"] == KOSIS_CANDIDATE


def test_threshold_demotes_low_confidence_routing():
    assignments = [{"source_subtype": "", "indicator_label": "취업자 수"}]

    kept = route_all(assignments, threshold=0.5)
    dropped = route_all(assignments, threshold=0.9)

    assert kept[0]["routing_class"] == KOSIS_CANDIDATE
    assert dropped[0]["routing_class"] == NOT_CLAIM
    assert dropped[0]["reason"].endswith("_BELOW_THRESHOLD")


def test_threshold_does_not_touch_confident_decisions():
    assignments = [{"source_subtype": "공식집계", "indicator_label": "취업자 수"}]

    routed = route_all(assignments, threshold=0.9)

    assert routed[0]["routing_class"] == KOSIS_CANDIDATE


def test_summary_counts_classes_and_reasons():
    routed = route_all([
        {"source_subtype": "공식집계", "indicator_label": "a"},
        {"source_subtype": "민간조사", "indicator_label": "b"},
        {"source_subtype": "", "indicator_label": ""},
    ])

    summary = routing_summary(routed)

    assert summary["classes"] == {
        KOSIS_CANDIDATE: 1, OUT_OF_SCOPE: 1, NOT_CLAIM: 1
    }


def _gold(span, cls):
    return {"article_idx": "1", "value_span_id": span, "judged_class": cls}


def _routed(span, subtype, indicator="지표"):
    return route_all([{
        "article_idx": "1", "value_span_id": span,
        "source_subtype": subtype, "indicator_label": indicator,
    }])[0]


def test_evaluate_routing_scores_precision_and_recall():
    gold = [_gold("a", KOSIS_CANDIDATE), _gold("b", OUT_OF_SCOPE)]
    routed = [_routed("a", "공식집계"), _routed("b", "민간조사")]

    result = evaluate_routing(gold, routed)

    assert result["true_positive"] == 1
    assert result["true_negative"] == 1
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0


def test_abstention_precision_reports_block_quality():
    """The metric that exposed the old contract at 0.330."""
    gold = [_gold("a", KOSIS_CANDIDATE), _gold("b", OUT_OF_SCOPE)]
    # both blocked: one deserved it, one did not
    routed = [_routed("a", "민간조사"), _routed("b", "민간조사")]

    result = evaluate_routing(gold, routed)

    assert result["abstention_precision"] == 0.5


def test_values_missing_from_prediction_count_as_lost_recall():
    gold = [_gold("a", KOSIS_CANDIDATE)]

    result = evaluate_routing(gold, [])

    assert result["values_missing_from_prediction"] == 1
    assert result["false_negative"] == 1
    assert result["recall"] == 0.0
