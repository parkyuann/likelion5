from src.develop.article_claim_pipeline import (
    SPAN_BINDING_SCHEMA,
    CLAIM_SKELETON_SCHEMA,
    SPAN_BINDING_SYSTEM_PROMPT,
    build_semantic_article_prompt,
    build_claim_skeleton_candidate_catalog,
    build_claim_skeleton_prompt,
    build_claim_skeleton_schema,
    complete_claim_skeleton_candidate_coverage,
    build_semantic_evidence_candidates,
    build_span_binding_prompt,
    build_span_binding_schema,
    build_span_candidates,
    apply_article_relative_period_context,
    filter_span_candidates_for_measurement_type,
    filter_span_candidates_for_target_selection,
    build_article_prompt,
    pass_observations,
    pass_scope_bound_observations,
    pass_span_bound_observations,
    sentence_offset_map,
    normalize_claim_skeleton_candidate_selection,
    validate_article_prediction,
    validate_semantic_claim,
    validate_claim_skeleton,
    validate_claim_observation_scope,
    validate_span_binding,
)
from src.develop import article_claim_pipeline


ARTICLE = "서울의 고용률은 45.1%였다. 전년보다 1.2%p 높았다."
PREDICTION = {"claims": [{
    "is_kosis_candidate": True, "evidence_sentence_ids": [0], "context_sentence_ids": [0], "evidence_quote": "서울의 고용률은 45.1%였다.",
    "indicator_evidence_texts": ["서울", "고용률"], "indicator_norm": "고용률", "dimensions": {"지역": ["서울"]},
    "dimension_source_texts": {"지역": ["서울"]}, "relation_json": {},
    "observations": [{"value": "45.1", "unit": "%", "evidence_sentence_ids": [0], "value_unit_evidence_text": "45.1%", "period": None, "period_evidence_text": None}],
}]}


def test_article_offset_map_and_pass_validation_are_grounded_in_source_text():
    offsets = sentence_offset_map(ARTICLE)
    report = validate_article_prediction(ARTICLE, PREDICTION)

    assert len(offsets) == 2
    assert report["claims"][0]["indicator_evidence_spans"][1]["text"] == "고용률"
    assert report["claims"][0]["dimension_source_spans"]["지역"][0]["text"] == "서울"
    assert report["pass_observation_count"] == 1
    assert len(pass_observations(PREDICTION, report)) == 1


def test_value_not_present_in_evidence_is_blocked_before_retrieval():
    prediction = {**PREDICTION, "claims": [{**PREDICTION["claims"][0], "observations": [{"value": "44.0", "unit": "%", "evidence_sentence_ids": [0], "value_unit_evidence_text": "44.0%", "period": None, "period_evidence_text": None}]}]}

    report = validate_article_prediction(ARTICLE, prediction)

    assert report["claims"][0]["observations"][0]["status"] == "CONFLICT"
    assert pass_observations(prediction, report) == []


def test_prompt_contains_numbered_article_sentences_for_hcx_grounding():
    prompt = build_article_prompt("고용 기사", ARTICLE)

    assert "[0] 서울의 고용률은 45.1%였다." in prompt
    assert "[1] 전년보다 1.2%p 높았다." in prompt


def test_span_candidates_preserve_source_offsets_and_exclude_age_as_value():
    article = "15~29세 서울 청년층의 고용률은 45.1%였다. 전년보다 1.2%p 높았다."
    candidates = build_span_candidates(article)

    assert any(item["kind"] == "dimension" and item["text"] == "15~29세" for item in candidates)
    assert any(item["kind"] == "dimension" and item["text"] == "서울" for item in candidates)
    assert {(item["text"], item.get("unit")) for item in candidates if item["kind"] == "value_unit"} == {("45.1%", "%"), ("1.2%p", "%p")}
    assert all(article[item["char_start"]:item["char_end"]] == item["text"] for item in candidates)
    assert len({item["span_id"] for item in candidates}) == len(candidates)


def test_span_binding_contract_requires_target_metric_relation_evidence():
    observation = SPAN_BINDING_SCHEMA["properties"]["observations"]["items"]

    assert {"value_role", "indicator_value_relation", "relation_evidence_sentence_ids"} <= set(observation["required"])
    assert "TARGET_MEASURE" in observation["properties"]["value_role"]["enum"]
    assert "SAME_METRIC" in observation["properties"]["indicator_value_relation"]["enum"]
    assert SPAN_BINDING_SCHEMA["properties"]["observations"]["maxItems"] == 1
    assert "measurement_type" in SPAN_BINDING_SYSTEM_PROMPT
    assert "TARGET_MEASURE" in SPAN_BINDING_SYSTEM_PROMPT
    assert {
        "indicator_evidence_span_ids",
        "population_evidence_span_ids",
        "item_evidence_span_ids",
    } <= set(SPAN_BINDING_SCHEMA["required"])


def test_span_binding_schema_constrains_all_source_ids_to_local_candidates():
    article = "개인사업자의 보험사 연체율은 1.46%로 집계됐다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    schema = build_span_binding_schema(
        candidates,
        {
            "indicator_norm": "보험사 연체율",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
            "target_value_span_ids": [
                item["span_id"]
                for item in candidates
                if item["kind"] == "value_unit"
            ],
        },
    )
    properties = schema["properties"]
    semantic_ids = {
        item["span_id"] for item in candidates if item["kind"] == "semantic_evidence"
    }
    observation = properties["observations"]["items"]["properties"]

    assert set(properties["indicator_evidence_span_ids"]["items"]["enum"]) == semantic_ids
    assert set(observation["value_span_id"]["enum"]) == {
        item["span_id"] for item in candidates if item["kind"] == "value_unit"
    }
    assert "enum" not in observation["period_span_id"]
    assert set(observation["dimension_span_ids"]["items"]["enum"]) == {
        item["span_id"]
        for item in candidates
        if item["kind"] == "semantic_evidence" and item["text"] == "보험사"
    }


