from src.develop.evaluate_l2_segmentation import (
    evaluate,
    label_similarity,
    match_labels,
    match_spans,
)
from src.develop.l2_segmentation import (
    build_l2_prompt,
    build_l2_schema,
    chunk_sentence_ids,
    resolve_prediction,
)

ARTICLE = "빵 물가 상승률은 38.5%다. 통계청에 따르면 취업자는 2909만명이다."


def test_schema_restricts_sentence_ids_to_the_article():
    schema = build_l2_schema(ARTICLE)

    ids = schema["properties"]["sentences"]["items"]["properties"][
        "sentence_id"
    ]["enum"]
    assert ids == [0, 1]


LONG = " ".join(f"{i}번 문장은 {i}%다." for i in range(40))


def test_chunking_covers_every_sentence_exactly_once():
    chunks = chunk_sentence_ids(LONG, size=15)
    flat = [i for chunk in chunks for i in chunk]

    assert flat == sorted(flat)
    assert len(flat) == len(set(flat))
    assert len(chunks) == 3


def test_chunk_schema_limits_targets_but_allows_pointing_backwards():
    chunks = chunk_sentence_ids(LONG, size=15)
    schema = build_l2_schema(LONG, chunks[1])

    props = schema["properties"]["sentences"]["items"]["properties"]
    assert props["sentence_id"]["enum"] == chunks[1]
    governing = props["source_region"]["properties"]["governing_sentence_id"]
    # a later chunk must still be able to inherit from the article's opening
    assert chunks[0][0] in governing["enum"]
    assert None in governing["enum"]


def test_split_schemas_ask_one_question_each():
    from src.develop.l2_segmentation import _split_schema

    source = _split_schema(ARTICLE, [0], pass_name="source")
    indicator = _split_schema(ARTICLE, [0], pass_name="indicator")

    source_props = source["properties"]["sentences"]["items"]["properties"]
    indicator_props = indicator["properties"]["sentences"]["items"]["properties"]
    assert "source_region" in source_props
    assert "indicator_scopes" not in source_props
    assert "indicator_scopes" in indicator_props
    assert "source_region" not in indicator_props


def test_split_source_schema_can_point_at_any_sentence():
    from src.develop.l2_segmentation import _split_schema

    schema = _split_schema(ARTICLE, [1], pass_name="source")
    governing = schema["properties"]["sentences"]["items"]["properties"][
        "source_region"
    ]["properties"]["governing_sentence_id"]

    assert 0 in governing["enum"]
    assert None in governing["enum"]


def test_prompt_lists_value_candidates_for_target_sentences():
    prompt = build_l2_prompt("t", ARTICLE, [0])

    assert "값:" in prompt
    assert "38.5%" in prompt


def test_prompt_omits_values_for_context_only_sentences():
    prompt = build_l2_prompt("t", ARTICLE, [0])
    tail = prompt.split("[1]")[-1]

    assert "값:" not in tail


def test_chunk_prompt_shows_whole_article_but_marks_targets():
    chunks = chunk_sentence_ids(LONG, size=15)
    prompt = build_l2_prompt("t", LONG, chunks[1])

    assert "▶ [15]" in prompt
    assert "   [0]" in prompt  # context only, not marked


def test_resolve_prediction_derives_dominance_from_the_pointer():
    resolved = resolve_prediction(ARTICLE, {"sentences": [
        {"sentence_id": 0, "indicator_scopes": [],
         "source_region": {"opens_region": True, "governing_sentence_id": 0,
                           "source_subtype": "공식집계"}},
        {"sentence_id": 1, "indicator_scopes": [],
         "source_region": {"opens_region": False, "governing_sentence_id": 0}},
    ]})

    assert resolved["sentences"][0]["source_region"]["dominance"] == "정의"
    assert resolved["sentences"][1]["source_region"]["dominance"] == "상속"


def test_resolve_prediction_treats_null_pointer_as_no_region():
    resolved = resolve_prediction(ARTICLE, {"sentences": [{
        "sentence_id": 0, "indicator_scopes": [],
        "source_region": {"opens_region": False, "governing_sentence_id": None},
    }]})

    assert resolved["sentences"][0]["source_region"]["dominance"] == "지배 없음"


def test_resolve_prediction_derives_offsets_from_span_text():
    resolved = resolve_prediction(ARTICLE, {"sentences": [{
        "sentence_id": 0,
        "indicator_scopes": [
            {"indicator_label": "빵 물가 상승률", "source_span_text": "빵 물가 상승률"}
        ],
        "source_region": {"opens_region": False, "governing_sentence_id": None},
    }]})

    scope = resolved["sentences"][0]["indicator_scopes"][0]
    assert scope["span_status"] == "RESOLVED"
    assert ARTICLE[
        scope["source_char_start"]:scope["source_char_end"]
    ] == "빵 물가 상승률"
    assert resolved["unresolved_spans"] == 0


