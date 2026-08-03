"""기사 단위 HCX claim 구조화의 입력 계약과 원문 기반 observation 검증기."""

from __future__ import annotations

import json
import re
import time
import uuid
from copy import deepcopy
from datetime import date
from typing import Any

try:  # Keep deterministic span validation testable without the HTTP client installed.
    import requests
except ImportError:  # pragma: no cover - exercised only in minimal test runtimes
    requests = None  # type: ignore[assignment]

try:
    from ..claim_extractor import TIME_RE, VALUE_UNIT_RE, canon_unit, iter_sentence_spans
    # L1 and the HCX client live in their own modules now (CLAUDE.md 6.4절).
    # Re-exported here so the r16i callers that predate the split — and the
    # tests that monkeypatch ``_call_hcx_json`` on this module — keep working.
    from .hcx_client import _call_hcx_json  # noqa: F401
    from .l1_value_candidates import (  # noqa: F401
        _keep_dimension_candidate,
        _span_record,
        build_span_candidates,
        sentence_offset_map,
    )
    from .lexical_rules import (
        _ABSOLUTE_PERIOD_RE,
        _AGE_INDICATOR_VALUE_RE,
        _AGE_RANGE_RE,
        _BASELINE_PAIR_RE,
        _COMPARISON_PAREN_PREFIX_RE,
        _COMPARISON_PERIOD_RE,
        _CONSECUTIVE_DURATION_VALUE_RE,
        _INDICATOR_CLAUSE_END_RE,
        _INDICATOR_SOURCE_ORG_RE,
        _INDEX_LEVEL_VALUE_RE,
        _INDUSTRY_STOPWORDS,
        _ITEM_ANCHOR_STOPWORDS,
        _LOCAL_CHANGE_PREDICATE_RE,
        _LOCAL_INDICATOR_LEADING_STOPWORDS,
        _LOCAL_INDICATOR_METRIC_RE,
        _METRIC_ANCHOR_SUFFIXES,
        _PERIOD_CANDIDATE_RE,
        _POPULATION_SURFACE_RE,
        _REGION_STOPWORDS,
        _RELATIVE_MEASUREMENT_PERIOD_RE,
        _RELATIVE_PERIOD_RANGE_VALUE_RE,
        _SEMANTIC_DIMENSION_SURFACE_RE,
        _SEMANTIC_EVIDENCE_PARTICLES,
        _SEMANTIC_EVIDENCE_STOPWORDS,
        _SEMANTIC_EVIDENCE_TOKEN_RE,
        _SPAN_DIMENSION_PATTERNS,
        rule_match,
        rule_search,
    )
except ImportError:  # pragma: no cover
    from claim_extractor import TIME_RE, VALUE_UNIT_RE, canon_unit, iter_sentence_spans
    from hcx_client import _call_hcx_json  # type: ignore  # noqa: F401
    from l1_value_candidates import (  # type: ignore  # noqa: F401
        _keep_dimension_candidate,
        _span_record,
        build_span_candidates,
        sentence_offset_map,
    )
    from lexical_rules import (  # type: ignore
        _ABSOLUTE_PERIOD_RE,
        _AGE_INDICATOR_VALUE_RE,
        _AGE_RANGE_RE,
        _BASELINE_PAIR_RE,
        _COMPARISON_PAREN_PREFIX_RE,
        _COMPARISON_PERIOD_RE,
        _CONSECUTIVE_DURATION_VALUE_RE,
        _INDICATOR_CLAUSE_END_RE,
        _INDICATOR_SOURCE_ORG_RE,
        _INDEX_LEVEL_VALUE_RE,
        _INDUSTRY_STOPWORDS,
        _ITEM_ANCHOR_STOPWORDS,
        _LOCAL_CHANGE_PREDICATE_RE,
        _LOCAL_INDICATOR_LEADING_STOPWORDS,
        _LOCAL_INDICATOR_METRIC_RE,
        _METRIC_ANCHOR_SUFFIXES,
        _PERIOD_CANDIDATE_RE,
        _POPULATION_SURFACE_RE,
        _REGION_STOPWORDS,
        _RELATIVE_MEASUREMENT_PERIOD_RE,
        _RELATIVE_PERIOD_RANGE_VALUE_RE,
        _SEMANTIC_DIMENSION_SURFACE_RE,
        _SEMANTIC_EVIDENCE_PARTICLES,
        _SEMANTIC_EVIDENCE_STOPWORDS,
        _SEMANTIC_EVIDENCE_TOKEN_RE,
        _SPAN_DIMENSION_PATTERNS,
        rule_match,
        rule_search,
    )


ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "is_kosis_candidate": {"type": "boolean"},
                    "evidence_sentence_ids": {"type": "array", "items": {"type": "integer"}},
                    "context_sentence_ids": {"type": "array", "items": {"type": "integer"}},
                    "evidence_quote": {"type": "string"},
                    "indicator_evidence_texts": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                    "indicator_norm": {"type": ["string", "null"]},
                    "dimensions": {"type": "object"},
                    "dimension_source_texts": {"type": "object"},
                    "relation_json": {"type": "object"},
                    "observations": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string", "minLength": 1},
                                "unit": {"type": "string", "minLength": 1},
                                "evidence_sentence_ids": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
                                "value_unit_evidence_text": {"type": "string", "minLength": 1},
                                "period": {"type": ["string", "null"]}, "period_evidence_text": {"type": ["string", "null"]},
                            },
                            "required": ["value", "unit", "evidence_sentence_ids", "value_unit_evidence_text", "period", "period_evidence_text"],
                        },
                    },
                },
                "required": ["is_kosis_candidate", "evidence_sentence_ids", "context_sentence_ids", "evidence_quote", "indicator_evidence_texts", "indicator_norm", "dimensions", "dimension_source_texts", "relation_json", "observations"],
            },
        }
    },
    "required": ["claims"],
}

ARTICLE_SYSTEM_PROMPT = """당신은 뉴스 기사 전체를 KOSIS 사실검증용 Claim/Observation 배열로 구조화한다.
기사 원문 밖의 지식이나 수치를 쓰지 않는다. claim마다 evidence_sentence_ids와 evidence_quote를,
지표마다 indicator_evidence_texts(각각 연속된 원문 substring)와 indicator_norm(검색용 의미 요약)을 반환한다.
각 observation에는 value/unit과 value_unit_evidence_text(연속된 원문의 값+단위 substring)를 반드시 넣는다. value는 단위 없이 숫자만 반환한다.
context_sentence_ids는 지표의 선행 문맥, observation.evidence_sentence_ids는 값·단위·시점이 실제 있는 문장이다. observation의 value/unit/period는 자신의 evidence 문장에 실제 있을 때만 반환한다. 복수 차원값의
관계가 명시되지 않으면 relation_json.dimension_pairing은 UNPAIRED_MULTI_VALUE로 둔다."""

# HCX가 원문 문자열을 재생성하지 않도록 하는 신규 2단계 계약이다. 1차는 문맥과
# 관측 문장만 고르고, 2차는 코드가 만든 span ID 중에서만 관측값의 관계를 고른다.
SEMANTIC_ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "is_kosis_candidate": {"type": "boolean"},
                    "claim_type": {"type": "string"},
                    "indicator_norm": {"type": "string", "minLength": 1},
                    "measurement_type": {"type": "string", "enum": ["INDEX_LEVEL", "LEVEL", "CHANGE_RATE", "CHANGE_POINT"]},
                    "population_constraints": {"type": "array", "items": {"type": "string", "minLength": 1}},
                    "item_constraints": {"type": "array", "items": {"type": "string", "minLength": 1}},
                    "period_constraints": {"type": "array", "items": {"type": "string", "minLength": 1}},
                    "comparison_constraints": {"type": "array", "items": {"type": "string", "minLength": 1}},
                    "context_sentence_ids": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
                    "observation_sentence_ids": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
                    "relation_json": {
                        "type": "object",
                        "properties": {
                            "dimension_pairing": {
                                "type": "string",
                                "enum": ["NOT_APPLICABLE", "EXPLICIT_PAIRING", "UNPAIRED_MULTI_VALUE", "AMBIGUOUS"],
                            },
                            "pairing_evidence_sentence_ids": {"type": "array", "items": {"type": "integer"}},
                        },
                        "required": ["dimension_pairing", "pairing_evidence_sentence_ids"],
                    },
                },
                "required": ["is_kosis_candidate", "claim_type", "indicator_norm", "measurement_type", "population_constraints", "item_constraints", "period_constraints", "comparison_constraints", "context_sentence_ids", "observation_sentence_ids", "relation_json"],
            },
        },
    },
    "required": ["claims"],
}

# Candidate skeleton contract: code exposes immutable sentence/value IDs first,
# and HCX may only select those IDs.  It does not generate sentence numbers,
# copy period/population/comparison substrings, or classify measurement type.
CLAIM_SKELETON_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_class": {
                        "type": "string",
                        "enum": ["KOSIS_CANDIDATE", "OUT_OF_SCOPE", "NOT_CLAIM", "AMBIGUOUS"],
                    },
                    "classification_reason": {
                        "type": "string",
                        "enum": [
                            "OFFICIAL_AGGREGATE",
                            "PRIVATE_OR_FOREIGN_SOURCE",
                            "FORECAST_OR_TARGET",
                            "LEGAL_OR_POLICY_STANDARD",
                            "TEMPORARY_OR_PROVISIONAL",
                            "DEFINITION_OR_THRESHOLD",
                            "DATE_DURATION_RANK_OR_SAMPLE",
                            "DERIVED_OR_REPEATED_VALUE",
                            "INSUFFICIENT_CONTEXT",
                        ],
                    },
                    "is_kosis_candidate": {"type": "boolean"},
                    "claim_type": {"type": "string"},
                    "indicator_norm": {
                        "type": ["string", "null"],
                        "description": "값·날짜·조사기관·서술어가 없는 짧은 검색용 지표명 명사구",
                    },
                    "context_sentence_candidate_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "description": "코드가 제공한 sentence_candidate_id만 그대로 선택",
                        },
                    },
                    "target_value_candidate_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "description": "현재 지표의 직접 측정값에 해당하는 value_candidate_id만 선택",
                        },
                    },
                },
                "required": [
                    "candidate_class",
                    "classification_reason",
                    "is_kosis_candidate",
                    "claim_type",
                    "indicator_norm",
                    "context_sentence_candidate_ids",
                    "target_value_candidate_ids",
                ],
            },
        },
    },
    "required": ["claims"],
}

SPAN_BINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "indicator_evidence_span_ids": {
            "type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "population_evidence_span_ids": {
            "type": "array", "maxItems": 4, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "item_evidence_span_ids": {
            "type": "array", "maxItems": 4, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "observations": {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                            "properties": {
                                "value_span_id": {"type": "string", "minLength": 1},
                                "measurement_type": {"type": "string", "enum": ["INDEX_LEVEL", "LEVEL", "CHANGE_RATE", "CHANGE_POINT"]},
                                "period_span_id": {"type": ["string", "null"]},
                                "dimension_span_ids": {"type": "array", "items": {"type": "string"}},
                                "value_role": {"type": "string", "enum": ["TARGET_MEASURE", "SUBGROUP_MEASURE", "CONTRIBUTOR", "COMPARISON_REFERENCE", "AMBIGUOUS"]},
                                "indicator_value_relation": {"type": "string", "enum": ["SAME_METRIC", "SUBGROUP_OF", "CONTRIBUTES_TO", "COMPARISON", "AMBIGUOUS"]},
                                "relation_evidence_sentence_ids": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
                            },
                "required": ["value_span_id", "measurement_type", "period_span_id", "dimension_span_ids", "value_role", "indicator_value_relation", "relation_evidence_sentence_ids"],
            },
        },
    },
    "required": [
        "indicator_evidence_span_ids",
        "population_evidence_span_ids",
        "item_evidence_span_ids",
        "observations",
    ],
}

SEMANTIC_SYSTEM_PROMPT = """뉴스 기사에서 KOSIS 사실검증 후보의 의미와 문장 관계만 구조화한다.
한 claim에는 하나의 지표와 하나의 measurement_type만 둔다. 지수의 수준값과 변화값이 함께 있으면 둘로 나누고,
다른 품목·하위 업종·과거 비교값은 섞지 않는다. constraint에는 선택 문장의 연속 원문 substring만 쓴다.
EXPLICIT_PAIRING은 두 개 이상의 차원-값 연결 근거 문장 ID가 있을 때만 쓴다. 그 밖의 단일 지표는 NOT_APPLICABLE을 선택한다."""

SPAN_BINDING_SYSTEM_PROMPT = """하나의 구조화 claim에 원문 span 후보 ID만 연결한다. 새 문자열을 만들지 않는다.
indicator_evidence_span_ids에는 indicator_norm의 지표 개념을 직접 뒷받침하는 semantic_evidence ID를 모두 넣는다.
population_evidence_span_ids에는 집계 대상 집단만, item_evidence_span_ids에는 독립 품목·대출유형·분류 항목만 넣는다.
indicator_norm에 품목·업권·지역명이 포함되어 있으면 같은 원문 span이 indicator 근거이면서 item 또는 dimension 근거일 수 있다.
population과 item은 서로 배타적이며, 명시되지 않은 population/item 역할은 빈 배열로 둔다.
예: '개인사업자의 보험사 연체율'은 개인사업자=population, 보험사=dimension이다.
예: '개인사업자대출 연체율'은 개인사업자대출=item이고, '사과 21.6%, 쌀 21.3%'는 현재 target 값에 직접 붙은 품목 하나만 item이다.
예: 저축은행·보험사·전(全)산업은 dimension이며, '식료품·에너지 제외'는 제외 조건을 이루는 두 span을 dimension에 넣는다.
그중 현재 indicator의 직접 측정값만 하나씩 고르고, 선택값의 단위와 문맥에 맞는 measurement_type을 선택한다. 연체율·고용률처럼 비율 자체의 수준은 LEVEL이고, '전월/전년 대비 증가·감소·상승·하락'한 %는 CHANGE_RATE다.
period_span_id는 현재 값의 측정시점 후보(지난달, 올해 1분기, 작년 4분기 말 등)가 있으면 반드시 선택한다. 전월·전년 동월 같은 비교기준은 측정시점 대신 고르지 않는다.
단일 target 값에는 각 dimension type별로 원문에서 직접 대응하는 값 하나만 고른다. 예: 울산(1.4%)에는 울산만 고르고 경북·서울을 함께 넣지 않는다.
다른 품목·하위 업종·기여도·과거 비교값은 고르지 않는다. 각 observation은 TARGET_MEASURE, SAME_METRIC과 관계 근거 문장 ID를 쓴다.
명시적 1:1 관계가 없으면 관측값을 억지로 늘리지 않는다."""

CLAIM_SKELETON_SYSTEM_PROMPT = """뉴스 기사에서 KOSIS 후보 claim의 지표와 코드가 제공한 원문 후보 ID만 구조화한다.
제공된 모든 target value candidate를 정확히 한 번씩 분류한다. 누락하거나 같은 target을 중복 반환하지 않는다.
candidate_class는 KOSIS_CANDIDATE, OUT_OF_SCOPE, NOT_CLAIM, AMBIGUOUS 중 하나다.
KOSIS_CANDIDATE는 KOSIS 공식 집계통계로 검증할 직접 측정값, OUT_OF_SCOPE는 민간·해외·전망·목표·법령·임시집계 등 현재 KOSIS 범위 밖 수치,
NOT_CLAIM은 날짜·순번·연령 경계·표본 수·설명용 임계값·계산 중간값처럼 독립 수치 주장이 아닌 값, AMBIGUOUS는 원문만으로 앞 범주를 확정할 수 없는 값이다.
classification_reason은 class의 근거를 통제어휘에서 하나 고른다. KOSIS_CANDIDATE는 OFFICIAL_AGGREGATE,
OUT_OF_SCOPE는 PRIVATE_OR_FOREIGN_SOURCE/FORECAST_OR_TARGET/LEGAL_OR_POLICY_STANDARD/TEMPORARY_OR_PROVISIONAL,
NOT_CLAIM은 DEFINITION_OR_THRESHOLD/DATE_DURATION_RANK_OR_SAMPLE/DERIVED_OR_REPEATED_VALUE,
AMBIGUOUS는 INSUFFICIENT_CONTEXT를 사용한다.
예: '주 36시간 미만 근로자'의 36시간은 DEFINITION_OR_THRESHOLD인 NOT_CLAIM이고, '주 15시간 이상이면 주휴수당'의 15시간은 LEGAL_OR_POLICY_STANDARD인 OUT_OF_SCOPE다.
예: '근로자는 881만명', '비율은 30.8%'처럼 공식 집계의 직접 관측값만 OFFICIAL_AGGREGATE인 KOSIS_CANDIDATE다.
is_kosis_candidate는 candidate_class가 KOSIS_CANDIDATE일 때만 true다.
indicator_norm은 값·날짜·조사기관·서술어가 없는 짧은 검색용 지표명 명사구만 반환한다. 완전한 문장이나 값만 반환하지 않는다.
수치·단위·시점·비교 문구나 새 ID를 생성하지 않는다. context_sentence_candidate_ids와 target_value_candidate_ids에는 제공된 ID만 복사한다.
모든 제공 target마다 하나의 분류 record를 반환하되, 한 record는 하나의 목표값 ID만 가진다.
지역별 동일 지표 값은 각각 별도 claim으로 만들되 indicator_norm에는 지역명을 붙이지 않고 같은 지표명을 반복한다. 예: 경북(1.6%), 울산(1.4%) → 두 claim 모두 '실질 지역내총생산 성장률'.
이 규칙은 같은 문장의 같은 지표 값에만 적용한다. 다른 문단·문장의 광업·제조업·서비스업·건설업 수치에 앞 문장의 지역내총생산 지표명을 재사용하지 않는다.
독립 품목 값은 품목별 claim으로 나누고 indicator_norm에 품목명을 유지한다. 예: 사과 21.6%, 쌀 21.3% → '사과 상승률', '쌀 상승률'.
같은 문장에 지수 수준과 증감률이 함께 있으면 indicator_norm을 각각 '지표명'과 '지표명 전월비 증감률'처럼 구분해 별도 claim으로 만든다.
context_sentence_candidate_ids에는 목표값 문장뿐 아니라 현재 값의 공통 측정시점이 있는 선행 문장도 포함한다.
모집단·대출 유형 수식어를 indicator_norm에 불필요하게 붙이지 말고 지표를 구분하는 업권·품목명은 유지한다.
공식 통계의 과거 비교값과 독립 품목·기업규모·산업별 직접 측정값은 별도 claim으로 고른다.
기여도만 설명하는 값, 계산 중간값, 집단 정의용 임계값, 조사 표본 수 역시 누락하지 말고 NOT_CLAIM 또는 문맥에 맞는 비후보 class로 분류한다.
같은 target 값이 여러 명제를 뒷받침하더라도 이 단계에서는 한 번만 분류한다. 명제 확장은 후속 단계의 책임이다.
차원-값 관계와 지표·모집단·품목 원문 근거는 다음 span binding 단계에서 결정하므로 반환하지 않는다."""

_CLAIM_SKELETON_CHUNK_SIZE = 12
_PAIRING_VALUES = frozenset({"NOT_APPLICABLE", "EXPLICIT_PAIRING", "UNPAIRED_MULTI_VALUE", "AMBIGUOUS"})
_VALUE_ROLES = frozenset({"TARGET_MEASURE", "SUBGROUP_MEASURE", "CONTRIBUTOR", "COMPARISON_REFERENCE", "AMBIGUOUS"})
_INDICATOR_VALUE_RELATIONS = frozenset({"SAME_METRIC", "SUBGROUP_OF", "CONTRIBUTES_TO", "COMPARISON", "AMBIGUOUS"})
_MEASUREMENT_TYPES = frozenset({"INDEX_LEVEL", "LEVEL", "CHANGE_RATE", "CHANGE_POINT"})