def test_span_binding_validates_and_returns_source_backed_semantic_roles():
    article = "개인사업자의 보험사 연체율은 1.46%로 집계됐다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    claim = {
        "indicator_norm": "보험사 연체율",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
    }
    binding = {
        "indicator_evidence_span_ids": [by_text["보험사"], by_text["연체율"]],
        "population_evidence_span_ids": [by_text["개인사업자"]],
        "item_evidence_span_ids": [],
        "observations": [{
            "value_span_id": by_text["1.46%"],
            "measurement_type": "LEVEL",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }

    report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert report["claim_status"] == "PASS"
    assert report["semantic_role_evidence"]["unsupported_anchor_terms"] == []
    assert [item["text"] for item in report["semantic_role_evidence"]["population_evidence_spans"]] == ["개인사업자"]


def test_span_binding_recovers_population_sector_dimension_and_shared_indicator_span():
    article = "개인사업자의 보험사 연체율은 1.46%로 집계됐다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    binding = {
        "indicator_evidence_span_ids": [by_text["보험사"], by_text["연체율"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [],
        "observations": [{
            "value_span_id": by_text["1.46%"],
            "measurement_type": "LEVEL",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }

    report = validate_span_binding(
        {
            "indicator_norm": "보험사 개인사업자대출 연체율",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
        },
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert report["claim_status"] == "PASS"
    assert [span["text"] for span in report["semantic_role_evidence"]["population_evidence_spans"]] == ["개인사업자"]
    assert report["semantic_role_evidence"]["item_evidence_spans"] == []
    assert [span["text"] for span in report["observations"][0]["dimension_spans"]] == ["보험사"]


def test_span_binding_recovers_indicator_local_item_without_selecting_other_value_item():
    article = "전년 동월과 비교해 지난달 사과는 21.6%, 쌀은 21.3% 올랐다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    binding = {
        "indicator_evidence_span_ids": [by_text["쌀은"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [],
        "observations": [{
            "value_span_id": by_text["21.3%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": by_text["지난달"],
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }

    report = validate_span_binding(
        {"indicator_norm": "쌀 상승률", "context_sentence_ids": [0], "observation_sentence_ids": [0]},
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert report["claim_status"] == "PASS"
    assert [span["text"] for span in report["semantic_role_evidence"]["item_evidence_spans"]] == ["쌀은"]
    assert by_text["쌀은"] in report["semantic_role_evidence"]["shared_indicator_role_span_ids"]


def test_span_binding_recovers_loan_item_and_composite_exclusion_dimension():
    article = "개인사업자대출 연체율은 3.67%였다. 경제협력개발기구(OECD) 기준 근원물가(식료품·에너지 제외)는 2.2% 상승했다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    loan_binding = {
        "indicator_evidence_span_ids": [by_text["연체율"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [],
        "observations": [{
            "value_span_id": by_text["3.67%"],
            "measurement_type": "LEVEL",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }
    exclusion_binding = {
        "indicator_evidence_span_ids": [by_text["근원물가"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [],
        "observations": [{
            "value_span_id": by_text["2.2%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [1],
        }],
    }

    loan_report = validate_span_binding(
        {"indicator_norm": "연체율", "context_sentence_ids": [0], "observation_sentence_ids": [0]},
        loan_binding,
        [candidate for candidate in candidates if candidate["sentence_id"] == 0],
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )
    exclusion_report = validate_span_binding(
        {"indicator_norm": "OECD 기준 근원물가 상승률", "context_sentence_ids": [1], "observation_sentence_ids": [1]},
        exclusion_binding,
        [candidate for candidate in candidates if candidate["sentence_id"] == 1],
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert [span["text"] for span in loan_report["semantic_role_evidence"]["item_evidence_spans"]] == ["개인사업자대출"]
    assert exclusion_report["semantic_role_evidence"]["item_evidence_spans"] == []
    assert [span["text"] for span in exclusion_report["observations"][0]["dimension_spans"]] == ["식료품·에너지", "제외"]
    exclusion_scope = validate_claim_observation_scope(
        article,
        {
            "indicator_norm": "OECD 기준 근원물가 상승률",
            "measurement_type": "CHANGE_RATE",
            "context_sentence_ids": [1],
            "observation_sentence_ids": [1],
            "relation_json": {
                "dimension_pairing": "NOT_APPLICABLE",
                "pairing_evidence_sentence_ids": [],
            },
        },
        exclusion_report,
    )
    assert exclusion_scope["claim_status"] == "PASS"
    assert "DIMENSION_MULTIPLE_VALUES_FOR_SINGLE_TARGET" not in exclusion_scope["errors"]


def test_span_binding_filters_unhinted_causal_semantic_dimension():
    article = "소매판매는 내구재 소비가 늘면서 전월 대비 1.5% 늘었다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    binding = {
        "indicator_evidence_span_ids": [by_text["소매판매는"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [],
        "observations": [{
            "value_span_id": by_text["1.5%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": None,
            "dimension_span_ids": [by_text["내구재"]],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }

    report = validate_span_binding(
        {
            "indicator_norm": "소매판매",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
            "relation_json": {
                "dimension_pairing": "NOT_APPLICABLE",
                "pairing_evidence_sentence_ids": [],
            },
        },
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert report["claim_status"] == "PASS"
    assert report["observations"][0]["dimension_spans"] == []
    assert [span["text"] for span in report["observations"][0]["filtered_dimension_spans"]] == ["내구재"]


def test_span_binding_filters_trailing_explanatory_dimensions_from_target():
    article = "건설 수주액은 6.9% 감소해, 향후 건설업 경기에 대한 우려도 남겼다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    binding = {
        "indicator_evidence_span_ids": [by_text["건설"], by_text["수주액"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [by_text["건설"]],
        "observations": [{
            "value_span_id": by_text["6.9%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": None,
            "dimension_span_ids": [by_text["건설업"], by_text["경기"]],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }
    claim = {
        "indicator_norm": "건설 수주액",
        "measurement_type": "CHANGE_RATE",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {
            "dimension_pairing": "NOT_APPLICABLE",
            "pairing_evidence_sentence_ids": [],
        },
    }

    report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )
    scope = validate_claim_observation_scope(article, claim, report)

    assert report["claim_status"] == "PASS"
    assert report["semantic_role_evidence"]["item_evidence_spans"] == []
    assert report["observations"][0]["dimension_spans"] == []
    assert {span["text"] for span in report["observations"][0]["filtered_dimension_spans"]} == {"건설업", "경기"}
    assert scope["claim_status"] == "PASS"


def test_span_binding_deterministically_recovers_a_source_anchor_omitted_by_hcx():
    article = "여신전문금융사에서 개인사업자대출 연체율은 3.67%였다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    binding = {
        "indicator_evidence_span_ids": [by_text["연체율"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [by_text["개인사업자대출"]],
        "observations": [{
            "value_span_id": by_text["3.67%"],
            "measurement_type": "LEVEL",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }

    report = validate_span_binding(
        {"indicator_norm": "여신전문금융사 연체율", "context_sentence_ids": [0], "observation_sentence_ids": [0]},
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert report["claim_status"] == "PASS"
    assert [item["text"] for item in report["semantic_role_evidence"]["indicator_evidence_spans_raw"]] == ["연체율"]
    assert [item["text"] for item in report["semantic_role_evidence"]["recovered_indicator_evidence_spans"]] == ["여신전문금융사"]
    assert report["semantic_role_evidence"]["contract_status"] == "ASSERTED_PLUS_DETERMINISTIC_ANCHOR_RECOVERY"


def test_span_binding_anchor_recovery_keeps_parenthetical_aliases():
    article = "경제협력개발기구(OECD) 기준 근원물가는 2.2% 상승했다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    binding = {
        "indicator_evidence_span_ids": [by_text["근원물가"]],
        "population_evidence_span_ids": [],
        "item_evidence_span_ids": [],
        "observations": [{
            "value_span_id": by_text["2.2%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }

    report = validate_span_binding(
        {"indicator_norm": "OECD 기준 근원물가 상승률", "context_sentence_ids": [0], "observation_sentence_ids": [0]},
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert report["claim_status"] == "PASS"
    assert "OECD" in report["semantic_role_evidence"]["supported_anchor_terms"]
    assert any(
        "OECD" in item["text"]
        for item in report["semantic_role_evidence"]["recovered_indicator_evidence_spans"]
    )


def test_span_binding_blocks_semantic_role_overlap_and_unsupported_indicator_anchor():
    article = "개인사업자의 보험사 연체율은 1.46%로 집계됐다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {}
    for item in candidates:
        by_text.setdefault(item["text"], item["span_id"])
    binding = {
        "indicator_evidence_span_ids": [by_text["연체율"]],
        "population_evidence_span_ids": [by_text["개인사업자"]],
        "item_evidence_span_ids": [by_text["개인사업자"]],
        "observations": [{
            "value_span_id": by_text["1.46%"],
            "measurement_type": "LEVEL",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }],
    }

    report = validate_span_binding(
        {"indicator_norm": "시중은행 연체율", "context_sentence_ids": [0], "observation_sentence_ids": [0]},
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        require_semantic_evidence=True,
    )

    assert report["claim_status"] == "CONFLICT"
    assert "SEMANTIC_EVIDENCE_ROLE_OVERLAP" in report["errors"]
    assert "INDICATOR_NORM_ANCHOR_NOT_SUPPORTED_BY_SELECTED_EVIDENCE" in report["errors"]


def test_claim_skeleton_contract_excludes_free_text_constraints_and_measurement_type():
    claim = CLAIM_SKELETON_SCHEMA["properties"]["claims"]["items"]
    prompt = build_claim_skeleton_prompt("고용 기사", ARTICLE)

    assert "measurement_type" not in claim["required"]
    assert "population_constraints" not in claim["properties"]
    assert "population_evidence_candidate_ids" not in claim["properties"]
    assert "item_evidence_candidate_ids" not in claim["properties"]
    assert "comparison_constraints" not in claim["properties"]
    assert "relation_json" not in claim["properties"]
    assert "값·단위·기간·비교 문자열을 새로 반환하지 말고" in prompt
    assert "candidate 목록" in prompt
    assert "context_sentence_ids" not in claim["properties"]
    assert "observation_sentence_ids" not in claim["properties"]
    assert "제공한 sentence_candidate_id만" in claim["properties"]["context_sentence_candidate_ids"]["items"]["description"]
    assert "직접 측정값" in claim["properties"]["target_value_candidate_ids"]["items"]["description"]
    assert claim["properties"]["target_value_candidate_ids"]["maxItems"] == 1
    assert "값·날짜·조사기관·서술어가 없는" in claim["properties"]["indicator_norm"]["description"]
    assert claim["properties"]["candidate_class"]["enum"] == [
        "KOSIS_CANDIDATE", "OUT_OF_SCOPE", "NOT_CLAIM", "AMBIGUOUS",
    ]
    assert "모든 제공 수치 후보" in prompt


def test_claim_skeleton_schema_requires_one_record_per_requested_target():
    target_ids = [
        row["value_candidate_id"]
        for row in build_claim_skeleton_candidate_catalog(ARTICLE)["value_candidates"]
        if row["selectable_as_target"]
    ][:2]

    schema = build_claim_skeleton_schema(ARTICLE, target_ids)

    assert schema["properties"]["claims"]["minItems"] == len(target_ids)
    assert schema["properties"]["claims"]["maxItems"] == len(target_ids)


def test_candidate_first_coverage_deduplicates_and_fills_missing_targets():
    target_ids = [
        row["value_candidate_id"]
        for row in build_claim_skeleton_candidate_catalog(ARTICLE)["value_candidates"]
        if row["selectable_as_target"]
    ][:2]
    duplicate = {
        "candidate_class": "KOSIS_CANDIDATE",
        "is_kosis_candidate": True,
        "claim_type": "고용",
        "indicator_norm": "고용률",
        "context_sentence_candidate_ids": ["sent_s0"],
        "target_value_candidate_ids": [target_ids[0]],
    }

    completed, audit = complete_claim_skeleton_candidate_coverage(
        ARTICLE,
        [duplicate, duplicate],
        target_ids,
    )

    assert [row["target_value_candidate_ids"][0] for row in completed] == target_ids
    assert completed[0]["candidate_coverage_source"] == "HCX"
    assert completed[1]["candidate_class"] == "AMBIGUOUS"
    assert completed[1]["candidate_coverage_source"] == "DETERMINISTIC_AMBIGUOUS_FALLBACK"
    assert audit["fallback_record_count"] == 1
    assert audit["duplicate_target_ids"] == [target_ids[0]]


def test_claim_skeleton_retries_only_missing_candidate_ids(monkeypatch):
    article = "취업자는 100명이고 고용률은 60%였다."
    target_ids = [
        row["value_candidate_id"]
        for row in build_claim_skeleton_candidate_catalog(article)[
            "value_candidates"
        ]
    ]
    calls = []

    def fake_call(*, schema, **kwargs):
        allowed = schema["properties"]["claims"]["items"]["properties"][
            "target_value_candidate_ids"
        ]["items"]["enum"]
        calls.append(list(allowed))
        returned = allowed[:-1] if len(calls) == 1 else allowed
        return {
            "claims": [{
                "candidate_class": "KOSIS_CANDIDATE",
                "classification_reason": "OFFICIAL_AGGREGATE",
                "is_kosis_candidate": True,
                "claim_type": "고용",
                "indicator_norm": "고용 지표",
                "context_sentence_candidate_ids": ["sentence:s0000"],
                "target_value_candidate_ids": [target_id],
            } for target_id in returned],
        }, {"totalTokens": len(returned)}, 1.0

    monkeypatch.setattr(article_claim_pipeline, "_call_hcx_json", fake_call)

    prediction, usage, latency = article_claim_pipeline.call_hcx_claim_skeleton(
        "고용",
        article,
        api_key="test",
    )

    assert calls == [target_ids, [target_ids[-1]]]
    assert [
        claim["target_value_span_ids"][0]
        for claim in prediction["claims"]
    ] == target_ids
    assert all(
        claim["candidate_coverage_source"] == "HCX"
        for claim in prediction["claims"]
    )
    assert usage["candidate_retry_calls"] == 1
    assert latency == 2.0


def test_candidate_first_model_non_kosis_class_is_audit_only_until_scope():
    target_id = next(
        item["span_id"] for item in build_span_candidates(ARTICLE)
        if item.get("text") == "45.1%"
    )
    skeleton = {
        "candidate_class": "OUT_OF_SCOPE",
        "is_kosis_candidate": False,
        "claim_type": "민간통계",
        "indicator_norm": "고용률",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "target_value_span_ids": [target_id],
    }

    effective, report = validate_claim_skeleton(ARTICLE, skeleton)

    assert effective["candidate_class"] == "OUT_OF_SCOPE"
    assert report["status"] == "PASS"
    assert report["candidate_classification"]["binding_eligible"] is True
    assert (
        report["candidate_classification"]["routing_policy"]
        == "MODEL_CLASS_AUDIT_ONLY"
    )


def test_candidate_first_deterministically_blocks_definition_threshold():
    article = "지난해 주 14시간 이하로 일하는 초단기 근로자는 174만2000명이었다."
    catalog = build_claim_skeleton_candidate_catalog(article)
    threshold = next(
        row for row in catalog["value_candidates"]
        if row["text"] == "14시간"
    )
    raw = {
        "candidate_class": "KOSIS_CANDIDATE",
        "classification_reason": "OFFICIAL_AGGREGATE",
        "is_kosis_candidate": True,
        "claim_type": "근로시간",
        "indicator_norm": "초단기 근로시간",
        "context_sentence_candidate_ids": [
            threshold["sentence_candidate_id"],
        ],
        "target_value_candidate_ids": [
            threshold["value_candidate_id"],
        ],
    }

    completed, _ = complete_claim_skeleton_candidate_coverage(
        article,
        [raw],
        [threshold["value_candidate_id"]],
    )

    assert threshold["source_role_hint"] == "CATEGORY_DEFINITION"
    assert completed[0]["candidate_class"] == "NOT_CLAIM"
    assert completed[0]["classification_reason"] == "DEFINITION_OR_THRESHOLD"
    assert completed[0]["candidate_class_override"] == "DETERMINISTIC_CATEGORY_DEFINITION"


def test_relative_period_normalization_uses_publication_date():
    article = "지난달 소비자물가는 올랐고 지난해 2분기 생산도 증가했다."
    periods = {
        row["text"]: row
        for row in build_span_candidates(article)
        if row["kind"] == "time"
    }

    assert article_claim_pipeline._normalize_local_period_text(
        periods["지난달"],
        article,
        reference_date="2025-04-02",
    ) == "2025년 3월"
    assert article_claim_pipeline._normalize_local_period_text(
        periods["지난해 2분기"],
        article,
        reference_date="2025-04-02",
    ) == "2024년 2분기"


def test_kosis_historical_comparison_value_can_form_independent_claim():
    article = "고용률은 올해 20%로 지난해(18%)보다 상승했다."
    value_span = next(
        row for row in build_span_candidates(article)
        if row.get("text") == "18%"
    )
    skeleton = {
        "candidate_class": "KOSIS_CANDIDATE",
        "classification_reason": "OFFICIAL_AGGREGATE",
        "is_kosis_candidate": True,
        "claim_type": "고용",
        "indicator_norm": "고용률",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "target_value_span_ids": [value_span["span_id"]],
    }

    effective, semantic_report = validate_claim_skeleton(article, skeleton)
    scope_report = validate_claim_observation_scope(
        article,
        effective,
        {
            "observations": [{
                "observation_index": 0,
                "status": "PASS",
                "measurement_type": "LEVEL",
                "value_span": value_span,
                "dimension_spans": [],
            }],
        },
    )

    assert semantic_report["status"] == "PASS"
    assert scope_report["claim_status"] == "PASS"


def test_claim_skeleton_validation_adds_neutral_relation_after_model_stage():
    target_id = next(
        item["span_id"] for item in build_span_candidates(ARTICLE)
        if item.get("text") == "45.1%"
    )
    skeleton = {
        "is_kosis_candidate": True, "claim_type": "고용", "indicator_norm": "고용률",
        "context_sentence_ids": [0], "observation_sentence_ids": [0],
        "target_value_span_ids": [target_id],
    }

    effective, report = validate_claim_skeleton(ARTICLE, skeleton)

    assert report["status"] == "PASS"
    assert effective["relation_json"] == {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}
    assert report["indicator_contract"]["indicator_source_sentence_ids"] == [0]
    assert report["indicator_contract"]["observation_value_sentence_ids"] == [0]


def test_claim_skeleton_validation_blocks_value_sentence_and_source_org_as_indicator():
    article = "지난달 소비자물가 상승률이 2.4%를 기록했다."
    target_id = next(
        item["span_id"] for item in build_span_candidates(article)
        if item.get("text") == "2.4%"
    )
    skeletons = [
        ("2.4%", "INDICATOR_NORM_CONTAINS_VALUE"),
        ("지난달 소비자물가 상승률은 2.4%", "INDICATOR_NORM_CONTAINS_VALUE"),
        ("통계청 소비자물가 상승률", "INDICATOR_NORM_CONTAINS_SOURCE_ORG"),
        ("소비자물가 상승률이 증가함", "INDICATOR_NORM_CONTAINS_PREDICATE"),
    ]

    for indicator_norm, expected_error in skeletons:
        _, report = validate_claim_skeleton(article, {
            "is_kosis_candidate": True,
            "claim_type": "물가",
            "indicator_norm": indicator_norm,
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
            "target_value_span_ids": [target_id],
        })

        assert report["status"] == "CONFLICT"
        assert expected_error in report["errors"]


def test_claim_skeleton_validation_recovers_local_metric_and_requires_value_sentence():
    article = "지난달 소비자물가 상승률이 2.4%를 기록했다. 후속 설명이다."
    target_id = next(
        item["span_id"] for item in build_span_candidates(article)
        if item.get("text") == "2.4%"
    )

    effective, recovered = validate_claim_skeleton(article, {
        "is_kosis_candidate": True,
        "claim_type": "고용",
        "indicator_norm": "고용률",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "target_value_span_ids": [target_id],
    })
    _, value_missing = validate_claim_skeleton(article, {
        "is_kosis_candidate": True,
        "claim_type": "물가",
        "indicator_norm": "소비자물가 상승률",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [1],
        "target_value_span_ids": [target_id],
    })

    assert recovered["status"] == "PASS"
    assert effective["indicator_norm"] == "소비자물가 상승률"
    assert recovered["indicator_recovery"]["source"] == "LOCAL_METRIC_PHRASE_RULE"
    assert "OBSERVATION_SENTENCE_VALUE_MISSING" in value_missing["errors"]
    assert "TARGET_VALUE_OBSERVATION_SENTENCE_MISMATCH" in value_missing["errors"]


def test_claim_skeleton_local_indicator_recovery_keeps_region_and_industry_phrase():
    article = "올해 1분기 전국 건설업 성장률은 전년 동기 대비 -12.4%였다."
    target_id = next(
        item["span_id"] for item in build_span_candidates(article)
        if item.get("text") == "-12.4%"
    )

    effective, report = validate_claim_skeleton(article, {
        "is_kosis_candidate": True,
        "claim_type": "지역경제",
        "indicator_norm": "실질 지역내총생산 증가율",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "target_value_span_ids": [target_id],
    })

    assert report["status"] == "PASS"
    assert effective["indicator_norm"] == "전국 건설업 성장률"
    assert report["indicator_recovery"]["raw_indicator_norm"] == "실질 지역내총생산 증가율"


def test_span_candidate_ids_are_stable_between_whole_article_and_selected_sentences():
    article = "고용률은 45.1%였다. 실업률은 3.2%였다."
    whole = build_span_candidates(article)
    selected = build_span_candidates(article, [1])

    whole_ids = {
        item["span_id"] for item in whole
        if item["sentence_id"] == 1
    }

    assert {item["span_id"] for item in selected} == whole_ids


def test_semantic_evidence_candidates_preserve_source_spans_and_strip_only_particles():
    article = "개인사업자의 보험사 연체율은 1.46%로 집계됐다."
    candidates = build_semantic_evidence_candidates(article)
    by_text = {item["text"]: item for item in candidates}

    assert {"개인사업자", "보험사", "연체율"} <= set(by_text)
    assert all(article[item["char_start"]:item["char_end"]] == item["text"] for item in candidates)
    assert len({item["span_id"] for item in candidates}) == len(candidates)


def test_candidate_skeleton_resolves_semantic_roles_and_requires_selected_indicator_support():
    article = "개인사업자의 보험사 연체율은 1.46%로 집계됐다."
    catalog = build_claim_skeleton_candidate_catalog(article, include_semantic_evidence=True)
    evidence_by_text = {
        item["text"]: item["semantic_evidence_candidate_id"]
        for item in catalog["semantic_evidence_candidates"]
    }
    prediction = {"claims": [{
        "is_kosis_candidate": True,
        "claim_type": "연체율",
        "indicator_norm": "보험사 연체율",
        "context_sentence_candidate_ids": ["sentence:s0000"],
        "target_value_candidate_ids": [catalog["value_candidates"][0]["value_candidate_id"]],
        "indicator_evidence_candidate_ids": [evidence_by_text["보험사"], evidence_by_text["연체율"]],
        "population_evidence_candidate_ids": [evidence_by_text["개인사업자"]],
        "item_evidence_candidate_ids": [],
        "dimension_candidate_ids": [],
    }]}

    claim = normalize_claim_skeleton_candidate_selection(article, prediction)["claims"][0]
    _, report = validate_claim_skeleton(article, claim)

    assert report["status"] == "PASS"
    assert claim["indicator_evidence_texts"] == ["보험사", "연체율"]
    assert claim["population_constraints"] == ["개인사업자"]
    assert report["indicator_contract"]["unsupported_anchor_terms"] == []


def test_candidate_skeleton_blocks_unsupported_indicator_anchor_and_role_overlap():
    article = "개인사업자의 보험사 연체율은 1.46%로 집계됐다."
    catalog = build_claim_skeleton_candidate_catalog(article, include_semantic_evidence=True)
    evidence_by_text = {
        item["text"]: item["semantic_evidence_candidate_id"]
        for item in catalog["semantic_evidence_candidates"]
    }
    prediction = {"claims": [{
        "is_kosis_candidate": True,
        "claim_type": "연체율",
        "indicator_norm": "저축은행 연체율",
        "context_sentence_candidate_ids": ["sentence:s0000"],
        "target_value_candidate_ids": [catalog["value_candidates"][0]["value_candidate_id"]],
        "indicator_evidence_candidate_ids": [evidence_by_text["연체율"]],
        "population_evidence_candidate_ids": [evidence_by_text["개인사업자"]],
        "item_evidence_candidate_ids": [evidence_by_text["개인사업자"]],
        "dimension_candidate_ids": [],
    }]}

    claim = normalize_claim_skeleton_candidate_selection(article, prediction)["claims"][0]
    _, report = validate_claim_skeleton(article, claim)

    assert report["status"] == "CONFLICT"
    assert "INDICATOR_NORM_ANCHOR_NOT_SUPPORTED_BY_SELECTED_EVIDENCE" in report["errors"]
    assert "SEMANTIC_EVIDENCE_ROLE_OVERLAP" in report["errors"]
    assert report["indicator_contract"]["unsupported_anchor_terms"] == ["저축은행"]


def test_candidate_skeleton_schema_and_normalization_resolve_only_source_ids():
    article = "지난달 소비자물가 상승률이 2.4%를 기록했다."
    catalog = build_claim_skeleton_candidate_catalog(article, include_semantic_evidence=True)
    target_id = catalog["value_candidates"][0]["value_candidate_id"]
    evidence_by_text = {
        item["text"]: item["semantic_evidence_candidate_id"]
        for item in catalog["semantic_evidence_candidates"]
    }
    schema = build_claim_skeleton_schema(article)
    claim_schema = schema["properties"]["claims"]["items"]["properties"]
    prediction = {"claims": [{
        "is_kosis_candidate": True,
        "claim_type": "물가",
        "indicator_norm": "소비자물가 상승률",
        "context_sentence_candidate_ids": ["sentence:s0000"],
        "target_value_candidate_ids": [target_id],
        "indicator_evidence_candidate_ids": [evidence_by_text["소비자물가"], evidence_by_text["상승률"]],
        "population_evidence_candidate_ids": [],
        "item_evidence_candidate_ids": [],
        "dimension_candidate_ids": [],
    }]}

    normalized = normalize_claim_skeleton_candidate_selection(article, prediction)
    claim = normalized["claims"][0]
    _, report = validate_claim_skeleton(article, claim)

    assert claim_schema["context_sentence_candidate_ids"]["items"]["enum"] == ["sentence:s0000"]
    assert claim_schema["target_value_candidate_ids"]["items"]["enum"] == [target_id]
    assert claim["context_sentence_ids"] == [0]
    assert claim["observation_sentence_ids"] == [0]
    assert claim["target_value_span_ids"] == [target_id]
    assert claim["indicator_evidence_texts"] == ["소비자물가", "상승률"]
    assert claim["population_constraints"] == []
    assert claim["item_constraints"] == []
    assert claim["candidate_selection"]["errors"] == []
    assert report["status"] == "PASS"


def test_candidate_skeleton_normalization_blocks_unknown_candidate_ids():
    article = "지난달 소비자물가 상승률이 2.4%를 기록했다."
    prediction = {"claims": [{
        "is_kosis_candidate": True,
        "claim_type": "물가",
        "indicator_norm": "소비자물가 상승률",
        "context_sentence_candidate_ids": ["sentence:s9999"],
        "target_value_candidate_ids": ["unknown:value"],
        "indicator_evidence_candidate_ids": ["unknown:evidence"],
        "population_evidence_candidate_ids": [],
        "item_evidence_candidate_ids": [],
        "dimension_candidate_ids": [],
    }]}

    claim = normalize_claim_skeleton_candidate_selection(article, prediction)["claims"][0]
    _, report = validate_claim_skeleton(article, claim)

    assert report["status"] == "CONFLICT"
    assert "CONTEXT_SENTENCE_CANDIDATE_ID_UNKNOWN" in report["errors"]
    assert "TARGET_VALUE_CANDIDATE_ID_UNKNOWN" in report["errors"]
    assert "INDICATOR_EVIDENCE_CANDIDATE_ID_UNKNOWN" in report["errors"]


def test_candidate_skeleton_filter_exposes_only_selected_target_values_to_binding():
    article = "서울 소비자물가 상승률은 2.4%였고 전월보다 0.3%p 높았다."
    candidates = build_span_candidates(article)
    target_id = next(item["span_id"] for item in candidates if item.get("text") == "2.4%")
    dimension_id = next(item["span_id"] for item in candidates if item.get("text") == "서울")

    filtered, audit = filter_span_candidates_for_target_selection(
        {"target_value_span_ids": [target_id], "dimension_span_ids": [dimension_id]},
        candidates,
    )

    assert {
        item["text"] for item in filtered if item["kind"] == "value_unit"
    } == {"2.4%"}
    assert audit["missing_target_value_span_ids"] == []
    assert audit["binding_value_candidate_count"] == 1
    assert {item["text"] for item in filtered if item["kind"] == "dimension"} == {"서울"}


def test_candidate_first_skeleton_classifies_historical_comparison_values_too():
    article = "연체율은 3.67%로 2014년 2분기(3.69%) 이래 가장 높았다."
    catalog = build_claim_skeleton_candidate_catalog(article)
    by_text = {item["text"]: item for item in catalog["value_candidates"]}
    target_enum = build_claim_skeleton_schema(article)["properties"]["claims"]["items"]["properties"]["target_value_candidate_ids"]["items"]["enum"]

    assert by_text["3.67%"]["source_role_hint"] == "TARGET_CANDIDATE"
    assert by_text["3.67%"]["selectable_as_target"] is True
    assert by_text["3.69%"]["source_role_hint"] == "COMPARISON_REFERENCE"
    assert by_text["3.69%"]["selectable_as_target"] is False
    assert by_text["3.67%"]["value_candidate_id"] in target_enum
    assert by_text["3.69%"]["value_candidate_id"] in target_enum


def test_candidate_skeleton_blocks_multiple_target_values_in_one_observation_skeleton():
    article = "사과는 21.6%, 쌀은 21.3% 올랐다."
    catalog = build_claim_skeleton_candidate_catalog(article)
    prediction = {"claims": [{
        "is_kosis_candidate": True,
        "claim_type": "물가",
        "indicator_norm": "사과 및 쌀 상승률",
        "context_sentence_candidate_ids": ["sentence:s0000"],
        "target_value_candidate_ids": [
            item["value_candidate_id"] for item in catalog["value_candidates"]
        ],
        "indicator_evidence_candidate_ids": [
            item["semantic_evidence_candidate_id"]
            for item in catalog["semantic_evidence_candidates"]
            if item["text"] in {"사과", "쌀"}
        ],
        "population_evidence_candidate_ids": [],
        "item_evidence_candidate_ids": [],
        "dimension_candidate_ids": [],
    }]}

    claim = normalize_claim_skeleton_candidate_selection(article, prediction)["claims"][0]
    _, report = validate_claim_skeleton(article, claim)

    assert report["status"] == "CONFLICT"
    assert "TARGET_VALUE_CANDIDATE_CARDINALITY" in report["errors"]


def test_derived_duration_and_quarter_range_are_selectable_value_candidates():
    article = (
        "제조업 취업자는 12개월 연속 감소했다. "
        "소비재 수출은 지난해 3분기~올해 2분기 감소한 뒤 반등했다."
    )
    catalog = build_claim_skeleton_candidate_catalog(article)
    by_text = {row["text"]: row for row in catalog["value_candidates"]}

    assert by_text["12개월 연속"]["unit"] == "개월 연속"
    assert by_text["12개월 연속"]["selectable_as_target"] is True
    assert by_text["지난해 3분기~올해 2분기"]["unit"] == "기간"
    assert by_text["지난해 3분기~올해 2분기"]["selectable_as_target"] is True


def test_claim_skeleton_call_chunks_large_candidate_catalog(monkeypatch):
    article = "수치는 " + ", ".join(f"{index}명" for index in range(1, 14)) + "이다."
    calls = []

    def fake_call(**kwargs):
        target_enum = kwargs["schema"]["properties"]["claims"]["items"][
            "properties"
        ]["target_value_candidate_ids"]["items"]["enum"]
        calls.append(target_enum)
        return {
            "claims": [{
                "is_kosis_candidate": True,
                "claim_type": "인원",
                "indicator_norm": "인원 수",
                "context_sentence_candidate_ids": ["sentence:s0000"],
                "target_value_candidate_ids": [target_id],
            } for target_id in target_enum],
        }, {"totalTokens": len(target_enum)}, 1.0

    monkeypatch.setattr(article_claim_pipeline, "_call_hcx_json", fake_call)
    prediction, usage, latency = article_claim_pipeline.call_hcx_claim_skeleton(
        "제목",
        article,
        api_key="test",
    )

    assert [len(chunk) for chunk in calls] == [12, 1]
    assert len(prediction["claims"]) == 13
    assert usage["totalTokens"] == 13
    assert latency == 2.0


def test_claim_skeleton_keeps_age_group_in_indicator_without_value_error():
    article = "60세 이상 취업자는 34만8000명 증가했다."
    target_id = next(
        row["span_id"] for row in build_span_candidates(article)
        if row.get("text") == "34만8000명"
    )
    skeleton = {
        "is_kosis_candidate": True,
        "claim_type": "연령별 취업자",
        "indicator_norm": "60세 이상 취업자 수 증가",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "target_value_span_ids": [target_id],
    }

    _, report = validate_claim_skeleton(article, skeleton)

    assert "INDICATOR_NORM_CONTAINS_VALUE" not in report["errors"]


def test_scope_blocks_category_threshold_and_local_non_kosis_sources():
    cases = [
        (
            "주당 근로시간이 36시간 미만인 단기 근로자 비율이 늘었다.",
            "36시간",
            "주당 근로시간",
            "VALUE_CATEGORY_DEFINITION",
        ),
        (
            "지난해 일주일에 36시간 미만으로 일한 근로자는 881만명이었다.",
            "36시간",
            "일주일 근로시간",
            "VALUE_CATEGORY_DEFINITION",
        ),
        (
            "어류·수산(20%) 등도 상승률이 20% 이상이었다.",
            "20%",
            "상승률",
            "VALUE_CATEGORY_DEFINITION",
        ),
        (
            "톱10 기업과 그 다음 90개 기업 간 수출 격차가 벌어졌다.",
            "90개",
            "수출 기업 수",
            "VALUE_CATEGORY_DEFINITION",
        ),
        (
            "한국경영자총협회의 조사에 따르면 신규 채용 계획 응답 기업은 60.8%였다.",
            "60.8%",
            "신규 채용 계획 비율",
            "LOCAL_NON_KOSIS_SOURCE_OUT_OF_SCOPE",
        ),
        (
            "지난해 노인 일자리 사업 중 63.5%는 공익형 일자리였다.",
            "63.5%",
            "공익형 일자리 비율",
            "LOCAL_NON_KOSIS_SOURCE_OUT_OF_SCOPE",
        ),
        (
            "국가데이터처가 추정한 올해 1~8월 자살 사망자 수는 9324명이었다.",
            "9324명",
            "자살 사망자 수",
            "LOCAL_NON_KOSIS_SOURCE_OUT_OF_SCOPE",
        ),
    ]
    for article, target_text, indicator, expected_error in cases:
        value_span = max(
            (
                row for row in build_span_candidates(article)
                if row.get("text") == target_text
            ),
            key=lambda row: row["char_start"],
        )
        semantic = {
            "indicator_norm": indicator,
            "measurement_type": "LEVEL",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
            "relation_json": {
                "dimension_pairing": "NOT_APPLICABLE",
                "pairing_evidence_sentence_ids": [],
            },
        }
        binding_validation = {
            "observations": [{
                "observation_index": 0,
                "status": "PASS",
                "measurement_type": "LEVEL",
                "value_span": value_span,
                "dimension_spans": [],
            }],
        }

        report = validate_claim_observation_scope(
            article,
            semantic,
            binding_validation,
        )

        assert report["claim_status"] == "BLOCKED", (expected_error, report)
        assert expected_error in report["observations"][0]["errors"]


def test_scope_blocks_private_source_in_immediately_preceding_context():
    article = (
        "기업 데이터 연구소 CEO스코어가 상위 기업을 조사한 결과를 발표했다. "
        "전체 임직원 중 20대 직원 비율은 지난해 21%였다."
    )
    value_span = next(
        row for row in build_span_candidates(article)
        if row.get("text") == "21%"
    )
    semantic = {
        "indicator_norm": "20대 직원 비율",
        "measurement_type": "LEVEL",
        "context_sentence_ids": [0, 1],
        "observation_sentence_ids": [1],
        "relation_json": {
            "dimension_pairing": "NOT_APPLICABLE",
            "pairing_evidence_sentence_ids": [],
        },
    }
    report = validate_claim_observation_scope(
        article,
        semantic,
        {
            "observations": [{
                "observation_index": 0,
                "status": "PASS",
                "measurement_type": "LEVEL",
                "value_span": value_span,
                "dimension_spans": [],
            }],
        },
    )

    assert report["claim_status"] == "BLOCKED"
    assert (
        "PRIVATE_SOURCE_CONTEXT_OUT_OF_SCOPE"
        in report["observations"][0]["errors"]
    )


def test_scope_blocks_future_policy_target_by_indicator_contract():
    article = "정부는 자살률을 2029년 19.4명으로 낮추겠다는 목표를 세웠다."
    value_span = next(
        row for row in build_span_candidates(article)
        if row.get("text") == "19.4명"
    )
    semantic = {
        "indicator_norm": "자살률 목표",
        "measurement_type": "LEVEL",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {
            "dimension_pairing": "NOT_APPLICABLE",
            "pairing_evidence_sentence_ids": [],
        },
    }
    report = validate_claim_observation_scope(
        article,
        semantic,
        {
            "observations": [{
                "observation_index": 0,
                "status": "PASS",
                "measurement_type": "LEVEL",
                "value_span": value_span,
                "dimension_spans": [],
            }],
        },
    )

    assert report["claim_status"] == "BLOCKED"
    assert "POLICY_TARGET_OUT_OF_SCOPE" in report["observations"][0]["errors"]


def test_scope_gate_keeps_indicator_anchor_across_a_temporal_definition_qualifier():
    article = "31일 자료에 따르면 저축은행 연체율(1개월 이상 연체 기준)은 11.7%로 집계됐다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "is_kosis_candidate": True,
        "claim_type": "연체율",
        "indicator_norm": "저축은행 연체율",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["11.7%"],
        "measurement_type": "LEVEL",
        "period_span_id": None,
        "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [0],
    }]}

    binding_report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
    )
    scope_report = validate_claim_observation_scope(article, claim, binding_report)

    assert binding_report["claim_status"] == "PASS"
    assert scope_report["claim_status"] == "PASS"


def test_scope_gate_keeps_main_indicator_across_a_causal_subgroup_value():
    article = "소매판매는 내구재(13.2%) 소비가 늘면서 전월 대비 1.5% 늘었다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "is_kosis_candidate": True,
        "claim_type": "소매판매",
        "indicator_norm": "소매판매 증가율",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["1.5%"],
        "measurement_type": "CHANGE_RATE",
        "period_span_id": by_text["전월"],
        "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [0],
    }]}

    binding_report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
    )
    scope_report = validate_claim_observation_scope(article, claim, binding_report)

    assert binding_report["claim_status"] == "PASS"
    assert scope_report["claim_status"] == "PASS"


def test_binding_uses_unit_rule_for_unambiguous_index_measurement_type():
    article = "전산업 생산지수는 111.7로 전월 대비 0.6% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "is_kosis_candidate": True,
        "claim_type": "생산지수",
        "indicator_norm": "전산업 생산지수",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["111.7"],
        "measurement_type": "LEVEL",
        "period_span_id": by_text["전월"],
        "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [0],
    }]}

    report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
    )

    assert report["claim_status"] == "PASS"
    assert report["observations"][0]["measurement_type_raw"] == "LEVEL"
    assert report["observations"][0]["measurement_type"] == "INDEX_LEVEL"
    assert report["observations"][0]["measurement_type_source"] == "UNIT_RULE"


def test_binding_recovers_one_unambiguous_relative_measurement_period():
    article = "지난달 소비자물가 상승률은 전년 동월 대비 2.4%였다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "indicator_norm": "소비자물가 상승률",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["2.4%"],
        "measurement_type": "CHANGE_RATE",
        "period_span_id": None,
        "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [0],
    }]}

    report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
    )

    assert report["claim_status"] == "PASS"
    assert report["observations"][0]["period_span"]["text"] == "지난달"
    assert report["observations"][0]["period_span_source"] == "UNAMBIGUOUS_RELATIVE_PERIOD_RULE"


def test_binding_uses_local_change_predicate_for_percent_change_rate():
    article = "제조업 생산이 0.8% 증가했고 서비스업 생산도 0.5% 늘었다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "indicator_norm": "제조업 생산",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["0.8%"],
        "measurement_type": "LEVEL",
        "period_span_id": None,
        "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [0],
    }]}

    report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
    )

    assert report["claim_status"] == "PASS"
    assert report["observations"][0]["measurement_type"] == "CHANGE_RATE"
    assert report["observations"][0]["measurement_type_source"] == "LOCAL_CHANGE_PREDICATE_RULE"


def test_binding_replaces_historical_comparison_with_unique_article_period():
    article = "작년 4분기 말 업권별 연체율을 집계했다. 보험사 연체율 1.46%는 2019년 2분기 이후 최고다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "indicator_norm": "보험사 연체율",
        "context_sentence_ids": [1],
        "observation_sentence_ids": [1],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["1.46%"],
        "measurement_type": "LEVEL",
        "period_span_id": by_text["2019년 2분기"],
        "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [1],
    }]}

    report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )

    assert report["claim_status"] == "PASS"
    assert report["observations"][0]["period_span"]["text"] == "작년 4분기 말"
    assert report["observations"][0]["period_span_source"] == "NEAREST_PRECEDING_RELATIVE_PERIOD_RULE"


def test_binding_filters_comparison_period_and_uses_nearest_preceding_measurement_period():
    article = "지난달 생산과 소비가 늘었다. 다만 건설 수주액은 전년 동월 대비 6.9% 감소했다."
    candidates = [*build_span_candidates(article), *build_semantic_evidence_candidates(article)]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "indicator_norm": "건설 수주액",
        "context_sentence_ids": [1],
        "observation_sentence_ids": [1],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["6.9%"],
        "measurement_type": "LEVEL",
        "period_span_id": by_text["전년 동월"],
        "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [1],
    }]}

    report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )

    assert report["claim_status"] == "PASS"
    assert report["observations"][0]["measurement_type"] == "CHANGE_RATE"
    assert report["observations"][0]["period_span"]["text"] == "지난달"
    assert report["observations"][0]["period_span_source"] == "NEAREST_PRECEDING_RELATIVE_PERIOD_RULE"


def test_binding_keeps_ratio_level_when_later_sentence_text_mentions_a_trend():
    article = "농가의 65세 이상 인구 비율은 지난해 55.8%로 해마다 상승했다."
    candidates = [
        *build_span_candidates(article),
        *build_semantic_evidence_candidates(article),
    ]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    report = validate_span_binding(
        {
            "indicator_norm": "농가 고령 인구 비율",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
        },
        {"observations": [{
            "value_span_id": by_text["55.8%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": by_text["지난해"],
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }]},
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )

    assert report["observations"][0]["measurement_type"] == "LEVEL"
    assert report["observations"][0]["measurement_type_source"] == "INDICATOR_RULE"


def test_binding_classifies_locally_governed_absolute_count_as_change_point():
    article = "작년 하반기에 매장 판매 직원이 1년 새 10만명 줄어들었다."
    candidates = [
        *build_span_candidates(article),
        *build_semantic_evidence_candidates(article),
    ]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    report = validate_span_binding(
        {
            "indicator_norm": "매장 판매 직원 수",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
        },
        {"observations": [{
            "value_span_id": by_text["10만명"],
            "measurement_type": "LEVEL",
            "period_span_id": by_text["작년"],
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }]},
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )

    observation = report["observations"][0]
    assert observation["measurement_type"] == "CHANGE_POINT"
    assert observation["period_normalized"] == "작년 하반기, 1년새"
    assert observation["effective_search_fields"] == {
        "indicator_norm": "매장 판매 직원 감소폭",
        "population_terms": ["직원"],
        "item_terms": [],
        "dimension_terms": ["매장 판매"],
        "source": "DETERMINISTIC_SOURCE_GROUNDED_R14B",
        "raw_indicator_norm": "매장 판매 직원 수",
    }


def test_binding_uses_target_specific_absolute_periods_and_baseline_pair():
    article = (
        "2024년 농가 수는 97만4000가구로 전년 대비 2.5% 감소했다. "
        "임가 인구는 20만4000명에서 20만명으로 줄었다."
    )
    candidates = [
        *build_span_candidates(article),
        *build_semantic_evidence_candidates(article),
    ]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    rate_report = validate_span_binding(
        {
            "indicator_norm": "농가 수 감소율",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
        },
        {"observations": [{
            "value_span_id": by_text["2.5%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": by_text["전년"],
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }]},
        [item for item in candidates if item["sentence_id"] == 0],
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )
    baseline_report = validate_span_binding(
        {
            "indicator_norm": "임가 인구",
            "context_sentence_ids": [1],
            "observation_sentence_ids": [1],
        },
        {"observations": [{
            "value_span_id": by_text["20만4000명"],
            "measurement_type": "LEVEL",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [1],
        }]},
        [item for item in candidates if item["sentence_id"] == 1],
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )

    assert rate_report["observations"][0]["period_normalized"] == "2024년"
    assert baseline_report["observations"][0]["period_normalized"] == "전년"


def test_scope_gate_blocks_policy_allocation_and_threshold_values():
    article = (
        "정부는 조선 분야에 숙련기능인력 비자 400명을 별도로 배정했다. "
        "외국 인력 도입 허용 비율도 20%에서 확대했다."
    )
    for sentence_id, target, indicator in (
        (0, "400명", "조선 분야 숙련기능인력 비자 배정 인원"),
        (1, "20%", "외국 인력 도입 허용 비율"),
    ):
        candidates = [
            *build_span_candidates(article),
            *build_semantic_evidence_candidates(article),
        ]
        by_text = {item["text"]: item["span_id"] for item in candidates}
        binding_report = validate_span_binding(
            {
                "indicator_norm": indicator,
                "measurement_type": "LEVEL",
                "context_sentence_ids": [sentence_id],
                "observation_sentence_ids": [sentence_id],
                "relation_json": {
                    "dimension_pairing": "NOT_APPLICABLE",
                    "pairing_evidence_sentence_ids": [],
                },
            },
            {"observations": [{
                "value_span_id": by_text[target],
                "measurement_type": "LEVEL",
                "period_span_id": None,
                "dimension_span_ids": [],
                "value_role": "TARGET_MEASURE",
                "indicator_value_relation": "SAME_METRIC",
                "relation_evidence_sentence_ids": [sentence_id],
            }]},
            [
                item
                for item in candidates
                if item["sentence_id"] == sentence_id
            ],
            require_value_relation=True,
            require_measurement_type=True,
        )
        scope_report = validate_claim_observation_scope(
            article,
            {
                "indicator_norm": indicator,
                "measurement_type": "LEVEL",
                "context_sentence_ids": [sentence_id],
                "observation_sentence_ids": [sentence_id],
                "relation_json": {
                    "dimension_pairing": "NOT_APPLICABLE",
                    "pairing_evidence_sentence_ids": [],
                },
            },
            binding_report,
        )

        assert scope_report["claim_status"] == "BLOCKED"
        assert (
            "POLICY_OR_RULE_VALUE_OUT_OF_SCOPE"
            in scope_report["observations"][0]["errors"]
        )


def test_scope_gate_accepts_a_later_value_with_an_explicit_comparison_bridge():
    article = "2024년 농가 수는 97만4000가구로 전년 대비 2.5% 감소했다."
    candidates = [
        *build_span_candidates(article),
        *build_semantic_evidence_candidates(article),
    ]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "indicator_norm": "농가 수 변화율",
        "measurement_type": "CHANGE_RATE",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {
            "dimension_pairing": "NOT_APPLICABLE",
            "pairing_evidence_sentence_ids": [],
        },
    }
    binding_report = validate_span_binding(
        claim,
        {"observations": [{
            "value_span_id": by_text["2.5%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": by_text["전년"],
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [0],
        }]},
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )
    scope_report = validate_claim_observation_scope(
        article,
        claim,
        binding_report,
    )

    assert scope_report["claim_status"] == "PASS"
    assert (
        scope_report["observations"][0]["structured_comparison_linked"]
        is True
    )
    assert (
        binding_report["observations"][0]["effective_search_fields"][
            "indicator_norm"
        ]
        == "농가 수 전년비 감소율"
    )


def test_binding_inherits_a_unique_period_from_the_same_survey_context():
    article = (
        "산업기술인력 수급 실태조사에 따르면 2023년 말 전체 인력은 집계됐다. "
        "한편 이번 조사에서 부족 인원은 1.9% 늘었다."
    )
    candidates = [
        *build_span_candidates(article),
        *build_semantic_evidence_candidates(article),
    ]
    by_text = {item["text"]: item["span_id"] for item in candidates}
    report = validate_span_binding(
        {
            "indicator_norm": "부족 인원 증가율",
            "context_sentence_ids": [1],
            "observation_sentence_ids": [1],
        },
        {"observations": [{
            "value_span_id": by_text["1.9%"],
            "measurement_type": "CHANGE_RATE",
            "period_span_id": None,
            "dimension_span_ids": [],
            "value_role": "TARGET_MEASURE",
            "indicator_value_relation": "SAME_METRIC",
            "relation_evidence_sentence_ids": [1],
        }]},
        [item for item in candidates if item["sentence_id"] == 1],
        require_value_relation=True,
        require_measurement_type=True,
        article_text=article,
    )

    assert report["observations"][0]["period_normalized"] == "2023년 말"
    assert (
        report["observations"][0]["period_span_source"]
        == "ARTICLE_SURVEY_PERIOD_RULE"
    )


def test_scope_gate_accepts_one_locally_paired_region_for_a_single_target():
    article = "실질 지역내총생산 성장률은 경북(1.6%), 울산(1.4%), 서울(1.0%) 순이었다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "indicator_norm": "실질 지역내총생산 성장률",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["1.4%"],
        "measurement_type": "CHANGE_RATE",
        "period_span_id": None,
        "dimension_span_ids": [by_text["울산"]],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [0],
    }]}

    binding_report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
    )
    scope_report = validate_claim_observation_scope(article, claim, binding_report)

    assert binding_report["claim_status"] == "PASS"
    assert scope_report["claim_status"] == "PASS"
    assert scope_report["observations"][0]["local_dimension_paired"] is True


def test_binding_corrects_multiple_region_values_to_the_nearest_local_pair():
    article = "실질 지역내총생산 성장률은 경북(1.6%), 울산(1.4%), 서울(1.0%) 순이었다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {
        "indicator_norm": "실질 지역내총생산 성장률",
        "context_sentence_ids": [0],
        "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["1.4%"],
        "measurement_type": "CHANGE_RATE",
        "period_span_id": None,
        "dimension_span_ids": [by_text["경북"], by_text["울산"]],
        "value_role": "TARGET_MEASURE",
        "indicator_value_relation": "SAME_METRIC",
        "relation_evidence_sentence_ids": [0],
    }]}

    binding_report = validate_span_binding(
        claim,
        binding,
        candidates,
        require_value_relation=True,
        require_measurement_type=True,
    )
    scope_report = validate_claim_observation_scope(
        article,
        claim,
        binding_report,
    )

    assert binding_report["observations"][0]["dimension_span_source"] == "LOCAL_PAIR_RULE"
    assert [item["text"] for item in binding_report["observations"][0]["dimension_spans"]] == ["울산"]
    assert scope_report["claim_status"] == "PASS"


def test_span_candidates_reject_lexical_lookalikes_and_time_units():
    article = "서울 종로구 청운동은 따르면, 보다도 기여도와 수도를 언급했다. 경기를 경유하면 물가가 오르면 경제협력개발기구 기준이 된다. 개인사업자의 금융·보험과 제조업은 2024년 2분기 3.2% 증가했다."
    candidates = build_span_candidates(article)
    dimensions = {(item["dimension_type"], item["text"]) for item in candidates if item["kind"] == "dimension"}
    values = {item["text"] for item in candidates if item["kind"] == "value_unit"}

    assert {("지역", "서울"), ("지역", "종로구"), ("지역", "청운동")} <= dimensions
    assert {("산업", "금융·보험"), ("산업", "제조업")} <= dimensions
    assert not dimensions & {("지역", value) for value in ("따르면", "보다도", "기여도", "수도", "경기", "오르면", "경제협력개발기구")}
    assert ("산업", "개인사업") not in dimensions
    assert "2분기" not in values
    assert "3.2%" in values


def test_constrained_binding_passes_only_candidate_ids_in_selected_sentences():
    candidates = build_span_candidates(ARTICLE)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    semantic_claim = {
        "is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "고용률",
        "context_sentence_ids": [0], "observation_sentence_ids": [0, 1], "relation_json": {},
    }
    binding = {"observations": [{"value_span_id": by_text["45.1%"], "period_span_id": None, "dimension_span_ids": [by_text["서울"]]}]}

    report = validate_span_binding(semantic_claim, binding, candidates)

    assert report["observations"][0]["status"] == "PASS"
    assert report["observations"][0]["value_span"]["text"] == "45.1%"
    assert len(pass_span_bound_observations(semantic_claim, binding, report)) == 1


def test_skeleton_binding_assigns_measurement_type_after_selecting_source_value():
    article = "전산업 생산지수는 111.7로 전월 대비 0.6% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    skeleton = {
        "is_kosis_candidate": True, "claim_type": "생산", "indicator_norm": "전산업 생산지수",
        "context_sentence_ids": [0], "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }
    binding = {"observations": [{
        "value_span_id": by_text["0.6%"], "measurement_type": "CHANGE_RATE", "period_span_id": by_text["전월"], "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC", "relation_evidence_sentence_ids": [0],
    }]}

    report = validate_span_binding(skeleton, binding, candidates, require_value_relation=True, require_measurement_type=True)

    assert report["claim_status"] == "PASS"
    assert report["observations"][0]["measurement_type"] == "CHANGE_RATE"


def test_constrained_binding_blocks_unknown_id_and_wrong_sentence():
    candidates = build_span_candidates(ARTICLE)
    semantic_claim = {
        "is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "고용률",
        "context_sentence_ids": [0], "observation_sentence_ids": [0], "relation_json": {},
    }
    binding = {"observations": [{"value_span_id": "s1:value_unit:999", "period_span_id": None, "dimension_span_ids": []}]}

    report = validate_span_binding(semantic_claim, binding, candidates)

    assert report["observations"][0]["status"] == "CONFLICT"
    assert "VALUE_SPAN_ID_UNKNOWN" in report["observations"][0]["errors"]


def test_semantic_and_binding_prompts_forbid_free_text_fields():
    candidates = build_span_candidates(ARTICLE)
    semantic_prompt = build_semantic_article_prompt("고용 기사", ARTICLE)
    binding_prompt = build_span_binding_prompt({"indicator_norm": "고용률", "context_sentence_ids": [0], "observation_sentence_ids": [0], "relation_json": {"dimension_pairing": "NOT_APPLICABLE"}}, candidates)

    assert "constraint 배열" in semantic_prompt
    assert "연속 원문 substring" in semantic_prompt
    assert "빈 배열" in semantic_prompt
    assert "하위 업종" in semantic_prompt
    assert "UNPAIRED_MULTI_VALUE" in semantic_prompt
    assert '"span_id"' in binding_prompt


def test_semantic_schema_requires_non_empty_indicator_for_retrieval_contract():
    schema = __import__("src.develop.article_claim_pipeline", fromlist=["SEMANTIC_ARTICLE_SCHEMA"]).SEMANTIC_ARTICLE_SCHEMA["properties"]["claims"]["items"]
    indicator_schema = schema["properties"]["indicator_norm"]

    assert indicator_schema == {"type": "string", "minLength": 1}
    assert schema["properties"]["measurement_type"]["enum"] == ["INDEX_LEVEL", "LEVEL", "CHANGE_RATE", "CHANGE_POINT"]
    assert schema["properties"]["relation_json"]["required"] == ["dimension_pairing", "pairing_evidence_sentence_ids"]
    assert {"population_constraints", "item_constraints", "period_constraints", "comparison_constraints"} <= set(schema["required"])


def test_strict_semantic_constraints_require_exact_source_substrings():
    article = "개인사업자대출 저축은행 연체율은 작년 4분기 말 11.7%였다."
    claim = {
        "is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "저축은행 연체율",
        "measurement_type": "LEVEL",
        "population_constraints": ["개인사업자대출"], "item_constraints": ["저축은행"],
        "period_constraints": ["작년 4분기 말"], "comparison_constraints": [],
        "context_sentence_ids": [0], "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }

    report = validate_semantic_claim(article, claim, require_constraints=True)

    assert report["status"] == "PASS"
    assert report["constraint_contract_status"] == "ASSERTED"
    assert report["constraint_spans"]["population_constraints"][0]["text"] == "개인사업자대출"


def test_strict_semantic_constraints_block_missing_or_ungrounded_fields():
    claim = {
        "is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "고용률",
        "measurement_type": "LEVEL",
        "population_constraints": ["존재하지 않는 모집단"], "item_constraints": [], "period_constraints": [],
        "context_sentence_ids": [0], "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }

    report = validate_semantic_claim(ARTICLE, claim, require_constraints=True)

    assert report["status"] == "MISSING"
    assert "COMPARISON_CONSTRAINTS_MISSING" in report["errors"]
    assert "POPULATION_CONSTRAINTS_NOT_IN_CLAIM_SENTENCES" in report["errors"]


def test_strict_semantic_validation_requires_measurement_type():
    claim = {
        "is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "고용률",
        "population_constraints": [], "item_constraints": [], "period_constraints": [], "comparison_constraints": [],
        "context_sentence_ids": [0], "observation_sentence_ids": [0],
        "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []},
    }

    report = validate_semantic_claim(ARTICLE, claim, require_constraints=True)

    assert report["status"] == "MISSING"
    assert "MEASUREMENT_TYPE_MISSING" in report["errors"]


def test_semantic_validation_blocks_missing_indicator_before_binding():
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": None,
             "context_sentence_ids": [0], "observation_sentence_ids": [0], "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}

    report = validate_semantic_claim(ARTICLE, claim)

    assert report["status"] == "MISSING"
    assert report["errors"] == ["INDICATOR_NORM_MISSING"]


def test_semantic_validation_blocks_missing_and_unknown_sentence_ids_before_binding():
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "고용률",
             "context_sentence_ids": [], "observation_sentence_ids": [99], "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}

    report = validate_semantic_claim(ARTICLE, claim)

    assert report["status"] == "CONFLICT"
    assert "CONTEXT_SENTENCE_MISSING" in report["errors"]
    assert "OBSERVATION_SENTENCE_IDS_ID_UNKNOWN" in report["errors"]


def test_semantic_validation_blocks_missing_dimension_pairing_before_binding():
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "고용률",
             "context_sentence_ids": [0], "observation_sentence_ids": [0], "relation_json": {"pairing_evidence_sentence_ids": []}}

    report = validate_semantic_claim(ARTICLE, claim)

    assert report["status"] == "MISSING"
    assert report["errors"] == ["DIMENSION_PAIRING_MISSING"]


def test_semantic_validation_requires_pairing_evidence_only_when_pairing_applies():
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "지역내총생산 증가율",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "EXPLICIT_PAIRING", "pairing_evidence_sentence_ids": []}}

    report = validate_semantic_claim(ARTICLE, claim)

    assert report["status"] == "MISSING"
    assert report["errors"] == ["PAIRING_EVIDENCE_SENTENCE_MISSING"]