def test_resolve_prediction_flags_invented_span():
    resolved = resolve_prediction(ARTICLE, {"sentences": [{
        "sentence_id": 0,
        "indicator_scopes": [
            {"indicator_label": "실업률", "source_span_text": "실업률"}
        ],
        "source_region": {"dominance": "지배 없음"},
    }]})

    scope = resolved["sentences"][0]["indicator_scopes"][0]
    assert scope["span_status"] == "UNRESOLVED"
    assert resolved["unresolved_spans"] == 1


def test_resolve_prediction_reports_skipped_sentences():
    resolved = resolve_prediction(ARTICLE, {"sentences": [{
        "sentence_id": 0,
        "indicator_scopes": [],
        "source_region": {"dominance": "지배 없음"},
    }]})

    assert resolved["missing_sentence_ids"] == [1]


def test_match_spans_pairs_overlapping_spans_only():
    assert match_spans([(0, 10)], [(0, 9)]) == 1
    assert match_spans([(0, 10)], [(40, 50)]) == 0
    assert match_spans([(0, 10), (20, 30)], [(20, 30)]) == 1


def test_label_similarity_credits_shared_tokens():
    assert label_similarity("단기 근로자 비율", "단기 근로자 비율") == 1.0
    assert label_similarity("단기 근로자 비율", "실업률") == 0.0
    assert 0 < label_similarity("단기 근로자 비율", "근로자 비율") < 1


def test_match_labels_pairs_each_gold_label_once():
    gold = ["단기 근로자 비율", "전체 근로자 수"]

    assert match_labels(gold, ["단기 근로자 비율"]) == 1
    assert match_labels(gold, ["단기 근로자 비율", "전체 근로자 수"]) == 2
    assert match_labels(gold, ["환율"]) == 0


def _gold(**overrides):
    row = {
        "article_idx": "1",
        "sentence_id": 0,
        "row_kind": "검토대상",
        "review_reason": "MULTI_INDICATOR_BOUNDARY",
        "indicator_spans": [(0, 7)],
        "indicator_labels": ["단기 근로자 비율"],
        "source_subtype": "공식집계",
        "dominance_class": "정의",
    }
    row.update(overrides)
    return row


def _pred(**overrides):
    row = {
        "article_idx": "1",
        "sentence_id": 0,
        "indicator_scopes": [{
            "indicator_label": "단기 근로자 비율",
            "source_char_start": 0,
            "source_char_end": 7,
            "span_status": "RESOLVED",
        }],
        "source_region": {"dominance": "정의", "source_subtype": "공식집계"},
    }
    row.update(overrides)
    return row


def test_evaluate_scores_a_perfect_sentence():
    result = evaluate([_gold()], [_pred()])

    assert result["primary_metric"] == "indicator_label"
    assert result["indicator_label"]["f1"] == 1.0
    assert result["source_subtype_accuracy"] == 1.0
    assert result["source_region_dominance_accuracy"] == 1.0
    assert result["indicator_scope_count_accuracy"] == 1.0


def test_evaluate_penalises_wrong_subtype_but_credits_dominance():
    result = evaluate([_gold()], [_pred(
        source_region={"dominance": "정의", "source_subtype": "민간조사"}
    )])

    assert result["source_subtype_accuracy"] == 0.0
    assert result["source_region_dominance_accuracy"] == 1.0


def test_evaluate_follows_inherited_region_to_its_defining_sentence():
    """An inherited sentence names the opener instead of repeating subtype."""
    gold = [_gold(sentence_id=1, dominance_class="상속")]
    predictions = [
        _pred(sentence_id=0),
        _pred(
            sentence_id=1,
            source_region={
                "dominance": "상속",
                "introduced_in_sentence_id": 0,
            },
        ),
    ]

    result = evaluate(gold, predictions)

    assert result["source_subtype_accuracy"] == 1.0


def test_evaluate_excludes_undecided_gold_from_the_denominator():
    result = evaluate([_gold(dominance_class="판단 불가")], [_pred()])

    assert result["unmeasurable_sentences"] == 1
    assert result["dominance_scored"] == 0


def test_evaluate_excludes_gold_contradicted_by_its_own_sentence():
    """`통계청에 따르면 …` cannot also be governed by no source."""
    result = evaluate([_gold(dominance_class="모순")], [_pred()])

    assert result["dominance_scored"] == 0
    assert result["unmeasurable_reasons"] == {"모순": 1}
    # the indicator layer is still graded on that sentence
    assert result["indicator_label"]["f1"] == 1.0


def test_evaluate_counts_sentences_absent_from_prediction():
    result = evaluate([_gold()], [])

    assert result["sentences_missing_from_prediction"] == 1
    assert result["indicator_label"]["recall"] == 0.0