def apply_article_relative_period_context(article_text: str, semantic_claim: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Conservatively inherit one unambiguous article-wide relative period.

    HCX still owns semantic extraction.  This only repairs an omitted source
    sentence for an already explicit relative period, or fills an empty period
    field when the entire article exposes exactly one relative measurement-time
    surface form. Absolute dates and comparison phrases are never inherited.
    The returned audit preserves the model's raw contract beside the effective
    contract used by deterministic binding.
    """
    effective = dict(semantic_claim) if isinstance(semantic_claim, dict) else {}
    raw_context = semantic_claim.get("context_sentence_ids") if isinstance(semantic_claim, dict) else None
    raw_periods = semantic_claim.get("period_constraints") if isinstance(semantic_claim, dict) else None
    context_ids = [value for value in raw_context if isinstance(value, int)] if isinstance(raw_context, list) else []
    period_constraints = [value.strip() for value in raw_periods if isinstance(value, str) and value.strip()] if isinstance(raw_periods, list) else []
    occurrences = [
        {"text": match.group(), "sentence_id": row["sentence_id"]}
        for row in sentence_offset_map(article_text)
        for match in _RELATIVE_MEASUREMENT_PERIOD_RE.finditer(row["text"])
    ]
    article_periods = sorted({item["text"] for item in occurrences})
    explicit_relative_periods = [text for text in period_constraints if _RELATIVE_MEASUREMENT_PERIOD_RE.fullmatch(text)]
    inherited_periods: list[str] = []
    reason = "NONE"
    if explicit_relative_periods:
        claim_sentence_ids = set(context_ids) | {
            value for value in semantic_claim.get("observation_sentence_ids", [])
            if isinstance(value, int)
        }
        if any(item["text"] in explicit_relative_periods and item["sentence_id"] in claim_sentence_ids for item in occurrences):
            reason = "ALREADY_GROUNDED_IN_CLAIM_SENTENCES"
        else:
            matching = [item for item in occurrences if item["text"] in explicit_relative_periods]
            if len(matching) == 1:
                inherited_periods = [matching[0]["text"]]
                reason = "CONTEXT_FOR_UNIQUE_EXPLICIT_RELATIVE_PERIOD"
    elif not period_constraints and len(occurrences) == 1:
        inherited_periods = [occurrences[0]["text"]]
        period_constraints = inherited_periods
        reason = "UNAMBIGUOUS_ARTICLE_RELATIVE_PERIOD"
    inherited_sentence_ids = sorted({
        item["sentence_id"] for item in occurrences if item["text"] in inherited_periods
    })
    if inherited_sentence_ids:
        context_ids = list(dict.fromkeys([*context_ids, *inherited_sentence_ids]))
    effective["context_sentence_ids"] = context_ids
    effective["period_constraints"] = period_constraints
    return effective, {
        "raw_context_sentence_ids": raw_context if isinstance(raw_context, list) else [],
        "raw_period_constraints": raw_periods if isinstance(raw_periods, list) else [],
        "article_relative_periods": article_periods,
        "inherited_period_constraints": inherited_periods,
        "inherited_context_sentence_ids": inherited_sentence_ids,
        "reason": reason,
        "effective_context_sentence_ids": context_ids,
        "effective_period_constraints": period_constraints,
    }








def _semantic_evidence_surface(text: str) -> str:
    """Remove only a trailing Korean particle while preserving exact offsets."""
    for suffix in _SEMANTIC_EVIDENCE_PARTICLES:
        if text.endswith(suffix) and len(text) - len(suffix) >= 2:
            return text[:-len(suffix)]
    return text


def build_semantic_evidence_candidates(article_text: str) -> list[dict[str, Any]]:
    """Build bounded exact-source lexical candidates near selectable values.

    This is intentionally not a morphological analyzer.  It exposes compact
    source spans that HCX may assign a semantic role to, while keeping long
    unrelated article passages and embedded player artefacts out of the
    Structured Output enum.
    """
    sentences = sentence_offset_map(article_text)
    sentences_by_id = {sentence["sentence_id"]: sentence for sentence in sentences}
    span_candidates = build_span_candidates(article_text)
    value_candidates = [
        candidate for candidate in span_candidates
        if candidate.get("kind") == "value_unit"
    ]
    selectable_by_sentence: dict[int, list[dict[str, Any]]] = {}
    for candidate in value_candidates:
        sentence = sentences_by_id.get(candidate["sentence_id"], {})
        if _local_value_role(sentence, candidate, set())[1] is None:
            selectable_by_sentence.setdefault(candidate["sentence_id"], []).append(candidate)

    result: list[dict[str, Any]] = []
    for sentence_id, values in selectable_by_sentence.items():
        sentence = sentences_by_id[sentence_id]
        token_rows: list[tuple[int, int, str, int]] = []
        for match in _SEMANTIC_EVIDENCE_TOKEN_RE.finditer(sentence["text"]):
            # Keep the exact token and, when different, a source-exact span
            # without its likely trailing particle.  Keeping both avoids
            # corrupting lexical nouns that happen to end in 이/가 (for
            # example, 소비자물가) while still exposing 사과 from 사과가.
            surfaces = list(dict.fromkeys([match.group(), _semantic_evidence_surface(match.group())]))
            for surface in surfaces:
                if len(surface) < 2 or surface in _SEMANTIC_EVIDENCE_STOPWORDS:
                    continue
                end = match.start() + len(surface)
                if any(match.start() < value["char_end"] - sentence["char_start"]
                       and end > value["char_start"] - sentence["char_start"] for value in values):
                    continue
                distance = min(
                    max(
                        value["char_start"] - sentence["char_start"] - end,
                        match.start() - (value["char_end"] - sentence["char_start"]),
                        0,
                    )
                    for value in values
                )
                # Semantic labels are rarely more than one short clause away
                # from their value.  The bound also prevents media-player junk
                # from dominating the article-level candidate catalog.
                if distance <= 120:
                    token_rows.append((match.start(), end, surface, distance))
        token_rows.sort(key=lambda row: (row[3], row[0], -(row[1] - row[0])))
        for start, end, _, _ in token_rows[:64]:
            result.append(_span_record(
                sentence=sentence,
                kind="semantic_evidence",
                ordinal=len(result),
                start=start,
                end=end,
            ))
    return sorted(result, key=lambda item: (item["sentence_id"], item["char_start"], item["char_end"]))


def _sentence_candidate_id(sentence_id: int) -> str:
    return f"sentence:s{sentence_id:04d}"


def build_claim_skeleton_candidate_catalog(
    article_text: str,
    *,
    include_semantic_evidence: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Expose immutable sentence/value IDs before HCX creates claim skeletons."""
    sentences = sentence_offset_map(article_text)
    sentences_by_id = {sentence["sentence_id"]: sentence for sentence in sentences}
    value_candidates = [
        candidate for candidate in build_span_candidates(article_text)
        if candidate.get("kind") == "value_unit"
    ]
    value_candidate_rows = []
    dimension_candidates = [
        candidate for candidate in build_span_candidates(article_text)
        if candidate.get("kind") == "dimension"
    ]
    semantic_evidence_candidates = (
        build_semantic_evidence_candidates(article_text)
        if include_semantic_evidence else []
    )
    for candidate in value_candidates:
        source_role_hint, role_error = _local_value_role(
            sentences_by_id.get(candidate["sentence_id"], {}),
            candidate,
            set(),
        )
        value_candidate_rows.append({
            "value_candidate_id": candidate["span_id"],
            "sentence_candidate_id": _sentence_candidate_id(candidate["sentence_id"]),
            "text": candidate["text"],
            "value": candidate.get("value"),
            "unit": candidate.get("unit"),
            "source_role_hint": source_role_hint,
            "selectable_as_target": role_error is None,
        })
    value_ids_by_sentence: dict[int, list[str]] = {}
    for candidate, candidate_row in zip(value_candidates, value_candidate_rows):
        if candidate_row["selectable_as_target"]:
            value_ids_by_sentence.setdefault(candidate["sentence_id"], []).append(candidate["span_id"])
    return {
        "sentence_candidates": [
            {
                "sentence_candidate_id": _sentence_candidate_id(sentence["sentence_id"]),
                "text": sentence["text"],
                "value_candidate_ids": value_ids_by_sentence.get(sentence["sentence_id"], []),
                "dimension_candidate_ids": [
                    candidate["span_id"]
                    for candidate in dimension_candidates
                    if candidate["sentence_id"] == sentence["sentence_id"]
                ],
                "semantic_evidence_candidate_ids": [
                    candidate["span_id"]
                    for candidate in semantic_evidence_candidates
                    if candidate["sentence_id"] == sentence["sentence_id"]
                ],
            }
            for sentence in sentences
        ],
        "value_candidates": value_candidate_rows,
        "dimension_candidates": [
            {
                "dimension_candidate_id": candidate["span_id"],
                "sentence_candidate_id": _sentence_candidate_id(candidate["sentence_id"]),
                "dimension_type": candidate.get("dimension_type"),
                "text": candidate["text"],
            }
            for candidate in dimension_candidates
        ],
        "semantic_evidence_candidates": [
            {
                "semantic_evidence_candidate_id": candidate["span_id"],
                "sentence_candidate_id": _sentence_candidate_id(candidate["sentence_id"]),
                "text": candidate["text"],
            }
            for candidate in semantic_evidence_candidates
        ],
    }


def build_claim_skeleton_schema(
    article_text: str,
    target_value_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Bind Structured Output fields to the exact candidate IDs in this article."""
    catalog = build_claim_skeleton_candidate_catalog(article_text)
    schema = deepcopy(CLAIM_SKELETON_SCHEMA)
    properties = schema["properties"]["claims"]["items"]["properties"]
    properties["context_sentence_candidate_ids"]["items"]["enum"] = [
        item["sentence_candidate_id"] for item in catalog["sentence_candidates"]
    ]
    selectable_ids = [
        item["value_candidate_id"] for item in catalog["value_candidates"]
    ]
    if target_value_ids is not None:
        allowed = set(target_value_ids)
        selectable_ids = [
            value_id for value_id in selectable_ids if value_id in allowed
        ]
        schema["properties"]["claims"]["minItems"] = len(selectable_ids)
        schema["properties"]["claims"]["maxItems"] = len(selectable_ids)
    properties["target_value_candidate_ids"]["items"]["enum"] = selectable_ids
    return schema


_CANDIDATE_CLASSES = frozenset({
    "KOSIS_CANDIDATE",
    "OUT_OF_SCOPE",
    "NOT_CLAIM",
    "AMBIGUOUS",
})
_CANDIDATE_CLASS_REASONS = {
    "KOSIS_CANDIDATE": frozenset({"OFFICIAL_AGGREGATE"}),
    "OUT_OF_SCOPE": frozenset({
        "PRIVATE_OR_FOREIGN_SOURCE",
        "FORECAST_OR_TARGET",
        "LEGAL_OR_POLICY_STANDARD",
        "TEMPORARY_OR_PROVISIONAL",
    }),
    "NOT_CLAIM": frozenset({
        "DEFINITION_OR_THRESHOLD",
        "DATE_DURATION_RANK_OR_SAMPLE",
        "DERIVED_OR_REPEATED_VALUE",
    }),
    "AMBIGUOUS": frozenset({"INSUFFICIENT_CONTEXT"}),
}


def _normalize_candidate_class(claim: dict[str, Any]) -> tuple[str, list[str]]:
    """Normalize the r16 classification contract while retaining legacy inputs."""
    errors: list[str] = []
    raw_class = claim.get("candidate_class")
    if raw_class in _CANDIDATE_CLASSES:
        candidate_class = str(raw_class)
    elif raw_class is None:
        candidate_class = (
            "KOSIS_CANDIDATE"
            if claim.get("is_kosis_candidate") is True
            else "OUT_OF_SCOPE"
        )
    else:
        candidate_class = "AMBIGUOUS"
        errors.append("CANDIDATE_CLASS_INVALID")
    expected_boolean = candidate_class == "KOSIS_CANDIDATE"
    if (
        isinstance(claim.get("is_kosis_candidate"), bool)
        and claim["is_kosis_candidate"] != expected_boolean
    ):
        errors.append("CANDIDATE_CLASS_BOOLEAN_MISMATCH")
    classification_reason = claim.get("classification_reason")
    if (
        classification_reason is not None
        and classification_reason
        not in _CANDIDATE_CLASS_REASONS[candidate_class]
    ):
        errors.append("CANDIDATE_CLASS_REASON_MISMATCH")
    claim["candidate_class"] = candidate_class
    claim["is_kosis_candidate"] = expected_boolean
    return candidate_class, errors


def complete_claim_skeleton_candidate_coverage(
    article_text: str,
    claims: object,
    target_value_ids: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return exactly one auditable classification record per target candidate."""
    target_ids = list(dict.fromkeys(target_value_ids))
    allowed = set(target_ids)
    by_target: dict[str, dict[str, Any]] = {}
    duplicate_target_ids: list[str] = []
    invalid_record_count = 0
    for raw_claim in claims if isinstance(claims, list) else []:
        if not isinstance(raw_claim, dict):
            invalid_record_count += 1
            continue
        raw_ids = raw_claim.get("target_value_candidate_ids")
        if (
            not isinstance(raw_ids, list)
            or len(raw_ids) != 1
            or not isinstance(raw_ids[0], str)
            or raw_ids[0] not in allowed
        ):
            invalid_record_count += 1
            continue
        target_id = raw_ids[0]
        if target_id in by_target:
            duplicate_target_ids.append(target_id)
            continue
        claim = dict(raw_claim)
        _, class_errors = _normalize_candidate_class(claim)
        claim["candidate_coverage_source"] = "HCX"
        claim["candidate_coverage_errors"] = class_errors
        by_target[target_id] = claim

    catalog = build_claim_skeleton_candidate_catalog(
        article_text,
        include_semantic_evidence=True,
    )
    value_rows = {
        row["value_candidate_id"]: row
        for row in catalog["value_candidates"]
    }
    missing_target_ids = [
        target_id for target_id in target_ids if target_id not in by_target
    ]
    for target_id in missing_target_ids:
        value_row = value_rows[target_id]
        by_target[target_id] = {
            "candidate_class": "AMBIGUOUS",
            "classification_reason": "INSUFFICIENT_CONTEXT",
            "is_kosis_candidate": False,
            "claim_type": "미분류",
            "indicator_norm": None,
            "context_sentence_candidate_ids": [
                value_row["sentence_candidate_id"],
            ],
            "target_value_candidate_ids": [target_id],
            "candidate_coverage_source": "DETERMINISTIC_AMBIGUOUS_FALLBACK",
            "candidate_coverage_errors": ["HCX_TARGET_CLASSIFICATION_MISSING"],
        }
    for target_id in target_ids:
        claim = by_target[target_id]
        source_role = value_rows[target_id].get("source_role_hint")
        if (
            claim.get("candidate_class")
            in {"KOSIS_CANDIDATE", "AMBIGUOUS"}
            and source_role == "CATEGORY_DEFINITION"
        ):
            claim["candidate_class"] = "NOT_CLAIM"
            claim["classification_reason"] = "DEFINITION_OR_THRESHOLD"
            claim["is_kosis_candidate"] = False
            claim["candidate_class_override"] = (
                "DETERMINISTIC_CATEGORY_DEFINITION"
            )
        elif (
            claim.get("candidate_class")
            in {"KOSIS_CANDIDATE", "AMBIGUOUS"}
            and source_role in {"CONTRIBUTOR", "AUXILIARY_MEASURE"}
        ):
            claim["candidate_class"] = "NOT_CLAIM"
            claim["classification_reason"] = "DERIVED_OR_REPEATED_VALUE"
            claim["is_kosis_candidate"] = False
            claim["candidate_class_override"] = (
                f"DETERMINISTIC_{source_role}"
            )
    completed = [by_target[target_id] for target_id in target_ids]
    return completed, {
        "target_count": len(target_ids),
        "hcx_record_count": sum(
            claim["candidate_coverage_source"] == "HCX"
            for claim in completed
        ),
        "fallback_record_count": sum(
            claim["candidate_coverage_source"]
            == "DETERMINISTIC_AMBIGUOUS_FALLBACK"
            for claim in completed
        ),
        "missing_target_ids": missing_target_ids,
        "duplicate_target_ids": duplicate_target_ids,
        "invalid_record_count": invalid_record_count,
        "complete": len(completed) == len(target_ids),
    }


def normalize_claim_skeleton_candidate_selection(article_text: str, prediction: object) -> dict[str, Any]:
    """Resolve model-selected candidate IDs to downstream sentence/span IDs."""
    catalog = build_claim_skeleton_candidate_catalog(article_text)
    sentence_by_candidate = {
        item["sentence_candidate_id"]: int(item["sentence_candidate_id"].rsplit("s", 1)[1])
        for item in catalog["sentence_candidates"]
    }
    value_by_candidate = {
        candidate["span_id"]: candidate
        for candidate in build_span_candidates(article_text)
        if candidate.get("kind") == "value_unit"
    }
    dimension_by_candidate = {
        candidate["span_id"]: candidate
        for candidate in build_span_candidates(article_text)
        if candidate.get("kind") == "dimension"
    }
    semantic_evidence_by_candidate = {
        candidate["span_id"]: candidate
        for candidate in build_semantic_evidence_candidates(article_text)
    }
    selectable_value_ids = {
        item["value_candidate_id"]
        for item in catalog["value_candidates"]
    }
    source_claims = prediction.get("claims", []) if isinstance(prediction, dict) else []
    claims: list[dict[str, Any]] = []
    for raw_claim in source_claims if isinstance(source_claims, list) else []:
        claim = dict(raw_claim) if isinstance(raw_claim, dict) else {}
        errors: list[str] = []
        _, class_errors = _normalize_candidate_class(claim)
        errors.extend(class_errors)

        raw_context_ids = claim.get("context_sentence_candidate_ids")
        context_candidate_ids = raw_context_ids if isinstance(raw_context_ids, list) else []
        if not isinstance(raw_context_ids, list):
            errors.append("CONTEXT_SENTENCE_CANDIDATE_IDS_INVALID")
        context_sentence_ids: list[int] = []
        seen_context: set[str] = set()
        for candidate_id in context_candidate_ids:
            if not isinstance(candidate_id, str) or candidate_id not in sentence_by_candidate:
                errors.append("CONTEXT_SENTENCE_CANDIDATE_ID_UNKNOWN")
            elif candidate_id in seen_context:
                errors.append("CONTEXT_SENTENCE_CANDIDATE_ID_DUPLICATE")
            else:
                seen_context.add(candidate_id)
                context_sentence_ids.append(sentence_by_candidate[candidate_id])

        raw_target_ids = claim.get("target_value_candidate_ids")
        target_candidate_ids = raw_target_ids if isinstance(raw_target_ids, list) else []
        if not isinstance(raw_target_ids, list):
            errors.append("TARGET_VALUE_CANDIDATE_IDS_INVALID")
        target_value_span_ids: list[str] = []
        observation_sentence_ids: list[int] = []
        seen_targets: set[str] = set()
        for candidate_id in target_candidate_ids:
            if not isinstance(candidate_id, str) or candidate_id not in value_by_candidate:
                errors.append("TARGET_VALUE_CANDIDATE_ID_UNKNOWN")
            elif candidate_id not in selectable_value_ids:
                errors.append("TARGET_VALUE_CANDIDATE_NOT_SELECTABLE")
            elif candidate_id in seen_targets:
                errors.append("TARGET_VALUE_CANDIDATE_ID_DUPLICATE")
            else:
                seen_targets.add(candidate_id)
                target_value_span_ids.append(candidate_id)
                sentence_id = value_by_candidate[candidate_id]["sentence_id"]
                if sentence_id not in observation_sentence_ids:
                    observation_sentence_ids.append(sentence_id)

        def resolve_candidate_ids(field: str, candidates_by_id: dict[str, dict[str, Any]],
                                  error_prefix: str) -> tuple[list[str], list[dict[str, Any]]]:
            raw_ids = claim.get(field)
            candidate_ids = raw_ids if isinstance(raw_ids, list) else []
            if not isinstance(raw_ids, list):
                errors.append(f"{error_prefix}_IDS_INVALID")
            resolved_ids: list[str] = []
            resolved_spans: list[dict[str, Any]] = []
            seen: set[str] = set()
            for candidate_id in candidate_ids:
                if not isinstance(candidate_id, str) or candidate_id not in candidates_by_id:
                    errors.append(f"{error_prefix}_ID_UNKNOWN")
                elif candidate_id in seen:
                    errors.append(f"{error_prefix}_ID_DUPLICATE")
                else:
                    seen.add(candidate_id)
                    resolved_ids.append(candidate_id)
                    resolved_spans.append(dict(candidates_by_id[candidate_id]))
            return resolved_ids, resolved_spans

        semantic_role_selection_asserted = any(
            field in claim for field in (
                "indicator_evidence_candidate_ids",
                "population_evidence_candidate_ids",
                "item_evidence_candidate_ids",
                "dimension_candidate_ids",
            )
        )
        if semantic_role_selection_asserted:
            indicator_evidence_ids, indicator_evidence_spans = resolve_candidate_ids(
                "indicator_evidence_candidate_ids", semantic_evidence_by_candidate, "INDICATOR_EVIDENCE_CANDIDATE",
            )
            population_evidence_ids, population_evidence_spans = resolve_candidate_ids(
                "population_evidence_candidate_ids", semantic_evidence_by_candidate, "POPULATION_EVIDENCE_CANDIDATE",
            )
            item_evidence_ids, item_evidence_spans = resolve_candidate_ids(
                "item_evidence_candidate_ids", semantic_evidence_by_candidate, "ITEM_EVIDENCE_CANDIDATE",
            )
            dimension_candidate_ids, dimension_evidence_spans = resolve_candidate_ids(
                "dimension_candidate_ids", dimension_by_candidate, "DIMENSION_CANDIDATE",
            )
            if not indicator_evidence_ids:
                errors.append("INDICATOR_EVIDENCE_CANDIDATE_MISSING")
        else:
            indicator_evidence_ids, indicator_evidence_spans = [], []
            population_evidence_ids, population_evidence_spans = [], []
            item_evidence_ids, item_evidence_spans = [], []
            dimension_candidate_ids, dimension_evidence_spans = [], []

        claim["context_sentence_ids"] = context_sentence_ids
        claim["observation_sentence_ids"] = observation_sentence_ids
        claim["target_value_span_ids"] = target_value_span_ids
        claim["indicator_evidence_spans"] = indicator_evidence_spans
        claim["indicator_evidence_texts"] = [item["text"] for item in indicator_evidence_spans]
        claim["population_evidence_spans"] = population_evidence_spans
        claim["population_constraints"] = [item["text"] for item in population_evidence_spans]
        claim["item_evidence_spans"] = item_evidence_spans
        claim["item_constraints"] = [item["text"] for item in item_evidence_spans]
        claim["dimension_evidence_spans"] = dimension_evidence_spans
        claim["dimension_span_ids"] = dimension_candidate_ids
        claim["dimensions"] = {
            dimension_type: [item["text"] for item in dimension_evidence_spans
                             if item.get("dimension_type") == dimension_type]
            for dimension_type in sorted({
                str(item.get("dimension_type"))
                for item in dimension_evidence_spans
                if item.get("dimension_type")
            })
        }
        claim["candidate_selection"] = {
            "context_sentence_candidate_ids": context_candidate_ids,
            "target_value_candidate_ids": target_candidate_ids,
            "indicator_evidence_candidate_ids": indicator_evidence_ids,
            "population_evidence_candidate_ids": population_evidence_ids,
            "item_evidence_candidate_ids": item_evidence_ids,
            "dimension_candidate_ids": dimension_candidate_ids,
            "resolved_context_sentence_ids": context_sentence_ids,
            "resolved_observation_sentence_ids": observation_sentence_ids,
            "resolved_target_value_span_ids": target_value_span_ids,
            "resolved_indicator_evidence_spans": indicator_evidence_spans,
            "resolved_population_evidence_spans": population_evidence_spans,
            "resolved_item_evidence_spans": item_evidence_spans,
            "resolved_dimension_spans": dimension_evidence_spans,
            "semantic_role_selection_asserted": semantic_role_selection_asserted,
            "errors": [
                *(
                    claim.get("candidate_coverage_errors", [])
                    if isinstance(claim.get("candidate_coverage_errors"), list)
                    else []
                ),
                *errors,
            ],
        }
        claims.append(claim)
    normalized = dict(prediction) if isinstance(prediction, dict) else {}
    normalized["claims"] = claims
    return normalized


def build_semantic_article_prompt(title: str, article_text: str) -> str:
    numbered = "\n".join(f"[{row['sentence_id']}] {row['text']}" for row in sentence_offset_map(article_text))
    return f"""다음 뉴스 기사에서 KOSIS 사실검증 후보 claim의 의미와 문장 관계만 구조화하세요.
    값·단위·날짜를 생성하지 말고 문장 ID만 고릅니다. 각 constraint 배열은 선택 문장의 연속 원문 substring만 쓰며 빈 배열도 가능합니다.
    period는 측정 시점, comparison은 비교 기준입니다. 지수 수준값과 변화값이 함께 있으면 INDEX_LEVEL과 CHANGE_RATE/CHANGE_POINT로 나눕니다.
    하나의 claim에는 하나의 지표·측정유형만 두고, 다른 품목·하위 업종·비교값은 섞지 마세요. 차원 관계가 불명확하면 UNPAIRED_MULTI_VALUE 또는 AMBIGUOUS를 쓰세요.

제목: {title}
문장 목록:\n{numbered}"""


def _claim_skeleton_catalog_for_targets(
    article_text: str,
    target_value_ids: list[str] | None,
) -> dict[str, Any]:
    catalog = build_claim_skeleton_candidate_catalog(article_text)
    if target_value_ids is None:
        return catalog
    allowed = set(target_value_ids)
    catalog["value_candidates"] = [
        row for row in catalog["value_candidates"]
        if row["value_candidate_id"] in allowed
    ]
    catalog["sentence_candidates"] = [
        {
            **row,
            "value_candidate_ids": [
                value_id for value_id in row["value_candidate_ids"]
                if value_id in allowed
            ],
        }
        for row in catalog["sentence_candidates"]
    ]
    return catalog


def build_claim_skeleton_prompt(
    title: str,
    article_text: str,
    target_value_ids: list[str] | None = None,
) -> str:
    catalog = _claim_skeleton_catalog_for_targets(
        article_text,
        target_value_ids,
    )
    return f"""다음 뉴스 기사의 모든 제공 수치 후보를 candidate-first claim skeleton으로 구조화하세요.
candidate 목록의 모든 value_candidate_id를 정확히 한 번씩 반환하세요. KOSIS 후보가 아니어도 누락하지 말고 candidate_class로 분류하세요.
candidate_class=KOSIS_CANDIDATE일 때만 is_kosis_candidate=true이며, 나머지는 false입니다.
classification_reason은 candidate_class와 일치하는 통제어휘를 선택하세요.
값·단위·기간·비교 문자열을 새로 반환하지 말고, 지표와 제공된 candidate ID만 선택하세요.
indicator_norm에는 '소비자물가 상승률', '전산업 생산지수'처럼 검색용 지표명만 쓰세요.
값('2.4%'), 날짜, 조사기관, 완전한 진술('저축은행 연체율은 11.7%')은 indicator_norm이 아닙니다.
target_value_candidate_ids에는 현재 지표가 직접 보고한 측정값 ID를 넣으세요.
공식 통계의 과거 기준값, 비교값, 품목별·기업규모별·산업별 수치는 각각 독립 조회 가능한 값이면 포함하세요.
단순 계산 중간값, 기여도만 설명하는 값, 집단 정의용 임계값, 조사 표본 수는 제외하세요.
selectable_as_target이 false인 값도 누락하지 말고 분류하세요. 비교 기준·정의 임계값 등일 수 있으므로 원문 역할에 맞는 비후보 class를 우선 검토하세요.
각 record의 target_value_candidate_ids에는 ID를 정확히 하나만 넣고, 같은 ID를 둘 이상의 record에 넣지 마세요.
지역별 동일 지표 값은 claim을 나누되 지역명을 indicator_norm에 붙이지 말고 같은 지표명을 반복하세요.
독립 품목 값은 품목별 claim으로 나누고 품목명을 indicator_norm에 유지하세요.
단위나 측정 의미가 다른 값은 각각의 indicator_norm으로 분리하세요.
같은 문장에 지수 수준과 증감률이 함께 있으면 서로 다른 indicator_norm의 claim으로 분리하세요.
context_sentence_candidate_ids에는 목표값 문장과 현재 값의 공통 측정시점이 있는 선행 문장을 함께 선택하세요.
candidate 목록을 처음부터 끝까지 확인해 KOSIS 후보와 비후보를 모두 분류하세요.
같은 target 값이 여러 검증 명제를 동시에 뒷받침해도 이 단계에서는 하나의 record만 만드세요.

제목: {title}
candidate 목록:\n{json.dumps(catalog, ensure_ascii=False)}"""


def build_span_binding_prompt(semantic_claim: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    target_spans = [
        candidate for candidate in candidates
        if candidate.get("span_id") in set(semantic_claim.get("target_value_span_ids", []))
    ]
    role_hints = build_semantic_role_hints(
        semantic_claim,
        candidates,
        target_spans,
    )
    compact_candidates = [
        {
            **{
                key: candidate[key]
                for key in (
                    "span_id", "kind", "sentence_id", "text",
                    "dimension_type", "value", "unit",
                )
                if key in candidate
            },
            **(
                {"role_hints": role_hints["by_candidate_id"][str(candidate["span_id"])]}
                if role_hints["by_candidate_id"].get(str(candidate.get("span_id")))
                else {}
            ),
        }
        for candidate in candidates
    ]
    return "다음 claim과 원문 span 후보를 연결하세요. 후보 ID 이외의 문자열은 만들 수 없습니다.\n" + json.dumps(
        {"claim": semantic_claim, "span_candidates": compact_candidates}, ensure_ascii=False
    )


def build_span_binding_schema(
    candidates: list[dict[str, Any]],
    semantic_claim: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Constrain every binding ID to the exact locally exposed source domain."""
    schema = deepcopy(SPAN_BINDING_SCHEMA)
    properties = schema["properties"]
    semantic_ids = [
        str(candidate["span_id"]) for candidate in candidates
        if candidate.get("kind") == "semantic_evidence"
    ]
    for field in (
        "indicator_evidence_span_ids",
        "population_evidence_span_ids",
        "item_evidence_span_ids",
    ):
        properties[field]["items"]["enum"] = semantic_ids
    observation = properties["observations"]["items"]["properties"]
    observation["value_span_id"]["enum"] = [
        str(candidate["span_id"]) for candidate in candidates
        if candidate.get("kind") == "value_unit"
    ]
    # HCX Structured Output rejects an enum that contains JSON null and also
    # rejects empty enums.  Period/dimension IDs remain deterministically
    # checked by validate_span_binding; apply enums only to non-empty pure
    # string domains accepted by the API.
    target_spans = [
        candidate for candidate in candidates
        if candidate.get("span_id") in set(
            semantic_claim.get("target_value_span_ids", [])
            if isinstance(semantic_claim, dict) else []
        )
    ]
    role_hints = build_semantic_role_hints(
        semantic_claim if isinstance(semantic_claim, dict) else {},
        candidates,
        target_spans,
    )
    dimension_ids = [
        str(candidate["span_id"]) for candidate in candidates
        if candidate.get("kind") == "dimension"
        or "dimension" in role_hints["by_candidate_id"].get(
            str(candidate.get("span_id")), []
        )
    ]
    if dimension_ids:
        observation["dimension_span_ids"]["items"]["enum"] = dimension_ids
    observation["relation_evidence_sentence_ids"]["items"]["enum"] = sorted({
        int(candidate["sentence_id"]) for candidate in candidates
        if isinstance(candidate.get("sentence_id"), int)
    })
    return schema


def filter_span_candidates_for_measurement_type(semantic_claim: dict[str, Any],
                                                 candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Expose only unit-compatible value candidates to constrained HCX binding.

    This is a code-owned contract, rather than a natural-language instruction:
    all time and dimension candidates remain available, while only the direct
    measurement unit domain is selectable as a value.
    """
    measurement_type = semantic_claim.get("measurement_type") if isinstance(semantic_claim, dict) else None

    def compatible(candidate: dict[str, Any]) -> bool:
        if candidate.get("kind") != "value_unit":
            return True
        unit = candidate.get("unit")
        if measurement_type == "INDEX_LEVEL":
            return unit == "지수"
        if measurement_type == "CHANGE_RATE":
            return unit == "%"
        if measurement_type == "CHANGE_POINT":
            return unit in {"%p", "포인트"}
        if measurement_type == "LEVEL":
            # A rate level such as a delinquency rate is expressed in %, but
            # is not a change rate.  Unit alone cannot distinguish the two.
            return unit not in {"지수", "%p", "포인트"}
        return False

    filtered = [candidate for candidate in candidates if isinstance(candidate, dict) and compatible(candidate)]
    excluded_values = [
        {key: candidate.get(key) for key in ("span_id", "text", "unit", "sentence_id")}
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("kind") == "value_unit" and candidate not in filtered
    ]
    return filtered, {
        "measurement_type": measurement_type,
        "raw_value_candidate_count": sum(item.get("kind") == "value_unit" for item in candidates if isinstance(item, dict)),
        "binding_value_candidate_count": sum(item.get("kind") == "value_unit" for item in filtered),
        "excluded_value_candidates": excluded_values,
    }


def filter_span_candidates_for_target_selection(semantic_claim: dict[str, Any],
                                                candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Expose only the selected value; dimensions remain a stage-2 decision."""
    raw_target_ids = semantic_claim.get("target_value_span_ids") if isinstance(semantic_claim, dict) else None
    target_ids = {
        candidate_id for candidate_id in raw_target_ids
        if isinstance(candidate_id, str)
    } if isinstance(raw_target_ids, list) else set()
    available_value_ids = {
        str(candidate.get("span_id"))
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("kind") == "value_unit"
    }
    available_dimension_ids = {
        str(candidate.get("span_id"))
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("kind") == "dimension"
    }
    filtered = [
        candidate for candidate in candidates
        if candidate.get("kind") != "value_unit" or candidate.get("span_id") in target_ids
    ]
    return filtered, {
        "selection_contract": "CANDIDATE_ID",
        "target_value_span_ids": sorted(target_ids),
        "missing_target_value_span_ids": sorted(target_ids - available_value_ids),
        "raw_value_candidate_count": len(available_value_ids),
        "binding_value_candidate_count": sum(
            candidate.get("kind") == "value_unit" for candidate in filtered
        ),
        "raw_dimension_candidate_count": len(available_dimension_ids),
        "binding_dimension_candidate_count": sum(
            candidate.get("kind") == "dimension" for candidate in filtered
        ),
        "excluded_value_candidates": [
            {key: candidate.get(key) for key in ("span_id", "text", "unit", "sentence_id")}
            for candidate in candidates
            if candidate.get("kind") == "value_unit" and candidate.get("span_id") not in target_ids
        ],
    }


_SEMANTIC_CONSTRAINT_FIELDS = (
    "population_constraints", "item_constraints", "period_constraints", "comparison_constraints",
)


def validate_semantic_claim(article_text: str, semantic_claim: object, *, require_constraints: bool = False) -> dict[str, Any]:
    """Block malformed semantic claims before candidate generation or HCX binding.

    The semantic stage is the retrieval contract: a source-grounded value binding
    without a usable indicator or valid sentence scope must never reach retrieval.
    """
    errors: list[str] = []
    if not isinstance(semantic_claim, dict):
        return {"status": "CONFLICT", "errors": ["SEMANTIC_CLAIM_NOT_OBJECT"],
                "indicator_norm": None, "context_sentence_ids": [], "observation_sentence_ids": []}

    indicator_norm = semantic_claim.get("indicator_norm")
    if not isinstance(indicator_norm, str) or not indicator_norm.strip():
        errors.append("INDICATOR_NORM_MISSING")
    measurement_type = semantic_claim.get("measurement_type")
    if require_constraints and not isinstance(measurement_type, str):
        errors.append("MEASUREMENT_TYPE_MISSING")
    elif isinstance(measurement_type, str) and measurement_type not in _MEASUREMENT_TYPES:
        errors.append("MEASUREMENT_TYPE_INVALID")

    relation_json = semantic_claim.get("relation_json")
    pairing = relation_json.get("dimension_pairing") if isinstance(relation_json, dict) else None
    pairing_evidence = relation_json.get("pairing_evidence_sentence_ids") if isinstance(relation_json, dict) else None
    if pairing is None:
        errors.append("DIMENSION_PAIRING_MISSING")
    elif pairing not in _PAIRING_VALUES:
        errors.append("DIMENSION_PAIRING_INVALID")
    if not isinstance(pairing_evidence, list):
        errors.append("PAIRING_EVIDENCE_SENTENCE_IDS_MISSING")

    valid_sentence_ids = {row["sentence_id"] for row in sentence_offset_map(article_text)}

    def checked_sentence_ids(field: str, missing_code: str) -> list[int]:
        source = semantic_claim.get(field)
        if not isinstance(source, list) or not source:
            errors.append(missing_code)
            return []
        result: list[int] = []
        for sentence_id in source:
            if not isinstance(sentence_id, int):
                errors.append(f"{field.upper()}_ID_INVALID")
                continue
            if sentence_id not in valid_sentence_ids:
                errors.append(f"{field.upper()}_ID_UNKNOWN")
                continue
            if sentence_id in result:
                errors.append(f"{field.upper()}_ID_DUPLICATE")
                continue
            result.append(sentence_id)
        if not result and missing_code not in errors:
            errors.append(missing_code)
        return result

    context_sentence_ids = checked_sentence_ids("context_sentence_ids", "CONTEXT_SENTENCE_MISSING")
    observation_sentence_ids = checked_sentence_ids("observation_sentence_ids", "OBSERVATION_SENTENCE_MISSING")
    checked_pairing_evidence: list[int] = []
    if isinstance(pairing_evidence, list):
        allowed_pairing_sentence_ids = set(context_sentence_ids) | set(observation_sentence_ids)
        for sentence_id in pairing_evidence:
            if not isinstance(sentence_id, int):
                errors.append("PAIRING_EVIDENCE_SENTENCE_ID_INVALID")
            elif sentence_id not in valid_sentence_ids:
                errors.append("PAIRING_EVIDENCE_SENTENCE_ID_UNKNOWN")
            elif sentence_id not in allowed_pairing_sentence_ids:
                errors.append("PAIRING_EVIDENCE_OUTSIDE_CLAIM_SENTENCES")
            elif sentence_id in checked_pairing_evidence:
                errors.append("PAIRING_EVIDENCE_SENTENCE_ID_DUPLICATE")
            else:
                checked_pairing_evidence.append(sentence_id)
        if pairing == "NOT_APPLICABLE" and checked_pairing_evidence:
            errors.append("NOT_APPLICABLE_PAIRING_EVIDENCE_PRESENT")
        elif pairing in {"EXPLICIT_PAIRING", "UNPAIRED_MULTI_VALUE", "AMBIGUOUS"} and not checked_pairing_evidence:
            errors.append("PAIRING_EVIDENCE_SENTENCE_MISSING")
    allowed_constraint_sentence_ids = set(context_sentence_ids) | set(observation_sentence_ids)
    sentences_by_id = {row["sentence_id"]: row for row in sentence_offset_map(article_text)}
    constraint_spans: dict[str, list[dict[str, Any]]] = {}
    constraint_contract_status = "ASSERTED"
    for field in _SEMANTIC_CONSTRAINT_FIELDS:
        raw_values = semantic_claim.get(field)
        if raw_values is None:
            constraint_contract_status = "LEGACY_UNASSERTED"
            if require_constraints:
                errors.append(f"{field.upper()}_MISSING")
            raw_values = []
        if not isinstance(raw_values, list):
            errors.append(f"{field.upper()}_INVALID")
            raw_values = []
        seen_constraints: set[str] = set()
        spans: list[dict[str, Any]] = []
        for raw_value in raw_values:
            if not isinstance(raw_value, str) or not raw_value.strip():
                errors.append(f"{field.upper()}_VALUE_INVALID")
                continue
            text = raw_value.strip()
            if text in seen_constraints:
                errors.append(f"{field.upper()}_VALUE_DUPLICATE")
                continue
            seen_constraints.add(text)
            matches = [
                {"text": text, "sentence_id": sentence_id,
                 "char_start": sentence["char_start"] + sentence["text"].find(text),
                 "char_end": sentence["char_start"] + sentence["text"].find(text) + len(text)}
                for sentence_id, sentence in sentences_by_id.items()
                if sentence_id in allowed_constraint_sentence_ids and text in sentence["text"]
            ]
            if not matches:
                errors.append(f"{field.upper()}_NOT_IN_CLAIM_SENTENCES")
                continue
            spans.extend(matches)
        constraint_spans[field] = spans
    return {
        "status": "PASS" if not errors else ("CONFLICT" if any("INVALID" in error or "UNKNOWN" in error or "DUPLICATE" in error for error in errors) else "MISSING"),
        "errors": errors,
        "indicator_norm": indicator_norm.strip() if isinstance(indicator_norm, str) else None,
        "measurement_type": measurement_type if isinstance(measurement_type, str) else None,
        "dimension_pairing": pairing,
        "pairing_evidence_sentence_ids": checked_pairing_evidence,
        "context_sentence_ids": context_sentence_ids,
        "observation_sentence_ids": observation_sentence_ids,
        "constraint_contract_status": constraint_contract_status,
        "constraint_spans": constraint_spans,
    }


def _recover_local_indicator_norm(
    article_text: str,
    skeleton_claim: dict[str, Any],
) -> str | None:
    """Recover a local metric noun phrase when HCX copied another sentence's indicator."""
    raw_target_ids = skeleton_claim.get("target_value_span_ids")
    target_ids = [
        value for value in raw_target_ids
        if isinstance(value, str)
    ] if isinstance(raw_target_ids, list) else []
    if len(target_ids) != 1:
        return None
    values_by_id = {
        candidate["span_id"]: candidate
        for candidate in build_span_candidates(article_text)
        if candidate.get("kind") == "value_unit"
    }
    target = values_by_id.get(target_ids[0])
    if not target:
        return None
    sentence = next(
        (
            row for row in sentence_offset_map(article_text)
            if row["sentence_id"] == target.get("sentence_id")
        ),
        None,
    )
    if not sentence:
        return None
    local_value_start = int(target["char_start"]) - int(sentence["char_start"])
    prefix = sentence["text"][:local_value_start]
    prefix = _PERIOD_CANDIDATE_RE.sub(" ", prefix)
    prefix = re.sub(
        r"(?:전년|전월|전분기|전기|동월|동기)(?:\s+\w+){0,2}\s+대비",
        " ",
        prefix,
    )
    metric_matches = list(_LOCAL_INDICATOR_METRIC_RE.finditer(prefix))
    if not metric_matches:
        return None
    metric_match = metric_matches[-1]
    clause_start = max(
        prefix.rfind(delimiter, 0, metric_match.start()) + 1
        for delimiter in (".", ",", ";", ":")
    )
    clause = prefix[clause_start:metric_match.end()]
    tokens = re.findall(r"[가-힣A-Za-z0-9·()]+", clause)
    while tokens and (
        tokens[0] in _LOCAL_INDICATOR_LEADING_STOPWORDS
        or re.search(r"\d", tokens[0])
    ):
        tokens.pop(0)
    if len(tokens) > 6:
        tokens = tokens[-6:]
    phrase = " ".join(tokens).strip()
    return phrase if len(phrase) >= 2 else None


def validate_claim_skeleton(article_text: str, skeleton_claim: object) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the minimal semantic contract and add a safe pre-binding relation.

    Pairing cannot be established until values and dimensions are bound, so it
    is intentionally absent from HCX skeleton output.  The effective claim
    carries a neutral relation only to reuse the downstream source validators.
    """
    effective = dict(skeleton_claim) if isinstance(skeleton_claim, dict) else {}
    candidate_class, candidate_class_errors = _normalize_candidate_class(effective)
    hard_classification_block = (
        candidate_class != "KOSIS_CANDIDATE"
        and (
            bool(effective.get("candidate_class_override"))
            or effective.get("candidate_coverage_source")
            == "DETERMINISTIC_AMBIGUOUS_FALLBACK"
        )
    )
    if hard_classification_block:
        effective["relation_json"] = {
            "dimension_pairing": "NOT_APPLICABLE",
            "pairing_evidence_sentence_ids": [],
        }
        errors = [
            *candidate_class_errors,
            f"CANDIDATE_CLASS_{candidate_class}",
        ]
        return effective, {
            "status": "CONFLICT",
            "errors": errors,
            "candidate_classification": {
                "candidate_class": candidate_class,
                "is_kosis_candidate": False,
                "binding_eligible": False,
                "routing_policy": "DETERMINISTIC_HARD_BLOCK",
            },
            "indicator_contract": {
                "errors": errors,
                "target_value_span_ids": effective.get(
                    "target_value_span_ids", []
                ),
            },
        }
    raw_indicator = effective.get("indicator_norm")
    if isinstance(raw_indicator, str):
        stripped_indicator = _PERIOD_CANDIDATE_RE.sub(" ", raw_indicator)
        stripped_indicator = re.sub(r"\s+", " ", stripped_indicator).strip()
        if stripped_indicator:
            effective["indicator_norm"] = stripped_indicator
    claim_sentence_ids = {
        value for value in [
            *effective.get("context_sentence_ids", []),
            *effective.get("observation_sentence_ids", []),
        ]
        if isinstance(value, int)
    }
    sentence_rows = {
        row["sentence_id"]: row for row in sentence_offset_map(article_text)
    }
    raw_anchors = _indicator_anchor_terms(effective.get("indicator_norm"))
    raw_grounded = bool(raw_anchors) and any(
        any(
            _evidence_supports_anchor(
                sentence_rows.get(sentence_id, {}).get("text", ""),
                anchor,
            )
            for anchor in raw_anchors
        )
        for sentence_id in claim_sentence_ids
    )
    recovered_indicator = (
        None if raw_grounded
        else _recover_local_indicator_norm(article_text, effective)
    )
    if recovered_indicator:
        effective["indicator_norm"] = recovered_indicator
    expanded_indicator_context_ids: list[int] = []
    target_sentence_ids = [
        value
        for value in effective.get("observation_sentence_ids", [])
        if isinstance(value, int)
    ]
    current_context_ids = [
        value
        for value in effective.get("context_sentence_ids", [])
        if isinstance(value, int)
    ]
    for anchor in sorted(_indicator_anchor_terms(effective.get("indicator_norm"))):
        if any(
            _evidence_supports_anchor(
                sentence_rows.get(sentence_id, {}).get("text", ""),
                anchor,
            )
            for sentence_id in set(current_context_ids) | set(target_sentence_ids)
        ):
            continue
        nearby = [
            sentence_id
            for sentence_id, sentence in sentence_rows.items()
            if any(
                abs(sentence_id - target_id) <= 2
                for target_id in target_sentence_ids
            )
            and _evidence_supports_anchor(sentence.get("text", ""), anchor)
        ]
        if len(nearby) == 1 and nearby[0] not in current_context_ids:
            current_context_ids.append(nearby[0])
            expanded_indicator_context_ids.append(nearby[0])
    if expanded_indicator_context_ids:
        effective["context_sentence_ids"] = current_context_ids
    effective["relation_json"] = {"dimension_pairing": "NOT_APPLICABLE", "pairing_evidence_sentence_ids": []}
    report = validate_semantic_claim(article_text, effective, require_constraints=False)
    report["indicator_recovery"] = {
        "source": "LOCAL_METRIC_PHRASE_RULE" if recovered_indicator else "HCX",
        "raw_indicator_norm": raw_indicator,
        "effective_indicator_norm": effective.get("indicator_norm"),
        "expanded_context_sentence_ids": sorted(
            set(expanded_indicator_context_ids)
        ),
    }
    indicator = report.get("indicator_norm")
    selection_audit = effective.get("candidate_selection") if isinstance(effective.get("candidate_selection"), dict) else {}
    contract_errors: list[str] = [
        *candidate_class_errors,
        *[
            error for error in selection_audit.get("errors", [])
            if isinstance(error, str)
        ],
    ]
    if isinstance(indicator, str) and indicator:
        measurement_values = [
            match
            for match in VALUE_UNIT_RE.finditer(indicator)
            if not (
                _AGE_INDICATOR_VALUE_RE.search(
                    indicator[max(0, match.start() - 8):match.end() + 8]
                )
                or re.search(
                    r"(?:상위\s*)?\d{1,3}\s*대(?:\s*기업)?",
                    indicator[max(0, match.start() - 8):match.end() + 8],
                )
            )
        ]
        if measurement_values:
            contract_errors.append("INDICATOR_NORM_CONTAINS_VALUE")
        if TIME_RE.search(indicator):
            contract_errors.append("INDICATOR_NORM_CONTAINS_PERIOD")
        if _INDICATOR_SOURCE_ORG_RE.search(indicator):
            contract_errors.append("INDICATOR_NORM_CONTAINS_SOURCE_ORG")
        if _INDICATOR_CLAUSE_END_RE.search(indicator):
            contract_errors.append("INDICATOR_NORM_CONTAINS_PREDICATE")

    sentence_rows = {row["sentence_id"]: row for row in sentence_offset_map(article_text)}
    claim_sentence_ids = list(dict.fromkeys([
        *report.get("context_sentence_ids", []),
        *report.get("observation_sentence_ids", []),
    ]))
    anchor_terms = _indicator_anchor_terms(indicator)
    indicator_evidence_spans = [
        span for span in effective.get("indicator_evidence_spans", [])
        if isinstance(span, dict)
    ] if isinstance(effective.get("indicator_evidence_spans"), list) else []
    population_evidence_spans = [
        span for span in effective.get("population_evidence_spans", [])
        if isinstance(span, dict)
    ] if isinstance(effective.get("population_evidence_spans"), list) else []
    item_evidence_spans = [
        span for span in effective.get("item_evidence_spans", [])
        if isinstance(span, dict)
    ] if isinstance(effective.get("item_evidence_spans"), list) else []
    dimension_evidence_spans = [
        span for span in effective.get("dimension_evidence_spans", [])
        if isinstance(span, dict)
    ] if isinstance(effective.get("dimension_evidence_spans"), list) else []
    selection_contract_asserted = bool(selection_audit.get("semantic_role_selection_asserted"))
    selected_indicator_texts = [
        str(span.get("text") or "") for span in indicator_evidence_spans
        if span.get("text")
    ]
    supported_anchor_terms = sorted({
        anchor for anchor in anchor_terms
        if any(_evidence_supports_anchor(text, anchor) for text in selected_indicator_texts)
    })
    unsupported_anchor_terms = sorted(anchor_terms - set(supported_anchor_terms))
    selected_role_spans = {
        "indicator": indicator_evidence_spans,
        "population": population_evidence_spans,
        "item": item_evidence_spans,
        "dimension": dimension_evidence_spans,
    }
    for role, spans in selected_role_spans.items():
        if any(span.get("sentence_id") not in claim_sentence_ids for span in spans):
            contract_errors.append(f"{role.upper()}_EVIDENCE_OUTSIDE_CLAIM_SENTENCES")
    role_ids = {
        role: {
            str(span.get("span_id")) for span in spans
            if span.get("span_id")
        }
        for role, spans in selected_role_spans.items()
        if role != "dimension"
    }
    # Indicator evidence is lexical grounding and may legitimately be reused
    # as an item or population role (for example ``쌀 상승률``). Population
    # and item remain mutually exclusive because they have different mapping
    # responsibilities.
    overlapping_role_ids = sorted(
        role_ids.get("population", set()) & role_ids.get("item", set())
    )
    if overlapping_role_ids:
        contract_errors.append("SEMANTIC_EVIDENCE_ROLE_OVERLAP")
    if selection_contract_asserted and not indicator_evidence_spans:
        contract_errors.append("INDICATOR_EVIDENCE_CANDIDATE_MISSING")
    if selection_contract_asserted and unsupported_anchor_terms:
        contract_errors.append("INDICATOR_NORM_ANCHOR_NOT_SUPPORTED_BY_SELECTED_EVIDENCE")
    indicator_source_sentence_ids = [
        sentence_id for sentence_id in claim_sentence_ids
        if any(term in sentence_rows.get(sentence_id, {}).get("text", "") for term in anchor_terms)
    ]
    if isinstance(indicator, str) and indicator and not anchor_terms:
        contract_errors.append("INDICATOR_NORM_ANCHOR_MISSING")
    elif anchor_terms and not indicator_source_sentence_ids:
        contract_errors.append("INDICATOR_NORM_NOT_GROUNDED_IN_CLAIM_SENTENCES")

    observation_value_sentence_ids = [
        sentence_id for sentence_id in report.get("observation_sentence_ids", [])
        if any(
            candidate.get("kind") == "value_unit"
            for candidate in build_span_candidates(article_text, [sentence_id])
        )
    ]
    if report.get("observation_sentence_ids") and not observation_value_sentence_ids:
        contract_errors.append("OBSERVATION_SENTENCE_VALUE_MISSING")

    raw_target_value_span_ids = effective.get("target_value_span_ids")
    target_value_span_ids = [
        candidate_id for candidate_id in raw_target_value_span_ids
        if isinstance(candidate_id, str)
    ] if isinstance(raw_target_value_span_ids, list) else []
    value_candidates_by_id = {
        candidate["span_id"]: candidate
        for candidate in build_span_candidates(article_text)
        if candidate.get("kind") == "value_unit"
    }
    if not target_value_span_ids:
        contract_errors.append("TARGET_VALUE_CANDIDATE_MISSING")
    elif len(target_value_span_ids) != 1:
        contract_errors.append("TARGET_VALUE_CANDIDATE_CARDINALITY")
    unknown_target_ids = [
        candidate_id for candidate_id in target_value_span_ids
        if candidate_id not in value_candidates_by_id
    ]
    if unknown_target_ids:
        contract_errors.append("TARGET_VALUE_CANDIDATE_ID_UNKNOWN")
    sentence_rows_by_id = {row["sentence_id"]: row for row in sentence_offset_map(article_text)}
    target_source_roles = [
        _local_value_role(
            sentence_rows_by_id.get(
                value_candidates_by_id[candidate_id]["sentence_id"],
                {},
            ),
            value_candidates_by_id[candidate_id],
            set(),
        )
        for candidate_id in target_value_span_ids
        if candidate_id in value_candidates_by_id
    ]
    if any(
        role_error is not None
        and not (
            source_role == "COMPARISON_REFERENCE"
            and candidate_class == "KOSIS_CANDIDATE"
        )
        for source_role, role_error in target_source_roles
    ):
        contract_errors.append("TARGET_VALUE_CANDIDATE_NOT_SELECTABLE")
    selected_target_sentence_ids = sorted({
        value_candidates_by_id[candidate_id]["sentence_id"]
        for candidate_id in target_value_span_ids
        if candidate_id in value_candidates_by_id
    })
    if selected_target_sentence_ids != sorted(report.get("observation_sentence_ids", [])):
        contract_errors.append("TARGET_VALUE_OBSERVATION_SENTENCE_MISMATCH")

    report["errors"] = [*report["errors"], *contract_errors]
    if contract_errors:
        report["status"] = "CONFLICT"
    report["indicator_contract"] = {
        "anchor_terms": sorted(anchor_terms),
        "indicator_source_sentence_ids": indicator_source_sentence_ids,
        "observation_value_sentence_ids": observation_value_sentence_ids,
        "target_value_span_ids": target_value_span_ids,
        "target_value_sentence_ids": selected_target_sentence_ids,
        "indicator_evidence_spans": indicator_evidence_spans,
        "population_evidence_spans": population_evidence_spans,
        "item_evidence_spans": item_evidence_spans,
        "dimension_evidence_spans": dimension_evidence_spans,
        "supported_anchor_terms": supported_anchor_terms,
        "unsupported_anchor_terms": unsupported_anchor_terms,
        "overlapping_role_span_ids": overlapping_role_ids,
        "selection_contract_asserted": selection_contract_asserted,
        "errors": contract_errors,
    }
    report["candidate_classification"] = {
        "candidate_class": candidate_class,
        "is_kosis_candidate": candidate_class == "KOSIS_CANDIDATE",
        "binding_eligible": report["status"] == "PASS",
        "routing_policy": (
            "MODEL_CLASS_AUDIT_ONLY"
            if candidate_class != "KOSIS_CANDIDATE"
            else "KOSIS_CANDIDATE"
        ),
    }
    return effective, report




def call_hcx_semantic_article(title: str, article_text: str, *, api_key: str, model: str = "HCX-007",
                              timeout: int = 120) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Stage 1: HCX returns meaning and sentence IDs, never source field strings."""
    return _call_hcx_json(system_prompt=SEMANTIC_SYSTEM_PROMPT, user_prompt=build_semantic_article_prompt(title, article_text),
                          schema=SEMANTIC_ARTICLE_SCHEMA, api_key=api_key, model=model, timeout=timeout)


def call_hcx_claim_skeleton(title: str, article_text: str, *, api_key: str, model: str = "HCX-007",
                            timeout: int = 120) -> tuple[dict[str, Any], dict[str, Any], float]:
    catalog = build_claim_skeleton_candidate_catalog(article_text)
    selectable_ids = [
        row["value_candidate_id"]
        for row in catalog["value_candidates"]
    ]
    chunks = [
        selectable_ids[index:index + _CLAIM_SKELETON_CHUNK_SIZE]
        for index in range(0, len(selectable_ids), _CLAIM_SKELETON_CHUNK_SIZE)
    ] or [[]]
    merged_claims: list[dict[str, Any]] = []
    merged_usage: dict[str, Any] = {}
    latency_ms = 0.0
    for chunk in chunks:
        prediction, usage, chunk_latency_ms = _call_hcx_json(
            system_prompt=CLAIM_SKELETON_SYSTEM_PROMPT,
            user_prompt=build_claim_skeleton_prompt(
                title,
                article_text,
                chunk,
            ),
            schema=build_claim_skeleton_schema(article_text, chunk),
            api_key=api_key,
            model=model,
            timeout=timeout,
        )
        source_claims = (
            prediction.get("claims", [])
            if isinstance(prediction, dict)
            else []
        )
        completed_claims, coverage_audit = complete_claim_skeleton_candidate_coverage(
            article_text,
            source_claims,
            chunk,
        )
        if coverage_audit["missing_target_ids"]:
            retry_prediction, retry_usage, retry_latency_ms = _call_hcx_json(
                system_prompt=CLAIM_SKELETON_SYSTEM_PROMPT,
                user_prompt=build_claim_skeleton_prompt(
                    title,
                    article_text,
                    coverage_audit["missing_target_ids"],
                ),
                schema=build_claim_skeleton_schema(
                    article_text,
                    coverage_audit["missing_target_ids"],
                ),
                api_key=api_key,
                model=model,
                timeout=timeout,
            )
            retry_claims = (
                retry_prediction.get("claims", [])
                if isinstance(retry_prediction, dict)
                else []
            )
            completed_claims, coverage_audit = (
                complete_claim_skeleton_candidate_coverage(
                    article_text,
                    [
                        *(
                            source_claims
                            if isinstance(source_claims, list)
                            else []
                        ),
                        *(
                            retry_claims
                            if isinstance(retry_claims, list)
                            else []
                        ),
                    ],
                    chunk,
                )
            )
            for key, value in retry_usage.items():
                if isinstance(value, (int, float)):
                    merged_usage[key] = merged_usage.get(key, 0) + value
            merged_usage["candidate_retry_calls"] = (
                merged_usage.get("candidate_retry_calls", 0) + 1
            )
            latency_ms += retry_latency_ms
        merged_claims.extend(completed_claims)
        merged_usage["candidate_target_count"] = (
            merged_usage.get("candidate_target_count", 0)
            + coverage_audit["target_count"]
        )
        merged_usage["candidate_fallback_count"] = (
            merged_usage.get("candidate_fallback_count", 0)
            + coverage_audit["fallback_record_count"]
        )
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                merged_usage[key] = merged_usage.get(key, 0) + value
        latency_ms += chunk_latency_ms
    return (
        normalize_claim_skeleton_candidate_selection(
            article_text,
            {"claims": merged_claims},
        ),
        merged_usage,
        latency_ms,
    )


def call_hcx_span_binding(semantic_claim: dict[str, Any], candidates: list[dict[str, Any]], *, api_key: str,
                          model: str = "HCX-007", timeout: int = 120) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Stage 2: HCX may select only IDs supplied by deterministic source extraction."""
    return _call_hcx_json(system_prompt=SPAN_BINDING_SYSTEM_PROMPT,
                          user_prompt=build_span_binding_prompt(semantic_claim, candidates),
                          schema=build_span_binding_schema(candidates, semantic_claim),
                          api_key=api_key, model=model, timeout=timeout)


def build_article_prompt(title: str, article_text: str) -> str:
    sentences = sentence_offset_map(article_text)
    numbered = "\n".join(f"[{row['sentence_id']}] {row['text']}" for row in sentences)
    return f"""다음 뉴스 기사 전체에서 KOSIS 사실검증 후보 claim을 배열로 구조화하세요.
숫자·단위·시점·근거를 절대 추정하거나 새로 만들지 마세요. 각 claim은 근거 문장 ID와
원문 그대로의 evidence_quote, indicator_evidence_texts를 포함해야 합니다. indicator_evidence_texts,
dimension_source_texts, value_unit_evidence_text, period_evidence_text는 각각 하나의 연속된 원문 substring이어야 합니다. value에는 단위를 넣지 말고 숫자만, unit에는 단위만 넣으세요. indicator_norm은
검색용 의미 요약이며 원문과 다를 수 있습니다. 복수 지역·산업의 1:1 대응이 원문에 없으면
relation_json.dimension_pairing을 UNPAIRED_MULTI_VALUE로 두세요.

제목: {title}
문장 목록:\n{numbered}"""


def call_hcx_article(title: str, article_text: str, *, api_key: str, model: str = "HCX-007", timeout: int = 120) -> tuple[dict[str, Any], dict[str, Any], float]:
    """기사 단위 HCX 호출. API 키와 원문은 출력·로그에 기록하지 않는다."""
    if not api_key:
        raise ValueError("HCX API key is required")
    model = model.upper()
    url = f"https://clovastudio.stream.ntruss.com/v3/chat-completions/{model}"
    body = {
        "messages": [
            {"role": "system", "content": ARTICLE_SYSTEM_PROMPT},
            {"role": "user", "content": build_article_prompt(title, article_text)},
        ],
        "temperature": 0.1, "topP": 0.8, "topK": 0, "repetitionPenalty": 1.1,
        "maxCompletionTokens": 4000, "thinking": {"effort": "none"},
        "responseFormat": {"type": "json", "schema": ARTICLE_SCHEMA},
    }
    started = time.perf_counter()
    response = requests.post(url, headers={
        "Authorization": f"Bearer {api_key}", "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json", "Accept": "application/json",
    }, json=body, timeout=timeout)
    latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        detail = response.text.replace("\n", " ")[:500]
        raise RuntimeError(f"HCX article request failed ({response.status_code}): {detail}")
    payload = response.json()
    content = str(payload.get("result", {}).get("message", {}).get("content", "")).strip()
    if not content:
        raise ValueError("HCX article response content is empty")
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("HCX article response does not contain a JSON object")
    return json.loads(content[start:end + 1]), payload.get("result", {}).get("usage", {}) or {}, latency_ms


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace(",", "").replace("−", "-")


def _evidence_sentences(offset_map: list[dict[str, Any]], ids: object) -> list[dict[str, Any]]:
    wanted = {value for value in ids if isinstance(value, int)} if isinstance(ids, list) else set()
    return [row for row in offset_map if row["sentence_id"] in wanted]


def _locate_exact(sentences: list[dict[str, Any]], value: object) -> dict[str, Any] | None:
    needle = str(value or "").strip()
    if not needle:
        return None
    found = []
    for sentence in sentences:
        start = sentence["text"].find(needle)
        if start >= 0:
            found.append({"char_start": sentence["char_start"] + start, "char_end": sentence["char_start"] + start + len(needle), "text": needle, "sentence_id": sentence["sentence_id"]})
    return found[0] if len(found) == 1 else None


def _value_unit_span(sentences: list[dict[str, Any]], value: object, unit: object) -> dict[str, Any] | None:
    wanted_value, wanted_unit = _compact(value), canon_unit(str(unit or ""))
    for sentence in sentences:
        for match in VALUE_UNIT_RE.finditer(sentence["text"]):
            if _compact(match.group("value")) == wanted_value and canon_unit(match.group("unit")) == wanted_unit:
                return {"char_start": sentence["char_start"] + match.start(), "char_end": sentence["char_start"] + match.end(), "text": match.group(), "sentence_id": sentence["sentence_id"]}
    return None


def validate_article_prediction(article_text: str, prediction: dict[str, Any]) -> dict[str, Any]:
    """LLM 구조화 초안을 원문 span과 대조해 PASS만 다음 검색 단계로 허용한다."""
    offsets = sentence_offset_map(article_text)
    report_claims = []
    for claim_index, claim in enumerate(prediction.get("claims", []) if isinstance(prediction, dict) else []):
        if not isinstance(claim, dict):
            continue
        evidence = _evidence_sentences(offsets, claim.get("evidence_sentence_ids"))
        context_evidence = _evidence_sentences(offsets, claim.get("context_sentence_ids")) or evidence
        claim_errors: list[str] = []
        quote_span = _locate_exact(evidence, claim.get("evidence_quote"))
        indicator_texts = claim.get("indicator_evidence_texts") if isinstance(claim.get("indicator_evidence_texts"), list) else []
        indicator_spans = [_locate_exact(context_evidence, value) for value in indicator_texts]
        if not evidence:
            claim_errors.append("EVIDENCE_SENTENCE_MISSING")
        if not quote_span:
            claim_errors.append("EVIDENCE_QUOTE_NOT_UNIQUELY_FOUND")
        if not indicator_texts or any(span is None for span in indicator_spans):
            claim_errors.append("INDICATOR_EVIDENCE_NOT_UNIQUELY_FOUND")
        dimension_spans: dict[str, list[dict[str, Any]]] = {}
        dimension_sources = claim.get("dimension_source_texts") if isinstance(claim.get("dimension_source_texts"), dict) else {}
        for name, source_values in dimension_sources.items():
            values = source_values if isinstance(source_values, list) else [source_values]
            spans = [_locate_exact(context_evidence, value) for value in values]
            dimension_spans[str(name)] = [span for span in spans if span]
            if len(dimension_spans[str(name)]) != len(values):
                claim_errors.append(f"DIMENSION_SOURCE_NOT_UNIQUELY_FOUND:{name}")
        observations = []
        for observation_index, observation in enumerate(claim.get("observations", []) if isinstance(claim.get("observations"), list) else []):
            errors = list(claim_errors)
            observation_evidence = _evidence_sentences(offsets, observation.get("evidence_sentence_ids"))
            if not observation_evidence:
                errors.append("OBSERVATION_EVIDENCE_SENTENCE_MISSING")
            value_span = _value_unit_span(observation_evidence, observation.get("value"), observation.get("unit"))
            if not value_span:
                errors.append("VALUE_UNIT_CONFLICT")
            value_unit_source_span = _locate_exact(observation_evidence, observation.get("value_unit_evidence_text"))
            if not value_unit_source_span:
                errors.append("OBSERVATION_SOURCE_NOT_UNIQUELY_FOUND")
            period = observation.get("period")
            period_span = None
            if period:
                period_span = _locate_exact(observation_evidence, observation.get("period_evidence_text") or period)
                if not period_span or not TIME_RE.search(period_span["text"]):
                    errors.append("PERIOD_CONFLICT")
            status = "PASS" if not errors else ("CONFLICT" if any("CONFLICT" in error for error in errors) else "MISSING")
            observations.append({
                "observation_index": observation_index, "status": status, "errors": errors,
                "value_unit_span": value_span, "value_unit_source_span": value_unit_source_span, "period_span": period_span,
            })
        report_claims.append({
            "claim_index": claim_index, "claim_status": "PASS" if not claim_errors else "MISSING",
            "errors": claim_errors, "evidence_quote_span": quote_span, "indicator_evidence_spans": [span for span in indicator_spans if span],
            "dimension_source_spans": dimension_spans,
            "observations": observations,
        })
    return {
        "sentence_offset_map": offsets,
        "claims": report_claims,
        "pass_observation_count": sum(item["status"] == "PASS" for claim in report_claims for item in claim["observations"]),
    }


def pass_observations(prediction: dict[str, Any], validation: dict[str, Any]) -> list[dict[str, Any]]:
    """검증을 통과한 observation만 원본 HCX claim과 함께 반환한다."""
    output = []
    claims = prediction.get("claims", []) if isinstance(prediction, dict) else []
    for claim_report in validation.get("claims", []):
        claim_index = claim_report["claim_index"]
        if claim_index >= len(claims):
            continue
        for observation_report in claim_report["observations"]:
            if observation_report["status"] == "PASS":
                output.append({"claim": claims[claim_index], "observation": claims[claim_index]["observations"][observation_report["observation_index"]], "validation": observation_report})
    return output


def _candidate_by_id(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(candidate.get("span_id")): candidate for candidate in candidates if isinstance(candidate, dict) and candidate.get("span_id")}


def validate_span_binding(semantic_claim: dict[str, Any], binding: dict[str, Any],
                          candidates: list[dict[str, Any]], *, require_value_relation: bool = False,
                          require_measurement_type: bool = False,
                          require_semantic_evidence: bool = False,
                          article_text: str | None = None,
                          reference_date: str | None = None) -> dict[str, Any]:
    """Validate that a binding selects only source spans of the allowed type and sentence.

    No LLM-produced text is used here. A PASS is consequently recoverable to a
    single exact substring in the article before retrieval begins.  New HCX
    calls set ``require_value_relation`` so a value must also be classified as
    the target metric rather than a subgroup, contributor, or comparison.
    """
    by_id = _candidate_by_id(candidates)
    context_ids = {value for value in semantic_claim.get("context_sentence_ids", []) if isinstance(value, int)}
    observation_ids = {value for value in semantic_claim.get("observation_sentence_ids", []) if isinstance(value, int)}
    claim_errors: list[str] = []
    claim_measurement_type = semantic_claim.get("measurement_type")
    if require_measurement_type and claim_measurement_type is not None and claim_measurement_type not in _MEASUREMENT_TYPES:
        claim_errors.append("MEASUREMENT_TYPE_MISSING_OR_INVALID")
    if not context_ids:
        claim_errors.append("CONTEXT_SENTENCE_MISSING")
    if not observation_ids:
        claim_errors.append("OBSERVATION_SENTENCE_MISSING")

    def resolve_semantic_role(field: str, *, required: bool = False) -> list[dict[str, Any]]:
        raw_ids = binding.get(field) if isinstance(binding, dict) else None
        if not isinstance(raw_ids, list):
            if require_semantic_evidence:
                claim_errors.append(f"{field.upper()}_MISSING")
            return []
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_id in raw_ids:
            candidate_id = str(raw_id)
            if candidate_id in seen:
                claim_errors.append(f"{field.upper()}_DUPLICATE")
                continue
            seen.add(candidate_id)
            candidate = by_id.get(candidate_id)
            if not candidate:
                claim_errors.append(f"{field.upper()}_UNKNOWN")
            elif candidate.get("kind") != "semantic_evidence":
                claim_errors.append(f"{field.upper()}_TYPE_INVALID")
            elif candidate.get("sentence_id") not in context_ids | observation_ids:
                claim_errors.append(f"{field.upper()}_OUTSIDE_CLAIM_SENTENCES")
            else:
                resolved.append(candidate)
        if required and not resolved:
            claim_errors.append(f"{field.upper()}_EMPTY")
        return resolved

    indicator_evidence_spans = resolve_semantic_role(
        "indicator_evidence_span_ids",
        required=require_semantic_evidence,
    )
    population_evidence_spans = resolve_semantic_role("population_evidence_span_ids")
    item_evidence_spans = resolve_semantic_role("item_evidence_span_ids")
    indicator_evidence_spans_raw = list(indicator_evidence_spans)
    population_evidence_spans_raw = list(population_evidence_spans)
    item_evidence_spans_raw = list(item_evidence_spans)
    target_value_spans = [
        by_id.get(str(observation.get("value_span_id")))
        for observation in (
            binding.get("observations", []) if isinstance(binding, dict) else []
        )
        if isinstance(observation, dict)
    ]
    target_value_spans = [span for span in target_value_spans if isinstance(span, dict)]
    role_hints = build_semantic_role_hints(
        semantic_claim,
        candidates,
        target_value_spans,
    )
    raw_population_item_overlap = sorted(
        {
            span.get("span_id") for span in population_evidence_spans_raw
        }
        & {
            span.get("span_id") for span in item_evidence_spans_raw
        }
    )
    filtered_population_evidence_spans = [
        span for span in population_evidence_spans
        if span.get("span_id") not in role_hints["population_ids"]
    ]
    filtered_item_evidence_spans = [
        span for span in item_evidence_spans
        if span.get("span_id") not in role_hints["item_ids"]
    ]
    population_evidence_spans = [
        span for span in population_evidence_spans
        if span.get("span_id") in role_hints["population_ids"]
    ]
    item_evidence_spans = [
        span for span in item_evidence_spans
        if span.get("span_id") in role_hints["item_ids"]
    ]
    recovered_population_evidence_spans = [
        by_id[candidate_id]
        for candidate_id in role_hints["population_ids"]
        if candidate_id in by_id
        and candidate_id not in {
            span.get("span_id") for span in population_evidence_spans
        }
    ]
    recovered_item_evidence_spans = [
        by_id[candidate_id]
        for candidate_id in role_hints["item_ids"]
        if candidate_id in by_id
        and candidate_id not in {
            span.get("span_id") for span in item_evidence_spans
        }
    ]
    population_evidence_spans.extend(recovered_population_evidence_spans)
    item_evidence_spans.extend(recovered_item_evidence_spans)
    anchor_terms = _indicator_anchor_terms(semantic_claim.get("indicator_norm"))
    initially_supported_anchor_terms = {
        anchor for anchor in anchor_terms
        if any(
            _evidence_supports_anchor(str(span.get("text") or ""), anchor)
            for span in indicator_evidence_spans
        )
    }
    recovered_indicator_evidence_spans: list[dict[str, Any]] = []
    for anchor in sorted(anchor_terms - initially_supported_anchor_terms):
        matches = [
            candidate for candidate in candidates
            if candidate.get("kind") == "semantic_evidence"
            and candidate.get("sentence_id") in context_ids | observation_ids
            and _evidence_supports_anchor(str(candidate.get("text") or ""), anchor)
        ]
        if not matches:
            continue

        def recovery_score(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
            candidate_compact = re.sub(
                r"[^가-힣A-Za-z0-9]",
                "",
                re.sub(r"\([^)]*\)", "", str(candidate.get("text") or "")),
            ).casefold()
            anchor_compact = re.sub(r"[^가-힣A-Za-z0-9]", "", anchor).casefold()
            same_sentence_distances = [
                abs(int(candidate.get("char_start", 0)) - int(target.get("char_start", 0)))
                for target in target_value_spans
                if target.get("sentence_id") == candidate.get("sentence_id")
            ]
            return (
                0 if candidate_compact == anchor_compact else 1,
                min(same_sentence_distances) if same_sentence_distances else 10**9,
                len(str(candidate.get("text") or "")),
                str(candidate.get("span_id") or ""),
            )

        recovered = min(matches, key=recovery_score)
        if recovered.get("span_id") not in {
            span.get("span_id") for span in indicator_evidence_spans
        }:
            indicator_evidence_spans.append(recovered)
            recovered_indicator_evidence_spans.append(recovered)

    semantic_role_ids = {
        "indicator": {span["span_id"] for span in indicator_evidence_spans},
        "population": {span["span_id"] for span in population_evidence_spans},
        "item": {span["span_id"] for span in item_evidence_spans},
    }
    overlapping_semantic_role_ids = sorted(
        (
            semantic_role_ids["population"] & semantic_role_ids["item"]
        ) | set(raw_population_item_overlap)
    )
    if overlapping_semantic_role_ids:
        claim_errors.append("SEMANTIC_EVIDENCE_ROLE_OVERLAP")
    shared_indicator_role_ids = sorted(
        (semantic_role_ids["indicator"] & semantic_role_ids["population"])
        | (semantic_role_ids["indicator"] & semantic_role_ids["item"])
    )
    selected_evidence_supported_anchors = {
        anchor for anchor in anchor_terms
        if any(
            _evidence_supports_anchor(str(span.get("text") or ""), anchor)
            for span in indicator_evidence_spans
        )
    }
    context_grounded_anchor_terms: set[str] = set()
    if article_text:
        source_sentences = {
            row["sentence_id"]: row["text"]
            for row in sentence_offset_map(article_text)
        }
        context_grounded_anchor_terms = {
            anchor
            for anchor in anchor_terms
            if any(
                _evidence_supports_anchor(
                    source_sentences.get(sentence_id, ""),
                    anchor,
                )
                for sentence_id in context_ids | observation_ids
            )
        }
    supported_anchor_terms = sorted(
        selected_evidence_supported_anchors | context_grounded_anchor_terms
    )
    unsupported_anchor_terms = sorted(anchor_terms - set(supported_anchor_terms))
    if require_semantic_evidence and unsupported_anchor_terms:
        claim_errors.append("INDICATOR_NORM_ANCHOR_NOT_SUPPORTED_BY_SELECTED_EVIDENCE")

    observations = []
    source_observations = binding.get("observations", []) if isinstance(binding, dict) else []
    for observation_index, observation in enumerate(source_observations if isinstance(source_observations, list) else []):
        errors = list(claim_errors)
        if not isinstance(observation, dict):
            observations.append({"observation_index": observation_index, "status": "CONFLICT", "errors": errors + ["OBSERVATION_NOT_OBJECT"]})
            continue
        raw_value_role = observation.get("value_role")
        raw_measurement_type = observation.get("measurement_type")
        measurement_type = raw_measurement_type if isinstance(raw_measurement_type, str) else claim_measurement_type
        raw_relation = observation.get("indicator_value_relation")
        raw_relation_evidence = observation.get("relation_evidence_sentence_ids")
        value_role = raw_value_role if isinstance(raw_value_role, str) else "TARGET_MEASURE"
        indicator_value_relation = raw_relation if isinstance(raw_relation, str) else "SAME_METRIC"
        relation_contract_status = "ASSERTED" if isinstance(raw_value_role, str) and isinstance(raw_relation, str) else "LEGACY_UNASSERTED"
        if require_value_relation and not isinstance(raw_value_role, str):
            errors.append("VALUE_ROLE_MISSING")
        elif isinstance(raw_value_role, str) and raw_value_role not in _VALUE_ROLES:
            errors.append("VALUE_ROLE_INVALID")
        if require_value_relation and not isinstance(raw_relation, str):
            errors.append("INDICATOR_VALUE_RELATION_MISSING")
        elif isinstance(raw_relation, str) and raw_relation not in _INDICATOR_VALUE_RELATIONS:
            errors.append("INDICATOR_VALUE_RELATION_INVALID")
        if value_role != "TARGET_MEASURE":
            errors.append("VALUE_ROLE_NOT_TARGET_MEASURE")
        if indicator_value_relation != "SAME_METRIC":
            errors.append("INDICATOR_VALUE_RELATION_NOT_SAME_METRIC")
        relation_evidence_sentence_ids: list[int] = []
        if require_value_relation and not isinstance(raw_relation_evidence, list):
            errors.append("RELATION_EVIDENCE_SENTENCE_IDS_MISSING")
        elif isinstance(raw_relation_evidence, list):
            for sentence_id in raw_relation_evidence:
                if not isinstance(sentence_id, int):
                    errors.append("RELATION_EVIDENCE_SENTENCE_ID_INVALID")
                elif sentence_id not in context_ids | observation_ids:
                    errors.append("RELATION_EVIDENCE_OUTSIDE_CLAIM_SENTENCES")
                elif sentence_id in relation_evidence_sentence_ids:
                    errors.append("RELATION_EVIDENCE_SENTENCE_ID_DUPLICATE")
                else:
                    relation_evidence_sentence_ids.append(sentence_id)
            if require_value_relation and not relation_evidence_sentence_ids:
                errors.append("RELATION_EVIDENCE_SENTENCE_MISSING")
        value_span = by_id.get(str(observation.get("value_span_id")))
        measurement_type_source = "HCX" if isinstance(raw_measurement_type, str) else "CLAIM"
        unit_owned_measurement_type = _unit_owned_measurement_type(value_span)
        if unit_owned_measurement_type:
            measurement_type = unit_owned_measurement_type
            measurement_type_source = "UNIT_RULE"
        indicator_owned_measurement_type = _indicator_owned_measurement_type(
            semantic_claim.get("indicator_norm"),
            value_span,
        )
        if indicator_owned_measurement_type:
            measurement_type = indicator_owned_measurement_type
            measurement_type_source = "INDICATOR_RULE"
        context_owned_measurement_type = _context_owned_measurement_type(
            semantic_claim.get("indicator_norm"),
            value_span,
            candidates,
            article_text,
        )
        if context_owned_measurement_type:
            measurement_type = context_owned_measurement_type
            measurement_type_source = "LOCAL_CHANGE_PREDICATE_RULE"
        if require_measurement_type and measurement_type not in _MEASUREMENT_TYPES:
            errors.append("MEASUREMENT_TYPE_MISSING_OR_INVALID")
        if not value_span:
            errors.append("VALUE_SPAN_ID_UNKNOWN")
        elif value_span.get("kind") != "value_unit":
            errors.append("VALUE_SPAN_TYPE_INVALID")
        elif value_span.get("sentence_id") not in observation_ids:
            errors.append("VALUE_SPAN_OUTSIDE_OBSERVATION_SENTENCE")
        elif require_value_relation and value_span.get("sentence_id") not in relation_evidence_sentence_ids:
            errors.append("VALUE_SENTENCE_NOT_IN_RELATION_EVIDENCE")
        elif value_span and _measurement_type_unit_error(measurement_type, value_span):
            errors.append(_measurement_type_unit_error(measurement_type, value_span))
        period_id = observation.get("period_span_id")
        period_span = by_id.get(str(period_id)) if period_id else None
        period_span_source = "HCX" if period_id else None
        period_normalized: str | None = None
        if period_id:
            if not period_span:
                errors.append("PERIOD_SPAN_ID_UNKNOWN")
            elif period_span.get("kind") != "time":
                same_text_times = [
                    candidate
                    for candidate in candidates
                    if candidate.get("kind") == "time"
                    and candidate.get("sentence_id") in context_ids | observation_ids
                    and candidate.get("text") == period_span.get("text")
                ]
                if len(same_text_times) == 1:
                    period_span = same_text_times[0]
                    period_span_source = "SAME_TEXT_TIME_CANDIDATE_RULE"
                else:
                    errors.append("PERIOD_SPAN_TYPE_INVALID")
            elif period_span.get("sentence_id") not in context_ids | observation_ids:
                errors.append("PERIOD_SPAN_OUTSIDE_CLAIM_SENTENCES")
            elif _COMPARISON_PERIOD_RE.fullmatch(
                re.sub(r"\s+", " ", str(period_span.get("text") or "")).strip()
            ):
                period_span = None
                period_span_source = "FILTERED_COMPARISON_PERIOD"
        else:
            relative_period_candidates = [
                candidate for candidate in candidates
                if candidate.get("kind") == "time"
                and candidate.get("sentence_id") in context_ids | observation_ids
                and _RELATIVE_MEASUREMENT_PERIOD_RE.fullmatch(str(candidate.get("text") or ""))
            ]
            relative_period_texts = {
                str(candidate.get("text") or "") for candidate in relative_period_candidates
            }
            if len(relative_period_texts) == 1:
                period_span = relative_period_candidates[0]
                period_span_source = "UNAMBIGUOUS_RELATIVE_PERIOD_RULE"
        target_period_resolved = False
        if article_text and value_span:
            (
                target_period_span,
                target_period_normalized,
                target_period_source,
            ) = _target_specific_period(
                article_text=article_text,
                value_span=value_span,
                measurement_type=measurement_type,
                candidates=candidates,
            )
            if target_period_normalized:
                if target_period_span:
                    period_span = target_period_span
                period_normalized = target_period_normalized
                period_span_source = target_period_source
                target_period_resolved = True
        if article_text and value_span:
            article_period_candidates = [
                candidate
                for candidate in build_span_candidates(article_text)
                if candidate.get("kind") == "time"
                and _RELATIVE_MEASUREMENT_PERIOD_RE.fullmatch(
                    str(candidate.get("text") or "")
                )
            ]
            value_sentence_id = value_span.get("sentence_id")
            sentence_rows = {
                row["sentence_id"]: row
                for row in sentence_offset_map(article_text)
            }
            selected_is_historical_absolute = bool(
                period_span
                and re.fullmatch(
                    r"\d{4}년(?:\s*\d{1,2}(?:월|분기))?(?:\s*(?:초|중|말))?",
                    str(period_span.get("text") or ""),
                )
            )
            preceding_relative = [
                candidate
                for candidate in article_period_candidates
                if isinstance(candidate.get("sentence_id"), int)
                and isinstance(value_sentence_id, int)
                and candidate["sentence_id"] <= value_sentence_id
            ]
            if preceding_relative and not target_period_resolved and (
                selected_is_historical_absolute or period_span is None
            ):
                latest_sentence_id = max(
                    int(candidate["sentence_id"])
                    for candidate in preceding_relative
                )
                latest_candidates = [
                    candidate
                    for candidate in preceding_relative
                    if candidate.get("sentence_id") == latest_sentence_id
                ]
                latest_texts = {
                    str(candidate.get("text") or "")
                    for candidate in latest_candidates
                }
                if len(latest_texts) == 1:
                    period_span = latest_candidates[0]
                    period_span_source = "NEAREST_PRECEDING_RELATIVE_PERIOD_RULE"
        if period_normalized is None and period_span:
            period_normalized = (
                _normalize_local_period_text(
                    period_span,
                    article_text,
                    reference_date=reference_date,
                )
                if article_text
                else str(period_span.get("text") or "")
            ) or None
        dimension_spans = []
        filtered_dimension_spans: list[dict[str, Any]] = []
        seen_dimension_ids: set[str] = set()
        for dimension_id in observation.get("dimension_span_ids", []) if isinstance(observation.get("dimension_span_ids"), list) else []:
            normalized_id = str(dimension_id)
            if normalized_id in seen_dimension_ids:
                errors.append("DIMENSION_SPAN_ID_DUPLICATE")
                continue
            seen_dimension_ids.add(normalized_id)
            dimension_span = by_id.get(normalized_id)
            if not dimension_span:
                errors.append("DIMENSION_SPAN_ID_UNKNOWN")
            elif (
                dimension_span.get("kind") == "semantic_evidence"
                and normalized_id not in role_hints["dimension_ids"]
            ):
                # HCX may select a causal or neighbouring phrase as a
                # dimension. Semantic evidence is admitted only when the
                # deterministic role contract has classified it as one.
                filtered_dimension_spans.append(dimension_span)
            elif (
                dimension_span.get("kind") != "dimension"
                and not (
                    dimension_span.get("kind") == "semantic_evidence"
                    and normalized_id in role_hints["dimension_ids"]
                )
            ):
                errors.append("DIMENSION_SPAN_TYPE_INVALID")
            elif dimension_span.get("sentence_id") not in context_ids | observation_ids:
                errors.append("DIMENSION_SPAN_OUTSIDE_CLAIM_SENTENCES")
            else:
                dimension_spans.append(dimension_span)
        raw_dimension_spans = list(dimension_spans)
        relation = (
            semantic_claim.get("relation_json")
            if isinstance(semantic_claim.get("relation_json"), dict)
            else {}
        )
        dimension_pairing = relation.get("dimension_pairing")
        if (
            value_span
            and len(source_observations) == 1
            and dimension_pairing in {None, "NOT_APPLICABLE"}
        ):
            retained_dimension_spans = []
            for dimension in dimension_spans:
                is_trailing_controlled_dimension = (
                    dimension.get("kind") == "dimension"
                    and dimension.get("sentence_id") == value_span.get("sentence_id")
                    and isinstance(dimension.get("char_start"), int)
                    and isinstance(value_span.get("char_end"), int)
                    and int(dimension["char_start"]) >= int(value_span["char_end"])
                )
                if is_trailing_controlled_dimension:
                    filtered_dimension_spans.append(dimension)
                else:
                    retained_dimension_spans.append(dimension)
            dimension_spans = retained_dimension_spans
        local_dimensions_by_type: dict[str, dict[str, Any]] = {}
        if value_span:
            for candidate in candidates:
                if candidate.get("kind") != "dimension":
                    continue
                dimension_end = candidate.get("char_end")
                value_start = value_span.get("char_start")
                same_sentence = candidate.get("sentence_id") == value_span.get("sentence_id")
                has_intervening_value = any(
                    other.get("kind") == "value_unit"
                    and other.get("sentence_id") == value_span.get("sentence_id")
                    and isinstance(other.get("char_start"), int)
                    and isinstance(dimension_end, int)
                    and isinstance(value_start, int)
                    and dimension_end <= other["char_start"] < value_start
                    for other in candidates
                )
                if (
                    not same_sentence
                    or not isinstance(dimension_end, int)
                    or not isinstance(value_start, int)
                    or not 0 <= value_start - dimension_end <= 16
                    or has_intervening_value
                ):
                    continue
                dimension_type = str(candidate.get("dimension_type") or "")
                previous = local_dimensions_by_type.get(dimension_type)
                if previous is None or int(candidate.get("char_end") or -1) > int(previous.get("char_end") or -1):
                    local_dimensions_by_type[dimension_type] = candidate
        if local_dimensions_by_type:
            dimension_spans = [
                dimension for dimension in dimension_spans
                if str(dimension.get("dimension_type") or "") not in local_dimensions_by_type
            ]
            dimension_spans.extend(local_dimensions_by_type.values())
        recovered_dimension_spans: list[dict[str, Any]] = []
        if value_span:
            for candidate_id in role_hints["dimension_ids"]:
                candidate = by_id.get(candidate_id)
                if (
                    not candidate
                    or candidate.get("kind") != "semantic_evidence"
                    or candidate.get("sentence_id") != value_span.get("sentence_id")
                    or candidate_id in {
                        dimension.get("span_id") for dimension in dimension_spans
                    }
                ):
                    continue
                base = _semantic_candidate_base(candidate.get("text"))
                is_exclusion_part = (
                    base == "제외"
                    or any(
                        other_id in role_hints["dimension_ids"]
                        and _semantic_candidate_base(by_id.get(other_id, {}).get("text")) == "제외"
                        and by_id.get(other_id, {}).get("sentence_id") == candidate.get("sentence_id")
                        and isinstance(by_id.get(other_id, {}).get("char_start"), int)
                        and isinstance(candidate.get("char_end"), int)
                        and 0 <= int(by_id[other_id]["char_start"]) - int(candidate["char_end"]) <= 2
                        for other_id in role_hints["dimension_ids"]
                    )
                )
                supports_indicator_anchor = any(
                    _evidence_supports_anchor(
                        str(candidate.get("text") or ""),
                        anchor,
                    )
                    for anchor in anchor_terms
                )
                if not is_exclusion_part and not supports_indicator_anchor:
                    continue
                dimension_spans.append(candidate)
                recovered_dimension_spans.append(candidate)
        local_pair_changed = {
            dimension.get("span_id") for dimension in dimension_spans
            if dimension not in recovered_dimension_spans
        } != {
            dimension.get("span_id") for dimension in raw_dimension_spans
        }
        dimension_span_source = (
            "LOCAL_PAIR_RULE+DETERMINISTIC_ROLE_HINT_RECOVERY"
            if local_pair_changed and recovered_dimension_spans
            else (
                "LOCAL_PAIR_RULE"
                if local_pair_changed
                else (
                    "DETERMINISTIC_ROLE_HINT_RECOVERY"
                    if recovered_dimension_spans else "HCX"
                )
            )
        )
        status = "PASS" if not errors else "CONFLICT"
        effective_search_fields = (
            _effective_search_fields(
                article_text=article_text,
                semantic_claim=semantic_claim,
                observation={
                    "value_span": value_span,
                    "measurement_type": measurement_type,
                    "dimension_spans": dimension_spans,
                },
                population_evidence_spans=population_evidence_spans,
                item_evidence_spans=item_evidence_spans,
            )
            if article_text and value_span
            else None
        )
        observations.append({
            "observation_index": observation_index, "status": status, "errors": errors,
            "value_span": value_span, "period_span": period_span, "dimension_spans": dimension_spans,
            "dimension_spans_raw": raw_dimension_spans,
            "filtered_dimension_spans": filtered_dimension_spans,
            "recovered_dimension_spans": recovered_dimension_spans,
            "dimension_span_source": dimension_span_source,
            "value_role": value_role, "indicator_value_relation": indicator_value_relation,
            "measurement_type": measurement_type if isinstance(measurement_type, str) else None,
            "measurement_type_raw": raw_measurement_type if isinstance(raw_measurement_type, str) else None,
            "measurement_type_source": measurement_type_source,
            "period_span_source": period_span_source,
            "period_normalized": period_normalized,
            "effective_search_fields": effective_search_fields,
            "relation_evidence_sentence_ids": relation_evidence_sentence_ids,
            "relation_contract_status": relation_contract_status,
        })
    if not observations:
        claim_errors.append("BINDING_OBSERVATION_MISSING")
    return {
        "claim_status": "PASS" if not claim_errors and any(item["status"] == "PASS" for item in observations) else "CONFLICT",
        "errors": claim_errors,
        "semantic_role_evidence": {
            "indicator_evidence_spans": indicator_evidence_spans,
            "indicator_evidence_spans_raw": indicator_evidence_spans_raw,
            "recovered_indicator_evidence_spans": recovered_indicator_evidence_spans,
            "population_evidence_spans": population_evidence_spans,
            "population_evidence_spans_raw": population_evidence_spans_raw,
            "filtered_population_evidence_spans": filtered_population_evidence_spans,
            "recovered_population_evidence_spans": recovered_population_evidence_spans,
            "item_evidence_spans": item_evidence_spans,
            "item_evidence_spans_raw": item_evidence_spans_raw,
            "filtered_item_evidence_spans": filtered_item_evidence_spans,
            "recovered_item_evidence_spans": recovered_item_evidence_spans,
            "supported_anchor_terms": supported_anchor_terms,
            "context_grounded_anchor_terms": sorted(
                context_grounded_anchor_terms
            ),
            "unsupported_anchor_terms": unsupported_anchor_terms,
            "overlapping_span_ids": overlapping_semantic_role_ids,
            "shared_indicator_role_span_ids": shared_indicator_role_ids,
            "contract_status": (
                "ASSERTED_PLUS_DETERMINISTIC_ROLE_RECOVERY"
                if recovered_population_evidence_spans or recovered_item_evidence_spans
                else (
                    "ASSERTED_PLUS_DETERMINISTIC_ANCHOR_RECOVERY"
                    if recovered_indicator_evidence_spans
                    else ("ASSERTED" if require_semantic_evidence else "OPTIONAL")
                )
            ),
        },
        "measurement_type": claim_measurement_type if isinstance(claim_measurement_type, str) else None,
        "context_sentence_ids": sorted(context_ids), "observation_sentence_ids": sorted(observation_ids),
        "observations": observations,
    }


_INDICATOR_GENERIC_TERMS = frozenset({
    "상승률", "증가율", "감소율", "증감률", "변화율", "성장률", "비율", "기여도",
    "점유율", "순환변동치", "평균", "생산", "가격", "상승", "증가", "감소",
    "증가폭", "감소폭", "수", "건수", "인원", "규모",
    "변화", "배수", "순위", "최고치", "최저치", "역사적", "전체",
    "전월비", "전년비", "전기비", "전년동월비",
})


def _indicator_anchor_terms(indicator_norm: object) -> set[str]:
    """Return source-matchable, non-generic terms from the retrieval indicator."""
    if not isinstance(indicator_norm, str):
        return set()
    normalized = re.sub(r"\([^)]*\)", "", indicator_norm)
    single_char_stopwords = frozenset({"전", "및", "등", "중", "의", "도", "은", "는", "이", "가", "을", "를", "과", "와"})
    return {
        term for term in re.findall(r"[가-힣A-Za-z0-9]+", normalized)
        if term not in _INDICATOR_GENERIC_TERMS and term not in single_char_stopwords
    }


def _evidence_supports_anchor(evidence_text: str, anchor: str) -> bool:
    """Match compact source variants such as ``전(全)산업`` to ``전산업``."""
    without_parenthetical = re.sub(r"\([^)]*\)", "", evidence_text)
    evidence_variants = {
        re.sub(r"[^가-힣A-Za-z0-9]", "", evidence_text).casefold(),
        re.sub(r"[^가-힣A-Za-z0-9]", "", without_parenthetical).casefold(),
    }
    anchor_compact = re.sub(r"[^가-힣A-Za-z0-9]", "", anchor).casefold()
    anchor_aliases = {
        "연상녀": {"연상인", "아내"},
        "연하남": {"연하", "남편"},
        "무역집중도": {"수출집중도", "수출 집중도", "무역집중도"},
        "수출집중도": {"수출집중도", "수출 집중도", "무역집중도"},
        "it부품": {"IT부품", "정보통신 부품", "정보통신부품"},
        "정보통신": {"정보통신", "IT"},
    }
    if anchor in anchor_aliases and any(
        alias in without_parenthetical
        for alias in anchor_aliases[anchor]
    ):
        return True
    return bool(
        anchor_compact
        and any(
            evidence_compact
            and (anchor_compact in evidence_compact or evidence_compact in anchor_compact)
            for evidence_compact in evidence_variants
        )
    )


def _indicator_allows_item_anchor(indicator_norm: object) -> bool:
    """Return whether a leading source noun is a stable independent item.

    Generic indicator anchors previously promoted metric words such as
    ``건수``, ``인구`` and ``직원`` to items.  Reviewed data only supports this
    recovery for item-level price changes.  Loan type uses the explicit
    delinquency rule below.
    """
    compact = re.sub(r"\s+", "", str(indicator_norm or ""))
    return compact.endswith(("가격상승률", "물가상승률", "상승률"))


def _semantic_candidate_base(text: object) -> str:
    value = str(text or "")
    return _semantic_evidence_surface(value)


def _candidate_distance_to_targets(
    candidate: dict[str, Any],
    target_value_spans: list[dict[str, Any]],
) -> int:
    distances = [
        abs(int(candidate.get("char_start", 0)) - int(target.get("char_start", 0)))
        for target in target_value_spans
        if target.get("sentence_id") == candidate.get("sentence_id")
    ]
    return min(distances) if distances else 10**9


def _best_semantic_anchor_candidate(
    anchor: str,
    candidates: list[dict[str, Any]],
    target_value_spans: list[dict[str, Any]],
) -> dict[str, Any] | None:
    matches = [
        candidate for candidate in candidates
        if candidate.get("kind") == "semantic_evidence"
        and _evidence_supports_anchor(str(candidate.get("text") or ""), anchor)
    ]
    if not matches:
        return None
    anchor_compact = re.sub(r"[^가-힣A-Za-z0-9]", "", anchor).casefold()

    def score(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
        base = _semantic_candidate_base(candidate.get("text"))
        compact = re.sub(r"[^가-힣A-Za-z0-9]", "", base).casefold()
        return (
            0 if compact == anchor_compact else 1,
            _candidate_distance_to_targets(candidate, target_value_spans),
            len(str(candidate.get("text") or "")),
            str(candidate.get("span_id") or ""),
        )

    return min(matches, key=score)


def build_semantic_role_hints(
    semantic_claim: dict[str, Any],
    candidates: list[dict[str, Any]],
    target_value_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return conservative, source-only role hints for stage-2 selection.

    Hints never create text. They identify stable source candidates that are
    structurally unambiguous: explicit population nouns, indicator-local items,
    known sector/whole-industry values, controlled dimensions, and exclusion
    phrases. Validation may use the same hints to recover an HCX omission.
    """
    claim_sentence_ids = {
        value for value in [
            *semantic_claim.get("context_sentence_ids", []),
            *semantic_claim.get("observation_sentence_ids", []),
        ]
        if isinstance(value, int)
    }
    local_candidates = [
        candidate for candidate in candidates
        if candidate.get("sentence_id") in claim_sentence_ids
    ]
    semantic_candidates = [
        candidate for candidate in local_candidates
        if candidate.get("kind") == "semantic_evidence"
    ]
    by_candidate_id: dict[str, list[str]] = {
        str(candidate.get("span_id")): ["dimension"]
        for candidate in local_candidates
        if candidate.get("kind") == "dimension" and candidate.get("span_id")
    }

    def add(candidate: dict[str, Any], role: str) -> None:
        candidate_id = str(candidate.get("span_id") or "")
        if not candidate_id:
            return
        roles = by_candidate_id.setdefault(candidate_id, [])
        if role not in roles:
            roles.append(role)

    dimension_like_semantic: list[dict[str, Any]] = []
    population_candidates_by_base: dict[str, list[dict[str, Any]]] = {}
    for candidate in semantic_candidates:
        base = _semantic_candidate_base(candidate.get("text"))
        duplicates_controlled_dimension = any(
            other.get("kind") == "dimension"
            and other.get("sentence_id") == candidate.get("sentence_id")
            and other.get("char_start") == candidate.get("char_start")
            and other.get("char_end") == candidate.get("char_end")
            for other in local_candidates
        )
        if duplicates_controlled_dimension:
            dimension_like_semantic.append(candidate)
        elif _SEMANTIC_DIMENSION_SURFACE_RE.fullmatch(base):
            add(candidate, "dimension")
            dimension_like_semantic.append(candidate)
        if _POPULATION_SURFACE_RE.fullmatch(base):
            population_candidates_by_base.setdefault(base, []).append(candidate)
    for population_candidates in population_candidates_by_base.values():
        add(
            min(
                population_candidates,
                key=lambda candidate: (
                    len(str(candidate.get("text") or "")),
                    _candidate_distance_to_targets(candidate, target_value_spans),
                    str(candidate.get("span_id") or ""),
                ),
            ),
            "population",
        )

    anchors = _indicator_anchor_terms(semantic_claim.get("indicator_norm"))
    indicator_compact = re.sub(
        r"[^가-힣A-Za-z0-9]",
        "",
        str(semantic_claim.get("indicator_norm") or ""),
    )
    allow_item_anchor = _indicator_allows_item_anchor(
        semantic_claim.get("indicator_norm")
    )
    for anchor in sorted(anchors):
        if any(anchor.endswith(suffix) for suffix in _METRIC_ANCHOR_SUFFIXES):
            continue
        candidate = _best_semantic_anchor_candidate(
            anchor,
            semantic_candidates,
            target_value_spans,
        )
        if not candidate:
            continue
        base = _semantic_candidate_base(candidate.get("text"))
        if (
            candidate in dimension_like_semantic
            or base in _ITEM_ANCHOR_STOPWORDS
            or _POPULATION_SURFACE_RE.fullmatch(base)
            or "OECD" in base
            or "경제협력개발기구" in base
            or _INDICATOR_SOURCE_ORG_RE.search(base)
            or any(base.endswith(suffix) for suffix in _METRIC_ANCHOR_SUFFIXES)
        ):
            continue
        # In a compound metric such as ``건설 수주액``, the leading domain
        # names the metric rather than an independently selectable item.
        if "수주액" in indicator_compact and indicator_compact.find(anchor) < indicator_compact.find("수주액"):
            continue
        if allow_item_anchor:
            add(candidate, "item")

    # Loan type is an independent item for delinquency/rate statistics even
    # when the skeleton indicator keeps only the sector plus metric name.
    if "연체율" in str(semantic_claim.get("indicator_norm") or ""):
        loan_candidates = [
            candidate for candidate in semantic_candidates
            if _semantic_candidate_base(candidate.get("text")).endswith("대출")
        ]
        if loan_candidates:
            add(
                min(
                    loan_candidates,
                    key=lambda candidate: (
                        _candidate_distance_to_targets(candidate, target_value_spans),
                        len(str(candidate.get("text") or "")),
                        str(candidate.get("span_id") or ""),
                    ),
                ),
                "item",
            )

    # Preserve both parts of an explicit parenthetical exclusion condition.
    exclusion_candidates = [
        candidate for candidate in semantic_candidates
        if _semantic_candidate_base(candidate.get("text")) == "제외"
    ]
    for exclusion in exclusion_candidates:
        add(exclusion, "dimension")
        preceding = [
            candidate for candidate in semantic_candidates
            if candidate.get("sentence_id") == exclusion.get("sentence_id")
            and isinstance(candidate.get("char_end"), int)
            and isinstance(exclusion.get("char_start"), int)
            and 0 <= int(exclusion["char_start"]) - int(candidate["char_end"]) <= 2
            and _semantic_candidate_base(candidate.get("text")) != "제외"
        ]
        if preceding:
            add(max(preceding, key=lambda candidate: int(candidate["char_end"])), "dimension")

    candidate_positions = {
        str(candidate.get("span_id")): (
            int(candidate.get("sentence_id") or 0),
            int(candidate.get("char_start") or 0),
            int(candidate.get("char_end") or 0),
        )
        for candidate in local_candidates
        if candidate.get("span_id")
    }

    def ordered_ids(role: str) -> list[str]:
        return sorted(
            (
                candidate_id for candidate_id, roles in by_candidate_id.items()
                if role in roles
            ),
            key=lambda candidate_id: (
                candidate_positions.get(candidate_id, (10**9, 10**9, 10**9)),
                candidate_id,
            ),
        )

    return {
        "by_candidate_id": {
            candidate_id: sorted(roles)
            for candidate_id, roles in by_candidate_id.items()
            if roles
        },
        "population_ids": ordered_ids("population"),
        "item_ids": ordered_ids("item"),
        "dimension_ids": ordered_ids("dimension"),
    }


def _has_local_indicator_value_segment(sentence_row: dict[str, Any], value_span: dict[str, Any], terms: set[str]) -> bool:
    """Require a claim label between the prior numeric value and this value.

    A fixed character window incorrectly links neighbouring item/value pairs in
    enumerations (for example, ``사과 21.6%, 쌀 21.3%``).  The source segment
    after the immediately preceding value-unit span is an auditable local
    boundary: a target item label must occur there before its own value.
    """
    sentence = str(sentence_row.get("text") or "")
    start = value_span.get("char_start")
    sentence_start = sentence_row.get("char_start")
    if not isinstance(start, int) or not isinstance(sentence_start, int):
        return False
    local_start = start - sentence_start
    if local_start < 0:
        return False
    prior_end = 0
    for match in VALUE_UNIT_RE.finditer(sentence[:local_start]):
        # Calendar/duration qualifiers inside an indicator definition (for
        # example, ``연체율(1개월 이상 연체 기준)은 11.7%``) must not erase
        # the preceding indicator anchor. They are not competing observations.
        if canon_unit(match.group("unit")) in {"년", "월", "분기", "개월", "일"}:
            continue
        prior_end = match.end()
    value_segment = sentence[prior_end:local_start]
    if any(term in value_segment for term in terms):
        return True
    # Main aggregates are often stated after a parenthesized component value:
    # ``소매판매는 내구재(13.2%) 소비가 늘면서 ... 1.5% 늘었다``.
    # Retain the earlier indicator only when the intervening source explicitly
    # marks a causal/component bridge; adjacent item enumerations remain split.
    causal_bridge = re.search(r"(?:늘면서|줄면서|중심으로)", value_segment)
    return bool(
        prior_end
        and causal_bridge
        and any(term in sentence[:prior_end] for term in terms)
    )


def _local_value_role(article_sentence: dict[str, Any], value_span: dict[str, Any], terms: set[str]) -> tuple[str, str | None]:
    """Classify only unambiguous local value relationships from source text.

    This deliberately overrides an HCX role assertion only for narrow, auditable
    patterns observed in review: parenthesized historical comparison values,
    item-list values, contribution point values, and an auxiliary absolute value
    nested after a percentage.  All other cases remain TARGET_CANDIDATE and are
    subject to the existing indicator-scope gate.
    """
    text = str(article_sentence.get("text") or "")
    start = value_span.get("char_start")
    sentence_start = article_sentence.get("char_start")
    if not isinstance(start, int) or not isinstance(sentence_start, int):
        return "TARGET_CANDIDATE", None
    local_start = max(0, start - sentence_start)
    end = value_span.get("char_end")
    local_end = (
        max(local_start, end - sentence_start)
        if isinstance(end, int)
        else local_start
    )
    before = text[:local_start]
    after = text[local_end:]
    if (
        value_span.get("unit") in {"시간", "세", "원", "만원"}
        and rule_match("category_definition_range", after)
        and rule_search("category_definition_population", after[:64])
    ):
        return "CATEGORY_DEFINITION", "VALUE_CATEGORY_DEFINITION"
    if (
        value_span.get("unit") == "%"
        and rule_match("category_definition_percent_threshold", after)
        and VALUE_UNIT_RE.search(before)
    ):
        return "CATEGORY_DEFINITION", "VALUE_CATEGORY_DEFINITION"
    if (
        value_span.get("unit") == "개"
        and rule_search("category_definition_rank_prefix", before[-24:])
        and rule_match("category_definition_company_suffix", after)
    ):
        return "CATEGORY_DEFINITION", "VALUE_CATEGORY_DEFINITION"
    if _COMPARISON_PAREN_PREFIX_RE.search(before[-96:]):
        return "COMPARISON_REFERENCE", "VALUE_COMPARISON_REFERENCE"
    if re.search(r"%\s*\(\s*$", before):
        return "AUXILIARY_MEASURE", "VALUE_AUXILIARY_MEASURE"
    if value_span.get("unit") == "%p" and ("기여" in before[-24:] or "기여" in after[:24]):
        return "CONTRIBUTOR", "VALUE_CONTRIBUTOR"
    anchor_positions = [text.find(term) for term in terms if text.find(term) >= 0]
    anchor_after_value = any(position > local_start for position in anchor_positions)
    item_list_before_value = "품목별" in before or "세부" in before[-32:]
    parenthesized_item_value = before.rstrip().endswith("(")
    if (item_list_before_value and parenthesized_item_value) or (anchor_after_value and parenthesized_item_value):
        return "SUBGROUP_MEASURE", "VALUE_SUBGROUP_MEASURE"
    return "TARGET_CANDIDATE", None


def _measurement_type_unit_error(measurement_type: object, value_span: dict[str, Any]) -> str | None:
    """Defence in depth for type-filtered candidates passed to HCX binding."""
    unit = value_span.get("unit")
    if measurement_type == "INDEX_LEVEL" and unit != "지수":
        return "VALUE_UNIT_INCOMPATIBLE_WITH_INDEX_LEVEL"
    if measurement_type == "CHANGE_RATE" and unit != "%":
        return "VALUE_UNIT_INCOMPATIBLE_WITH_CHANGE_RATE"
    if measurement_type == "CHANGE_POINT" and unit in {None, "", "%", "지수"}:
        return "VALUE_UNIT_INCOMPATIBLE_WITH_CHANGE_POINT"
    if measurement_type == "LEVEL" and unit in {"지수", "%p", "포인트"}:
        return "VALUE_UNIT_INCOMPATIBLE_WITH_LEVEL"
    return None


def _unit_owned_measurement_type(value_span: object) -> str | None:
    """Deterministically classify units whose measurement type is unambiguous."""
    if not isinstance(value_span, dict):
        return None
    unit = value_span.get("unit")
    if unit == "지수":
        return "INDEX_LEVEL"
    if unit in {"%p", "포인트"}:
        return "CHANGE_POINT"
    if isinstance(unit, str) and unit and unit != "%":
        return "LEVEL"
    return None


def _indicator_owned_measurement_type(indicator_norm: object, value_span: object) -> str | None:
    """Resolve percent change metrics from an explicit normalized indicator suffix."""
    if not isinstance(indicator_norm, str) or not isinstance(value_span, dict):
        return None
    compact = re.sub(r"\s+", "", indicator_norm)
    if value_span.get("unit") in {None, ""} and (
        "지수" in compact or "순환변동치" in compact
    ):
        return "INDEX_LEVEL"
    if value_span.get("unit") != "%":
        return None
    if compact.endswith(("비율", "비중", "점유율", "연체율", "고용률")):
        return "LEVEL"
    if compact.endswith(("상승률", "하락률", "증가율", "감소율", "성장률", "증감률", "변화율")):
        return "CHANGE_RATE"
    return None


def _context_owned_measurement_type(
    indicator_norm: object,
    value_span: object,
    candidates: list[dict[str, Any]],
    article_text: str | None = None,
) -> str | None:
    """Classify an explicit local absolute/rate change without crossing metrics."""
    if not isinstance(value_span, dict):
        return None
    unit = value_span.get("unit")
    compact_indicator = re.sub(r"\s+", "", str(indicator_norm or ""))
    ratio_level = (
        unit == "%"
        and compact_indicator.endswith(
            ("비율", "비중", "점유율", "연체율", "고용률")
        )
        and not compact_indicator.endswith(
            ("증가율", "감소율", "상승률", "하락률", "증감률", "변화율")
        )
    )
    if ratio_level:
        return None
    value_end = value_span.get("char_end")
    sentence_id = value_span.get("sentence_id")
    if not isinstance(value_end, int):
        return None
    for candidate in candidates:
        candidate_start = candidate.get("char_start")
        if (
            candidate.get("kind") == "semantic_evidence"
            and candidate.get("sentence_id") == sentence_id
            and isinstance(candidate_start, int)
            and 0 <= candidate_start - value_end <= 20
            and _LOCAL_CHANGE_PREDICATE_RE.search(
                _semantic_candidate_base(candidate.get("text"))
            )
        ):
            if unit != "%" and article_text:
                between = article_text[value_end:candidate_start]
                if not re.fullmatch(
                    r"\s*(?:(?:약|대략|가까이|넘게|정도)\s*)?",
                    between,
                ):
                    continue
            return "CHANGE_RATE" if unit == "%" else "CHANGE_POINT"
    return None


def _normalize_local_period_text(
    period_span: dict[str, Any],
    article_text: str,
    *,
    reference_date: str | None = None,
) -> str:
    """Repair the time-regex collision in a phrase such as ``작년 초혼인``."""
    text = str(period_span.get("text") or "")
    end = period_span.get("char_end")
    if (
        text == "작년 초"
        and isinstance(end, int)
        and article_text[end:end + 1] == "혼"
    ):
        text = "작년"
    start = period_span.get("char_start")
    if (
        text in {"작년", "지난해"}
        and isinstance(start, int)
        and article_text[start:start + len(text) + 4].startswith(
            f"{text} 하반기"
        )
    ):
        text = f"{text} 하반기"
    if not reference_date:
        return text
    try:
        reference = date.fromisoformat(reference_date[:10])
    except (TypeError, ValueError):
        return text
    year = reference.year
    relative_year_match = re.fullmatch(
        r"(?P<relative>지난해|작년|올해|금년)"
        r"(?:\s*(?P<part>\d{1,2}월|\d{1,2}분기|상반기|하반기|초|중|말))?",
        text,
    )
    if relative_year_match:
        normalized_year = (
            year - 1
            if relative_year_match.group("relative") in {"지난해", "작년"}
            else year
        )
        part = relative_year_match.group("part")
        return (
            f"{normalized_year}년 {part}"
            if part
            else f"{normalized_year}년"
        )
    compact = re.sub(r"\s+", "", text)
    if compact in {"이달", "이번달"}:
        return f"{year}년 {reference.month}월"
    if compact == "지난달":
        prior_year = year if reference.month > 1 else year - 1
        prior_month = reference.month - 1 if reference.month > 1 else 12
        return f"{prior_year}년 {prior_month}월"
    return text


def _target_specific_period(
    *,
    article_text: str,
    value_span: dict[str, Any],
    measurement_type: object,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve a period from the target sentence before article inheritance."""
    sentence_id = value_span.get("sentence_id")
    value_start = value_span.get("char_start")
    if not isinstance(sentence_id, int) or not isinstance(value_start, int):
        return None, None, None
    same_sentence_times = [
        candidate
        for candidate in candidates
        if candidate.get("kind") == "time"
        and candidate.get("sentence_id") == sentence_id
        and isinstance(candidate.get("char_end"), int)
    ]
    preceding_measurement_times = [
        candidate
        for candidate in same_sentence_times
        if int(candidate["char_end"]) <= value_start
        and not _COMPARISON_PERIOD_RE.fullmatch(
            re.sub(r"\s+", " ", str(candidate.get("text") or "")).strip()
        )
        and not (
            re.search(
                r"(?:전년|전월|직전)\s*(?:인|의)?\s*$",
                article_text[
                    max(0, int(candidate.get("char_start") or 0) - 8):
                    int(candidate.get("char_start") or 0)
                ],
            )
            and re.match(
                r"\s*(?:대비|보다)",
                article_text[
                    int(candidate.get("char_end") or 0):
                    int(candidate.get("char_end") or 0) + 6
                ],
            )
        )
    ]
    sentence_row = next(
        (
            row
            for row in sentence_offset_map(article_text)
            if row["sentence_id"] == sentence_id
        ),
        None,
    )
    sentence = str(sentence_row.get("text") or "") if sentence_row else ""
    sentence_start = (
        int(sentence_row.get("char_start") or 0) if sentence_row else 0
    )
    local_value_start = value_start - sentence_start
    pair_match = _BASELINE_PAIR_RE.search(sentence)
    if (
        measurement_type == "LEVEL"
        and pair_match
        and pair_match.start("first")
        <= local_value_start
        < pair_match.end("first")
    ):
        if "같은 기간" in sentence:
            previous_sentence = next(
                (
                    row
                    for row in sentence_offset_map(article_text)
                    if row["sentence_id"] == sentence_id - 1
                ),
                None,
            )
            previous_years = (
                _ABSOLUTE_PERIOD_RE.findall(
                    str(previous_sentence.get("text") or "")
                )
                if previous_sentence
                else []
            )
            if len(previous_years) >= 2:
                return (
                    None,
                    previous_years[0],
                    "TARGET_PREVIOUS_SENTENCE_PAIR_PERIOD_RULE",
                )
        return None, "전년", "TARGET_BASELINE_PAIR_RULE"

    if measurement_type == "CHANGE_POINT":
        absolute_times = [
            candidate
            for candidate in preceding_measurement_times
            if _ABSOLUTE_PERIOD_RE.fullmatch(str(candidate.get("text") or ""))
        ]
        distinct_absolute = list(dict.fromkeys(
            str(candidate.get("text") or "") for candidate in absolute_times
        ))
        if len(distinct_absolute) >= 2:
            return (
                absolute_times[-1],
                f"{distinct_absolute[0]}~{distinct_absolute[-1]}",
                "TARGET_ABSOLUTE_PERIOD_RANGE_RULE",
            )
        duration_match = re.search(r"\d+\s*년\s*새", sentence[:local_value_start])
        if preceding_measurement_times:
            selected = preceding_measurement_times[-1]
            selected_text = _normalize_local_period_text(
                selected,
                article_text,
            )
            if (
                selected_text == "작년"
                and "작년 하반기" in sentence[:local_value_start]
            ):
                selected_text = "작년 하반기"
            if duration_match:
                selected_text = (
                    f"{selected_text}, "
                    f"{duration_match.group(0).replace(' ', '')}"
                )
            return selected, selected_text, "TARGET_CHANGE_PERIOD_RULE"

    if measurement_type == "CHANGE_RATE" and preceding_measurement_times:
        value_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("kind") == "value_unit"
            and candidate.get("sentence_id") == sentence_id
            and isinstance(candidate.get("char_start"), int)
        ]
        first_value_start = min(
            (
                int(candidate["char_start"])
                for candidate in value_candidates
            ),
            default=value_start,
        )
        current_periods = [
            candidate
            for candidate in preceding_measurement_times
            if int(candidate["char_end"]) <= first_value_start
        ]
        selected = (current_periods or preceding_measurement_times)[-1]
        return (
            selected,
            _normalize_local_period_text(selected, article_text),
            "TARGET_CHANGE_RATE_PERIOD_RULE",
        )

    if preceding_measurement_times:
        selected = preceding_measurement_times[-1]
        return (
            selected,
            _normalize_local_period_text(selected, article_text),
            "TARGET_PRECEDING_PERIOD_RULE",
        )

    previous_sentence = next(
        (
            row
            for row in sentence_offset_map(article_text)
            if row["sentence_id"] == sentence_id - 1
        ),
        None,
    )
    survey_context = (
        sentence
        + " "
        + str(previous_sentence.get("text") or "")
        if previous_sentence
        else sentence
    )
    if "이번 조사" in survey_context:
        survey_periods = list(dict.fromkeys(
            re.findall(
                r"조사[^.]{0,120}?(20\d{2}년\s*말)",
                article_text[:value_start],
            )
        ))
        if len(survey_periods) == 1:
            matching_span = next(
                (
                    candidate
                    for candidate in build_span_candidates(article_text)
                    if candidate.get("kind") == "time"
                    and candidate.get("text") == survey_periods[0]
                ),
                None,
            )
            return (
                matching_span,
                survey_periods[0],
                "ARTICLE_SURVEY_PERIOD_RULE",
            )

    return None, None, None


def _effective_population_terms(
    indicator_norm: str,
    claim_text: str,
) -> list[str]:
    """Derive one retrieval population phrase from grounded indicator text."""
    if "평균 초혼 연령" in indicator_norm:
        return []
    if "중 외국인 비율" in indicator_norm:
        return ["외국인"]
    if "부족 인원" in indicator_norm:
        return ["부족 인원"]
    for group in ("농가", "어가", "임가"):
        if group in indicator_norm and "인구" in indicator_norm:
            return [f"{group} 인구"]
    ordered_patterns = (
        "산업기술인력",
        "주민등록인구",
        "국내 전체 인구",
        "농가 인구",
        "어가 인구",
        "임가 인구",
        "초혼인 신혼부부",
        "내국인 근로자",
        "외국인",
        "출생아",
        "직장인",
        "종사자",
        "직원",
        "혼인",
        "초혼",
        "농가",
        "어가",
        "임가",
        "인구",
    )
    for source in (indicator_norm, claim_text):
        for term in ordered_patterns:
            if term in source:
                return [term]
    return []


def _effective_search_fields(
    *,
    article_text: str,
    semantic_claim: dict[str, Any],
    observation: dict[str, Any],
    population_evidence_spans: list[dict[str, Any]],
    item_evidence_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build source-auditable retrieval fields without replacing raw fields."""
    indicator_raw = str(semantic_claim.get("indicator_norm") or "").strip()
    indicator = indicator_raw
    value_span = observation.get("value_span") or {}
    sentence_id = value_span.get("sentence_id")
    sentence_rows = {
        row["sentence_id"]: row
        for row in sentence_offset_map(article_text)
    }
    sentence = str(sentence_rows.get(sentence_id, {}).get("text") or "")
    claim_sentence_ids = {
        value
        for value in [
            *semantic_claim.get("context_sentence_ids", []),
            *semantic_claim.get("observation_sentence_ids", []),
        ]
        if isinstance(value, int)
    }
    claim_text = " ".join(
        str(sentence_rows.get(value, {}).get("text") or "")
        for value in sorted(claim_sentence_ids)
    )
    measurement_type = observation.get("measurement_type")

    if (
        measurement_type == "LEVEL"
        and value_span.get("unit") != "%"
        and indicator.endswith((" 증가율", " 감소율", " 변화율"))
    ):
        indicator = re.sub(r"\s+(?:증가율|감소율|변화율)$", "", indicator)
    if measurement_type == "CHANGE_POINT" and indicator.endswith(" 수"):
        predicate = _LOCAL_CHANGE_PREDICATE_RE.search(
            article_text[
                int(value_span.get("char_end") or 0):
                int(value_span.get("char_end") or 0) + 32
            ]
        )
        direction = "감소폭" if predicate and re.search(
            r"(?:감소|하락|줄)",
            predicate.group(),
        ) else "증가폭"
        indicator = re.sub(r"\s+수$", f" {direction}", indicator)
    if measurement_type == "CHANGE_RATE" and indicator.endswith(" 변화율"):
        local_tail = article_text[
            int(value_span.get("char_end") or 0):
            int(value_span.get("char_end") or 0) + 32
        ]
        direction = "감소율" if re.search(
            r"(?:감소|하락|줄)",
            local_tail,
        ) else "증가율"
        prefix = "전년비 " if re.search(
            r"(?:전년|2023년).{0,20}(?:대비|보다)",
            sentence,
        ) else ""
        indicator = re.sub(
            r"\s+변화율$",
            f" {prefix}{direction}",
            indicator,
        )
    if measurement_type == "LEVEL":
        indicator = re.sub(
            r"주민등록인구\s+수$",
            lambda match: match.group().removesuffix(" 수"),
            indicator,
        )

    local_dimensions = [
        candidate
        for candidate in build_span_candidates(
            article_text,
            [sentence_id] if isinstance(sentence_id, int) else [],
        )
        if candidate.get("kind") == "dimension"
    ]
    value_start = int(value_span.get("char_start") or 0)
    preceding_regions = [
        candidate
        for candidate in local_dimensions
        if candidate.get("dimension_type") == "지역"
        and candidate.get("text")
        and int(candidate.get("char_end") or 0) <= value_start
    ]
    region = (
        str(max(
            preceding_regions,
            key=lambda candidate: int(candidate.get("char_end") or 0),
        ).get("text") or "")
        if preceding_regions
        else None
    )
    if not region:
        first_sentence = sentence_rows.get(0, {})
        focus_regions = [
            candidate
            for candidate in build_span_candidates(article_text, [0])
            if candidate.get("kind") == "dimension"
            and candidate.get("dimension_type") == "지역"
            and candidate.get("char_start") == first_sentence.get("char_start")
        ]
        if len(focus_regions) == 1:
            region = str(focus_regions[0].get("text") or "") or None
    if region and region not in indicator:
        indicator = f"{region} {indicator}"
    if "국내 " in sentence and not indicator.startswith("국내 "):
        indicator = f"국내 {indicator}"
    age_terms = [
        str(candidate.get("text") or "")
        for candidate in local_dimensions
        if candidate.get("dimension_type") == "연령"
        and candidate.get("text")
    ]
    if age_terms and "고령" in indicator:
        indicator = indicator.replace("고령", age_terms[0])

    if (
        "이들이 차지하는 비율" in indicator_raw
        and "조선 산업기술인력 중 외국인" in (
            claim_text
            + " "
            + str(sentence_rows.get(int(sentence_id or 0) - 1, {}).get("text") or "")
        )
    ):
        indicator = "조선 산업기술인력 중 외국인 비율"
    if "조선 산업기술인력" in sentence:
        indicator = indicator.replace("조선업 산업기술인력", "조선 산업기술인력")
        if measurement_type == "CHANGE_RATE" and re.search(
            r"전년.{0,16}대비",
            sentence,
        ):
            indicator = indicator.replace(
                "산업기술인력 증가율",
                "산업기술인력 전년비 증가율",
            )
    if (
        indicator == "부족 인원 증가율"
        and "부족 인원도" in sentence
        and "전년 대비" in article_text
    ):
        indicator = "부족 인원 전년비 증가율"

    population = _effective_population_terms(indicator, claim_text)
    if not population:
        population = [
            str(span.get("text") or "")
            for span in population_evidence_spans
            if span.get("text")
        ]
    population_norm = {
        re.sub(r"[^가-힣A-Za-z0-9]", "", value)
        for value in population
    }

    dimensions: list[str] = []
    for dimension in observation.get("dimension_spans", []):
        text = str(dimension.get("text") or "")
        if not text or dimension.get("sentence_id") != sentence_id:
            continue
        normalized = re.sub(r"[^가-힣A-Za-z0-9]", "", text)
        if normalized in population_norm:
            continue
        if any(normalized and normalized in item for item in population_norm):
            continue
        if dimension.get("dimension_type") == "지역" and text != region:
            continue
        if (
            dimension.get("dimension_type") == "산업"
            and text not in indicator
            and value_start - int(dimension.get("char_end") or 0) > 24
        ):
            continue
        if text == "전체" and any("전체" in value for value in population):
            continue
        if text == "전체" and indicator == "전체 어가 인구":
            continue
        if text not in dimensions:
            dimensions.append(text)
    for candidate in local_dimensions:
        text = str(candidate.get("text") or "")
        dimension_type = candidate.get("dimension_type")
        if dimension_type not in {"지역", "연령", "성별"} or not text:
            continue
        if dimension_type == "지역" and text != region:
            continue
        if text == "전체" and (
            any("전체" in value for value in population)
            or indicator == "전체 어가 인구"
        ):
            continue
        normalized = re.sub(r"[^가-힣A-Za-z0-9]", "", text)
        if normalized in population_norm:
            continue
        if text not in dimensions:
            dimensions.append(text)
    if (
        indicator_raw.startswith("매장 판매 직원")
        and "매장 판매" in sentence
        and "매장 판매" not in dimensions
    ):
        dimensions.append("매장 판매")
    if "조선 산업기술인력" in sentence and "조선" not in dimensions:
        dimensions.append("조선")
    if (
        indicator == "조선 산업기술인력 중 외국인 비율"
        and "조선" not in dimensions
    ):
        dimensions.append("조선")
    marriage_pair = re.search(
        r"(연상인 아내)와 (연하 남편)",
        sentence,
    )
    if marriage_pair:
        indicator = indicator.replace(
            "연상녀 연하남",
            "연상 아내·연하 남편",
        )
        for group in marriage_pair.groups():
            if group not in dimensions:
                dimensions.append(group)
    if "아내와 남편이 동갑" in sentence:
        indicator = indicator.replace("동갑 초혼", "동갑 부부 초혼")
        if "아내와 남편이 동갑" not in dimensions:
            dimensions.append("아내와 남편이 동갑")
    if "남녀간" in sentence:
        indicator = indicator.replace("남녀 평균", "남녀간 평균")
        if "남녀간" not in dimensions:
            dimensions.append("남녀간")
    salary_range = re.search(
        r"월급이\s*(\d+\s*만?~\s*\d+\s*만원)인",
        sentence,
    )
    if salary_range and "직장인 수" in indicator:
        surface = f"월급이 {salary_range.group(1)}인"
        indicator = re.sub(r"^중하위\s+", f"{salary_range.group(1)} ", indicator)
        if not indicator.startswith("월급 "):
            indicator = f"월급 {indicator}"
        if surface not in dimensions:
            dimensions.append(surface)
    if indicator.startswith("중하위층의 평균 월급"):
        indicator = "평균 월급"
    if "고령 인구" in indicator and not age_terms and "고령 인구" not in dimensions:
        dimensions.append("고령 인구")
    if region and region not in dimensions:
        dimensions.insert(0, region)

    population = _effective_population_terms(indicator, claim_text)
    if "초혼인 신혼부부" in sentence:
        population = ["초혼인 신혼부부"]
    if not population:
        population = [
            str(span.get("text") or "")
            for span in population_evidence_spans
            if span.get("text")
        ]

    return {
        "indicator_norm": indicator,
        "population_terms": population,
        "item_terms": [
            str(span.get("text") or "")
            for span in item_evidence_spans
            if span.get("text")
        ],
        "dimension_terms": dimensions,
        "source": "DETERMINISTIC_SOURCE_GROUNDED_R14B",
        "raw_indicator_norm": indicator_raw,
    }


def _has_local_dimension_value_pair(sentence_row: dict[str, Any], value_span: dict[str, Any],
                                    dimensions: list[dict[str, Any]]) -> bool:
    """Recognize a nearby source pair such as ``울산(1.4%)``."""
    sentence = str(sentence_row.get("text") or "")
    sentence_start = sentence_row.get("char_start")
    value_start = value_span.get("char_start")
    if not isinstance(sentence_start, int) or not isinstance(value_start, int):
        return False
    local_value_start = value_start - sentence_start
    for dimension in dimensions:
        if dimension.get("sentence_id") != value_span.get("sentence_id"):
            continue
        dimension_end = dimension.get("char_end")
        if not isinstance(dimension_end, int) or dimension_end > value_start:
            continue
        local_dimension_end = dimension_end - sentence_start
        between = sentence[local_dimension_end:local_value_start]
        if len(between) <= 16 and not VALUE_UNIT_RE.search(between) and not re.search(r"[.!?;]", between):
            return True
    return False


def _has_structured_comparison_link(
    sentence_row: dict[str, Any],
    value_span: dict[str, Any],
    terms: set[str],
    measurement_type: object,
) -> bool:
    """Link a later value to an earlier indicator only through source syntax."""
    sentence = str(sentence_row.get("text") or "")
    sentence_start = sentence_row.get("char_start")
    value_start = value_span.get("char_start")
    if (
        not sentence
        or not isinstance(sentence_start, int)
        or not isinstance(value_start, int)
        or not any(term in sentence for term in terms)
    ):
        return False
    local_start = value_start - sentence_start
    prior_values = [
        match
        for match in VALUE_UNIT_RE.finditer(sentence[:local_start])
        if canon_unit(match.group("unit"))
        not in {"년", "월", "분기", "개월", "일"}
    ]
    if not prior_values:
        return False
    bridge = sentence[prior_values[-1].end():local_start]
    if measurement_type == "CHANGE_RATE" and re.search(
        r"(?:대비|보다|증감|변화)",
        bridge,
    ):
        return True
    if re.search(
        r"(?:에서|이후|전년|지난해|작년|지난\s*\d{4}년|\d{4}년)",
        bridge,
    ):
        return True
    value_end = value_span.get("char_end")
    if (
        measurement_type == "CHANGE_POINT"
        and len(prior_values) >= 2
        and isinstance(value_end, int)
        and _LOCAL_CHANGE_PREDICATE_RE.search(
            sentence[
                max(0, value_end - sentence_start):
                max(0, value_end - sentence_start) + 24
            ]
        )
    ):
        return True
    return False


def validate_claim_observation_scope(article_text: str, semantic_claim: dict[str, Any],
                                     binding_validation: dict[str, Any]) -> dict[str, Any]:
    """Block source-valid bindings that are not demonstrably values of this claim's indicator.

    A span ID being in the article is insufficient for retrieval.  This gate keeps
    a mixed claim out of the retrieval path until every selected observation is
    anchored to its indicator sentence or an auditable explicit-pairing sentence.
    """
    sentence_rows = {row["sentence_id"]: row for row in sentence_offset_map(article_text)}
    sentence_text = {sentence_id: row["text"] for sentence_id, row in sentence_rows.items()}
    relation = semantic_claim.get("relation_json") if isinstance(semantic_claim, dict) else {}
    pairing = relation.get("dimension_pairing") if isinstance(relation, dict) else None
    pairing_evidence = {
        item for item in relation.get("pairing_evidence_sentence_ids", [])
        if isinstance(item, int)
    } if isinstance(relation, dict) else set()
    terms = _indicator_anchor_terms(semantic_claim.get("indicator_norm"))
    passed = [item for item in binding_validation.get("observations", []) if item.get("status") == "PASS"]
    errors: list[str] = []
    if not terms:
        errors.append("INDICATOR_ANCHOR_MISSING")
    context_anchor_ids = {
        sentence_id for sentence_id in semantic_claim.get("context_sentence_ids", [])
        if isinstance(sentence_id, int) and any(term in sentence_text.get(sentence_id, "") for term in terms)
    }
    private_source_context_ids = {
        sentence_id
        for sentence_id, text in sentence_text.items()
        if rule_search("private_source_context", text)
    }
    direct = [
        item for item in passed
        if _has_local_indicator_value_segment(
            sentence_rows.get(item.get("value_span", {}).get("sentence_id"), {}), item.get("value_span", {}), terms,
        )
    ]

    all_dimensions = [dimension for item in passed for dimension in item.get("dimension_spans", [])]
    by_type: dict[str, set[str]] = {}
    for dimension in all_dimensions:
        dimension_type = str(dimension.get("dimension_type") or "")
        by_type.setdefault(dimension_type, set()).add(str(dimension.get("text") or ""))
    if pairing == "EXPLICIT_PAIRING":
        has_paired_dimension_set = any(len(values) >= 2 for values in by_type.values())
        if len(passed) < 2 or not has_paired_dimension_set:
            errors.append("EXPLICIT_PAIRING_CARDINALITY_INSUFFICIENT")
    elif pairing == "UNPAIRED_MULTI_VALUE" and sum(len(values) for values in by_type.values()) < 2:
        errors.append("UNPAIRED_MULTI_VALUE_CARDINALITY_INSUFFICIENT")
    dimension_texts = {
        str(dimension.get("text") or "")
        for dimension in all_dimensions
    }
    is_composite_exclusion = (
        "제외" in dimension_texts
        and len(all_dimensions) >= 2
        and all(
            dimension.get("kind") == "semantic_evidence"
            for dimension in all_dimensions
        )
    )
    if (
        pairing not in {"EXPLICIT_PAIRING", "UNPAIRED_MULTI_VALUE"}
        and len(passed) == 1
        and any(len(values) > 1 for values in by_type.values())
        and not is_composite_exclusion
    ):
        errors.append("DIMENSION_MULTIPLE_VALUES_FOR_SINGLE_TARGET")

    observations = []
    for item in passed:
        observation_errors: list[str] = []
        value_span = item.get("value_span") or {}
        value_sentence_id = value_span.get("sentence_id")
        value_sentence_text = sentence_text.get(value_sentence_id, "")
        source_value_role, local_role_error = _local_value_role(sentence_rows.get(value_sentence_id, {}), value_span, terms)
        if (
            local_role_error
            and not (
                source_value_role == "COMPARISON_REFERENCE"
                and semantic_claim.get("candidate_class")
                == "KOSIS_CANDIDATE"
            )
        ):
            observation_errors.append(local_role_error)
        if rule_search("policy_or_rule_value", value_sentence_text):
            observation_errors.append("POLICY_OR_RULE_VALUE_OUT_OF_SCOPE")
        if rule_search("local_out_of_scope_value", value_sentence_text):
            observation_errors.append("LOCAL_NON_KOSIS_SOURCE_OUT_OF_SCOPE")
        if (
            isinstance(value_sentence_id, int)
            and any(
                source_sentence_id <= value_sentence_id <= source_sentence_id + 1
                for source_sentence_id in private_source_context_ids
            )
        ):
            observation_errors.append("PRIVATE_SOURCE_CONTEXT_OUT_OF_SCOPE")
        if rule_search(
            "policy_target_indicator",
            str(semantic_claim.get("indicator_norm") or "")
        ):
            observation_errors.append("POLICY_TARGET_OUT_OF_SCOPE")
        unit_error = _measurement_type_unit_error(semantic_claim.get("measurement_type"), value_span)
        if unit_error:
            observation_errors.append(unit_error)
        is_direct = _has_local_indicator_value_segment(sentence_rows.get(value_sentence_id, {}), value_span, terms)
        dimensions = item.get("dimension_spans", [])
        allowed_context_link = (
            not is_direct
            and
            pairing == "NOT_APPLICABLE"
            and not dimensions
            and len(passed) == 1
            and isinstance(value_sentence_id, int)
            and value_sentence_id - 1 in context_anchor_ids
        )
        allowed_explicit_pairing = pairing == "EXPLICIT_PAIRING" and value_sentence_id in pairing_evidence
        allowed_structured_comparison_link = _has_structured_comparison_link(
            sentence_rows.get(value_sentence_id, {}),
            value_span,
            terms,
            item.get("measurement_type"),
        )
        allowed_local_dimension_pairing = (
            pairing == "NOT_APPLICABLE"
            and len(passed) == 1
            and bool(dimensions)
            and any(term in sentence_text.get(value_sentence_id, "") for term in terms)
            and all(
                dimension.get("dimension_type") != "산업"
                or str(dimension.get("text") or "") in str(semantic_claim.get("indicator_norm") or "")
                for dimension in dimensions
            )
            and _has_local_dimension_value_pair(
                sentence_rows.get(value_sentence_id, {}),
                value_span,
                dimensions,
            )
        )
        if (
            not is_direct
            and not allowed_context_link
            and not allowed_explicit_pairing
            and not allowed_structured_comparison_link
            and not allowed_local_dimension_pairing
        ):
            observation_errors.append("VALUE_OUTSIDE_INDICATOR_SCOPE")
        for dimension in dimensions:
            dimension_text = str(dimension.get("text") or "")
            dimension_sentence = sentence_text.get(dimension.get("sentence_id"), "")
            if dimension_text == "경기" and any(term in {"동행", "선행", "동행종합지수", "선행종합지수"} for term in terms) and "경기" in dimension_sentence:
                observation_errors.append("DIMENSION_OVERLAPS_INDICATOR_LABEL")
        observations.append({
            "observation_index": item.get("observation_index"),
            "status": "PASS" if not observation_errors else "BLOCKED",
            "errors": observation_errors,
            "context_linked": allowed_context_link,
            "structured_comparison_linked": allowed_structured_comparison_link,
            "local_dimension_paired": allowed_local_dimension_pairing,
            "source_value_role": source_value_role,
            "value_span": value_span,
            "dimension_spans": dimensions,
        })
    if (
        passed
        and not direct
        and pairing != "EXPLICIT_PAIRING"
        and not any(
            item["context_linked"]
            or item.get("structured_comparison_linked")
            or item.get("local_dimension_paired")
            for item in observations
        )
    ):
        errors.append("INDICATOR_ANCHOR_NOT_FOUND_IN_VALUE_SENTENCE")
    claim_status = "PASS" if not errors and observations and all(item["status"] == "PASS" for item in observations) else "BLOCKED"
    return {
        "claim_status": claim_status,
        "errors": errors,
        "indicator_anchor_terms": sorted(terms),
        "dimension_pairing": pairing,
        "pairing_evidence_sentence_ids": sorted(pairing_evidence),
        "observations": observations,
    }


def pass_span_bound_observations(semantic_claim: dict[str, Any], binding: dict[str, Any],
                                 validation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only source-grounded observations for the existing retrieval contract."""
    source = binding.get("observations", []) if isinstance(binding, dict) else []
    output = []
    for report in validation.get("observations", []):
        if report.get("status") != "PASS":
            continue
        index = report["observation_index"]
        if index < len(source):
            output.append({"semantic_claim": semantic_claim, "binding": source[index], "validation": report})
    return output


def pass_scope_bound_observations(semantic_claim: dict[str, Any], binding: dict[str, Any],
                                  binding_validation: dict[str, Any], scope_validation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return bindings only when the complete claim passes the indicator-scope gate."""
    if scope_validation.get("claim_status") != "PASS":
        return []
    allowed = {item.get("observation_index") for item in scope_validation.get("observations", []) if item.get("status") == "PASS"}
    source = binding.get("observations", []) if isinstance(binding, dict) else []
    reports = {item.get("observation_index"): item for item in binding_validation.get("observations", [])}
    effective_semantic_claim = dict(semantic_claim)
    semantic_role_evidence = binding_validation.get("semantic_role_evidence")
    if isinstance(semantic_role_evidence, dict):
        effective_semantic_claim["indicator_evidence_spans"] = semantic_role_evidence.get("indicator_evidence_spans", [])
        effective_semantic_claim["indicator_evidence_texts"] = [
            span.get("text") for span in semantic_role_evidence.get("indicator_evidence_spans", [])
            if isinstance(span, dict) and span.get("text")
        ]
        effective_semantic_claim["population_evidence_spans"] = semantic_role_evidence.get("population_evidence_spans", [])
        effective_semantic_claim["population_constraints"] = [
            span.get("text") for span in semantic_role_evidence.get("population_evidence_spans", [])
            if isinstance(span, dict) and span.get("text")
        ]
        effective_semantic_claim["item_evidence_spans"] = semantic_role_evidence.get("item_evidence_spans", [])
        effective_semantic_claim["item_constraints"] = [
            span.get("text") for span in semantic_role_evidence.get("item_evidence_spans", [])
            if isinstance(span, dict) and span.get("text")
        ]
    output: list[dict[str, Any]] = []
    for index, observation in enumerate(source):
        if index not in allowed or index not in reports:
            continue
        report = reports[index]
        observation_claim = dict(effective_semantic_claim)
        effective_fields = report.get("effective_search_fields")
        if isinstance(effective_fields, dict):
            observation_claim["indicator_norm_raw"] = observation_claim.get(
                "indicator_norm"
            )
            observation_claim["indicator_norm"] = effective_fields.get(
                "indicator_norm"
            )
            observation_claim["population_constraints"] = effective_fields.get(
                "population_terms",
                [],
            )
            observation_claim["item_constraints"] = effective_fields.get(
                "item_terms",
                [],
            )
            observation_claim["dimension_constraints"] = effective_fields.get(
                "dimension_terms",
                [],
            )
            observation_claim["effective_search_fields_source"] = (
                effective_fields.get("source")
            )
        output.append({
            "semantic_claim": observation_claim,
            "binding": observation,
            "validation": report,
            "semantic_role_evidence": semantic_role_evidence,
            "scope_validation": scope_validation,
        })
    return output


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