def test_semantic_validation_rejects_pairing_evidence_for_not_applicable():
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "고용률",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": [0]}}

    report = validate_semantic_claim(ARTICLE, claim)

    assert report["status"] == "MISSING"
    assert report["errors"] == ["NOT_APPLICABLE_PAIRING_EVIDENCE_PRESENT"]


def test_constrained_binding_allows_period_from_claim_context_sentence():
    article = "올해 1분기 지역내총생산이 증가했다. 경북은 1.6% 늘었다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "지역내총생산 증가율",
             "context_sentence_ids": [0], "observation_sentence_ids": [1],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{"value_span_id": by_text["1.6%"], "period_span_id": by_text["올해 1분기"], "dimension_span_ids": [by_text["경북"]]}]}

    report = validate_span_binding(claim, binding, candidates)

    assert report["observations"][0]["status"] == "PASS"


def test_scope_gate_blocks_other_indicator_values_inside_a_source_valid_binding():
    article = "전산업 생산지수는 0.6% 증가했다. 제조업은 9.1% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "전산업 생산지수",
             "context_sentence_ids": [0], "observation_sentence_ids": [0, 1],
             "relation_json": {"dimension_pairing": "EXPLICIT_PAIRING", "pairing_evidence_sentence_ids": [0]}}
    binding = {"observations": [
        {"value_span_id": by_text["0.6%"], "period_span_id": None, "dimension_span_ids": []},
        {"value_span_id": by_text["9.1%"], "period_span_id": None, "dimension_span_ids": [by_text["제조업"]]},
    ]}

    binding_report = validate_span_binding(claim, binding, candidates)
    scope_report = validate_claim_observation_scope(article, claim, binding_report)

    assert all(item["status"] == "PASS" for item in binding_report["observations"])
    assert scope_report["claim_status"] == "BLOCKED"
    assert "VALUE_OUTSIDE_INDICATOR_SCOPE" in scope_report["observations"][1]["errors"]
    assert pass_scope_bound_observations(claim, binding, binding_report, scope_report) == []


def test_scope_gate_requires_cardinality_before_accepting_explicit_pairing():
    article = "서비스업 생산은 0.9% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "서비스업 생산 증가",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "EXPLICIT_PAIRING", "pairing_evidence_sentence_ids": [0]}}
    binding = {"observations": [{"value_span_id": by_text["0.9%"], "period_span_id": None, "dimension_span_ids": [by_text["서비스업"]]}]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates))

    assert scope_report["claim_status"] == "BLOCKED"
    assert scope_report["errors"] == ["EXPLICIT_PAIRING_CARDINALITY_INSUFFICIENT"]


def test_scope_gate_allows_auditable_explicit_region_value_pairs():
    article = "실질 지역내총생산 증가율을 발표했다. 경북은 1.6%, 울산은 1.4% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "실질 지역내총생산 증가율",
             "context_sentence_ids": [0], "observation_sentence_ids": [1],
             "relation_json": {"dimension_pairing": "EXPLICIT_PAIRING", "pairing_evidence_sentence_ids": [1]}}
    binding = {"observations": [
        {"value_span_id": by_text["1.6%"], "period_span_id": None, "dimension_span_ids": [by_text["경북"]]},
        {"value_span_id": by_text["1.4%"], "period_span_id": None, "dimension_span_ids": [by_text["울산"]]},
    ]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates))

    assert scope_report["claim_status"] == "PASS"


def test_scope_gate_does_not_treat_generic_production_word_as_indicator_evidence():
    article = "광업과 제조업의 생산 증가를 발표했다. 경북은 1.6%, 울산은 1.4% 지역내총생산 증가를 기록했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "광업과 제조업의 생산 증가",
             "context_sentence_ids": [0], "observation_sentence_ids": [1],
             "relation_json": {"dimension_pairing": "EXPLICIT_PAIRING", "pairing_evidence_sentence_ids": [0]}}
    binding = {"observations": [
        {"value_span_id": by_text["1.6%"], "period_span_id": None, "dimension_span_ids": [by_text["경북"]]},
        {"value_span_id": by_text["1.4%"], "period_span_id": None, "dimension_span_ids": [by_text["울산"]]},
    ]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates))

    assert scope_report["claim_status"] == "BLOCKED"
    assert all("VALUE_OUTSIDE_INDICATOR_SCOPE" in item["errors"] for item in scope_report["observations"])


def test_scope_gate_allows_one_dimensionless_value_linked_to_the_immediately_prior_indicator_context():
    article = "국내 계란 가격이 계속 오르고 있다. 특란 평균 소매 가격은 작년 말보다 11.7% 올랐다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "국내 계란 가격 변화율",
             "context_sentence_ids": [0], "observation_sentence_ids": [1],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{"value_span_id": by_text["11.7%"], "period_span_id": by_text["작년 말"], "dimension_span_ids": []}]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates))

    assert scope_report["claim_status"] == "PASS"
    assert scope_report["observations"][0]["context_linked"] is True


def test_scope_gate_marks_a_direct_indicator_value_as_not_context_linked():
    article = "서비스 기여도가 가장 컸다. 서비스 가격은 2.5% 상승했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "서비스 가격 상승률",
             "context_sentence_ids": [0], "observation_sentence_ids": [1],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{"value_span_id": by_text["2.5%"], "period_span_id": None, "dimension_span_ids": []}]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates))

    assert scope_report["claim_status"] == "PASS"
    assert scope_report["observations"][0]["context_linked"] is False


def test_scope_gate_blocks_historical_comparison_value_even_when_hcx_declares_target():
    article = "저축은행 연체율은 11.7%였다. 이는 2015년 2분기(11.87%) 이후 가장 높다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "저축은행 연체율",
             "context_sentence_ids": [0], "observation_sentence_ids": [0, 1],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{
        "value_span_id": by_text["11.87%"], "period_span_id": None, "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC", "relation_evidence_sentence_ids": [1],
    }]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates, require_value_relation=True))

    assert scope_report["claim_status"] == "BLOCKED"
    assert scope_report["observations"][0]["source_value_role"] == "COMPARISON_REFERENCE"
    assert "VALUE_COMPARISON_REFERENCE" in scope_report["observations"][0]["errors"]


def test_scope_gate_blocks_item_list_value_even_when_hcx_declares_target():
    article = "서비스 가격은 지난달 2.5% 상승했는데, 품목별로는 보험서비스료(16.3%)가 크게 올랐다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "서비스 가격 상승률",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{
        "value_span_id": by_text["16.3%"], "period_span_id": None, "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC", "relation_evidence_sentence_ids": [0],
    }]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates, require_value_relation=True))

    assert scope_report["claim_status"] == "BLOCKED"
    assert scope_report["observations"][0]["source_value_role"] == "SUBGROUP_MEASURE"
    assert "VALUE_SUBGROUP_MEASURE" in scope_report["observations"][0]["errors"]


def test_scope_gate_does_not_context_link_a_dimension_bearing_next_sentence():
    article = "소매판매가 증가했다. 제조업은 9.1% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "aggregate_statistic", "indicator_norm": "소매판매 증가율",
             "context_sentence_ids": [0], "observation_sentence_ids": [1],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{"value_span_id": by_text["9.1%"], "period_span_id": None, "dimension_span_ids": [by_text["제조업"]]}]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates))

    assert scope_report["claim_status"] == "BLOCKED"
    assert scope_report["observations"][0]["context_linked"] is False


def test_hierarchy_contract_blocks_a_subgroup_value_for_a_parent_indicator():
    article = "전자부품(9.1%)과 전기장비(6%) 생산 증가에 힘입어 서비스업 생산도 전월 대비 0.5% 늘었다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "서비스업 생산", "indicator_norm": "서비스업 생산 증가율",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    subgroup_binding = {"observations": [{
        "value_span_id": by_text["9.1%"], "period_span_id": None, "dimension_span_ids": [],
        "value_role": "SUBGROUP_MEASURE", "indicator_value_relation": "SUBGROUP_OF", "relation_evidence_sentence_ids": [0],
    }]}

    subgroup_report = validate_span_binding(claim, subgroup_binding, candidates, require_value_relation=True)
    subgroup_scope = validate_claim_observation_scope(article, claim, subgroup_report)

    assert subgroup_report["observations"][0]["status"] == "CONFLICT"
    assert "VALUE_ROLE_NOT_TARGET_MEASURE" in subgroup_report["observations"][0]["errors"]
    assert "INDICATOR_VALUE_RELATION_NOT_SAME_METRIC" in subgroup_report["observations"][0]["errors"]
    assert subgroup_scope["claim_status"] == "BLOCKED"

    target_binding = {"observations": [{
        "value_span_id": by_text["0.5%"], "period_span_id": by_text["전월"], "dimension_span_ids": [],
        "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC", "relation_evidence_sentence_ids": [0],
    }]}
    target_report = validate_span_binding(claim, target_binding, candidates, require_value_relation=True)
    target_scope = validate_claim_observation_scope(article, claim, target_report)

    assert target_report["observations"][0]["status"] == "PASS"
    assert target_scope["claim_status"] == "PASS"


def test_scope_gate_blocks_later_parent_value_for_a_leading_contributor_indicator():
    article = "전자부품(9.1%)과 전기장비(6%) 생산 증가에 힘입어 서비스업 생산도 전월 대비 0.5% 늘었다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "전자부품 생산", "indicator_norm": "전자부품 및 전기장비 생산",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{"value_span_id": by_text["0.5%"], "period_span_id": by_text["전월"], "dimension_span_ids": [],
                                 "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC",
                                 "relation_evidence_sentence_ids": [0]}]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates, require_value_relation=True))

    assert scope_report["claim_status"] == "BLOCKED"
    assert "VALUE_OUTSIDE_INDICATOR_SCOPE" in scope_report["observations"][0]["errors"]


def test_scope_gate_blocks_adjacent_other_item_value_in_an_item_value_enumeration():
    article = "전년 동월과 비교해 지난달 사과는 21.6%, 쌀은 21.3% 올랐다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "사과 가격", "indicator_norm": "사과 상승률",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [
        {"value_span_id": by_text["21.6%"], "period_span_id": by_text["지난달"], "dimension_span_ids": [],
         "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC", "relation_evidence_sentence_ids": [0]},
        {"value_span_id": by_text["21.3%"], "period_span_id": by_text["지난달"], "dimension_span_ids": [],
         "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC", "relation_evidence_sentence_ids": [0]},
    ]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates, require_value_relation=True))

    assert scope_report["claim_status"] == "BLOCKED"
    assert scope_report["observations"][0]["status"] == "PASS"
    assert "VALUE_OUTSIDE_INDICATOR_SCOPE" in scope_report["observations"][1]["errors"]


def test_scope_gate_allows_a_single_character_item_anchor():
    article = "지난달 쌀은 21.3% 올랐다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "쌀 가격", "indicator_norm": "쌀 상승률",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{"value_span_id": by_text["21.3%"], "period_span_id": by_text["지난달"], "dimension_span_ids": [],
                                 "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC",
                                 "relation_evidence_sentence_ids": [0]}]}

    scope_report = validate_claim_observation_scope(article, claim, validate_span_binding(claim, binding, candidates, require_value_relation=True))

    assert scope_report["claim_status"] == "PASS"


def test_span_candidates_preserve_bare_index_level_in_a_narrow_source_pattern():
    candidates = build_span_candidates("전산업 생산지수는 111.7로 전월 대비 0.6% 증가했다.")
    index_level = next(item for item in candidates if item["text"] == "111.7")

    assert index_level["kind"] == "value_unit"
    assert index_level["unit"] == "지수"


def test_measurement_type_filters_binding_value_candidates_before_hcx():
    article = "전산업 생산지수는 111.7로 전월 대비 0.6% 증가했다."
    candidates = build_span_candidates(article)
    level_candidates, level_audit = filter_span_candidates_for_measurement_type({"measurement_type": "INDEX_LEVEL"}, candidates)
    rate_candidates, rate_audit = filter_span_candidates_for_measurement_type({"measurement_type": "CHANGE_RATE"}, candidates)

    assert {item["text"] for item in level_candidates if item["kind"] == "value_unit"} == {"111.7"}
    assert {item["text"] for item in rate_candidates if item["kind"] == "value_unit"} == {"0.6%"}
    assert level_audit["binding_value_candidate_count"] == 1
    assert rate_audit["binding_value_candidate_count"] == 1


def test_relative_period_is_inherited_only_when_article_context_is_unambiguous():
    article = "지난달 소비자물가가 올랐다. 사과는 21.6% 올랐다."
    claim = {
        "measurement_type": "CHANGE_RATE", "period_constraints": [],
        "context_sentence_ids": [1], "observation_sentence_ids": [1],
    }

    effective, audit = apply_article_relative_period_context(article, claim)

    assert effective["period_constraints"] == ["지난달"]
    assert effective["context_sentence_ids"] == [1, 0]
    assert audit["reason"] == "UNAMBIGUOUS_ARTICLE_RELATIVE_PERIOD"


def test_relative_period_is_not_inherited_when_article_has_multiple_relative_periods():
    article = "지난달 소비자물가가 올랐다. 지난해 같은 달보다 사과는 21.6% 올랐다."
    claim = {"measurement_type": "CHANGE_RATE", "period_constraints": [], "context_sentence_ids": [1], "observation_sentence_ids": [1]}

    effective, audit = apply_article_relative_period_context(article, claim)

    assert effective["period_constraints"] == []
    assert audit["reason"] == "NONE"


def test_explicit_relative_period_in_own_claim_sentence_does_not_expand_context():
    article = "지난달 소비자물가가 올랐다. 지난달 사과는 21.6% 올랐다."
    claim = {"measurement_type": "CHANGE_RATE", "period_constraints": ["지난달"], "context_sentence_ids": [1], "observation_sentence_ids": [1]}

    effective, audit = apply_article_relative_period_context(article, claim)

    assert effective["context_sentence_ids"] == [1]
    assert audit["reason"] == "ALREADY_GROUNDED_IN_CLAIM_SENTENCES"


def test_scope_gate_rejects_percent_value_for_an_index_level_claim():
    article = "전산업 생산지수는 111.7로 전월 대비 0.6% 증가했다."
    candidates = build_span_candidates(article)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "생산지수 수준", "indicator_norm": "전산업 생산지수 수준",
             "measurement_type": "INDEX_LEVEL",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    binding = {"observations": [{"value_span_id": by_text["0.6%"], "period_span_id": by_text["전월"], "dimension_span_ids": [],
                                 "value_role": "TARGET_MEASURE", "indicator_value_relation": "SAME_METRIC",
                                 "relation_evidence_sentence_ids": [0]}]}

    binding_report = validate_span_binding(claim, binding, candidates, require_value_relation=True)
    scope_report = validate_claim_observation_scope(article, claim, binding_report)

    assert scope_report["claim_status"] == "BLOCKED"
    assert "VALUE_UNIT_INCOMPATIBLE_WITH_INDEX_LEVEL" in binding_report["observations"][0]["errors"]


def test_strict_hierarchy_contract_blocks_legacy_binding_without_role_and_relation():
    candidates = build_span_candidates(ARTICLE)
    by_text = {item["text"]: item["span_id"] for item in candidates}
    claim = {"is_kosis_candidate": True, "claim_type": "고용률", "indicator_norm": "고용률",
             "context_sentence_ids": [0], "observation_sentence_ids": [0],
             "relation_json": {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}}
    legacy_binding = {"observations": [{"value_span_id": by_text["45.1%"], "period_span_id": None, "dimension_span_ids": []}]}

    report = validate_span_binding(claim, legacy_binding, candidates, require_value_relation=True)

    assert report["observations"][0]["status"] == "CONFLICT"
    assert {"VALUE_ROLE_MISSING", "INDICATOR_VALUE_RELATION_MISSING", "RELATION_EVIDENCE_SENTENCE_IDS_MISSING"} <= set(report["observations"][0]["errors"])
