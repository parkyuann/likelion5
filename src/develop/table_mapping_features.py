"""Deterministic retrieval and table-alignment inputs derived from validated HCX claims.

This module deliberately does not call an LLM or KOSIS.  It converts only
semantic/span/scope-validated observations to retrieval features and emits
separate block records for claims that must not enter the retrieval path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

try:
    from .article_claim_pipeline import sentence_offset_map
except ImportError:  # pragma: no cover
    from article_claim_pipeline import sentence_offset_map


FEATURE_SCHEMA_VERSION = "1.3"
FEATURE_BUILDER_VERSION = "20260729.1"
_TERM_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_GENERIC_TERMS = frozenset({"상승률", "증가율", "감소율", "성장률", "비율", "기여도", "순환변동치", "평균", "생산", "가격", "상승", "증가", "감소"})
_SIDO = frozenset({"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"})


def _unique_strings(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value).strip() if isinstance(value, str) else ""
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _indicator_terms(indicator_norm: object, scope_validation: dict[str, Any]) -> list[str]:
    scoped = scope_validation.get("indicator_anchor_terms")
    if isinstance(scoped, list):
        return _unique_strings(scoped)
    if not isinstance(indicator_norm, str):
        return []
    return _unique_strings(
        term for term in _TERM_RE.findall(re.sub(r"\([^)]*\)", "", indicator_norm))
        if term not in _GENERIC_TERMS
    )


def _semantic_terms(semantic_claim: dict[str, Any], field: str) -> list[str]:
    """Read optional semantic fields without inventing terms from prose."""
    value = semantic_claim.get(field)
    if isinstance(value, str):
        return _unique_strings([value])
    if isinstance(value, list):
        return _unique_strings(value)
    return []


def _region_level(text: str) -> str | None:
    if text in {"전국", "국내", "대한민국"}:
        return "국가"
    if text in _SIDO or text.endswith(("특별시", "광역시", "특별자치시", "도", "특별자치도")):
        return "시도"
    if text.endswith(("시", "군", "구")):
        return "시군구"
    if text.endswith(("읍", "면", "동", "리")):
        return "읍면동"
    return None


def _dimension_constraints(spans: object) -> list[dict[str, Any]]:
    output = []
    for span in spans if isinstance(spans, list) else []:
        if not isinstance(span, dict):
            continue
        raw = span.get("text")
        dimension_type = span.get("dimension_type")
        if not isinstance(raw, str) or not raw.strip() or not isinstance(dimension_type, str) or not dimension_type:
            continue
        normalized = raw.strip()
        output.append({
            "dimension_type": dimension_type,
            "raw": raw,
            "normalized": normalized,
            "granularity": _region_level(normalized) if dimension_type == "지역" else None,
            "source_span_id": span.get("span_id"),
            "sentence_id": span.get("sentence_id"),
        })
    return output


def _span_ids(value_span: dict[str, Any], period_span: dict[str, Any] | None,
              dimension_constraints: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "value": _unique_strings([value_span.get("span_id")]),
        "period": _unique_strings([(period_span or {}).get("span_id")]),
        "dimension": _unique_strings(item.get("source_span_id") for item in dimension_constraints),
    }


def _block_reasons(scope_validation: dict[str, Any], binding_validation: dict[str, Any] | None) -> list[str]:
    reasons = list(scope_validation.get("errors") or [])
    for observation in scope_validation.get("observations") or []:
        reasons.extend(observation.get("errors") or [])
    if isinstance(binding_validation, dict):
        reasons.extend(binding_validation.get("errors") or [])
        for observation in binding_validation.get("observations") or []:
            if observation.get("status") != "PASS":
                reasons.extend(observation.get("errors") or [])
    return _unique_strings(reasons) or ["SCOPE_VALIDATION_BLOCKED"]


def build_table_mapping_features(*, article_idx: str, article_sha256: str, article_text: str,
                                 claim_index: int, semantic_claim: dict[str, Any],
                                 binding: dict[str, Any] | None, binding_validation: dict[str, Any] | None,
                                 scope_validation: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return retrieval-eligible observation features and, if blocked, one audit record.

    The two result types are intentionally separate: invalid claims must remain
    visible to audit, but cannot masquerade as table-search features.
    """
    claim_id = f"{article_idx}:{claim_index}"
    indicator_norm = semantic_claim.get("indicator_norm") if isinstance(semantic_claim.get("indicator_norm"), str) else ""
    relation = semantic_claim.get("relation_json") if isinstance(semantic_claim.get("relation_json"), dict) else {}
    sentence_text = {row["sentence_id"]: row["text"] for row in sentence_offset_map(article_text)}
    common = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "claim_id": claim_id,
        "source_article_id": str(article_idx),
        "source_article_sha256": article_sha256,
        "claim_index": claim_index,
        "claim_type": semantic_claim.get("claim_type"),
        "indicator_norm": indicator_norm,
        "measurement_type": semantic_claim.get("measurement_type"),
        "indicator_terms": _indicator_terms(indicator_norm, scope_validation),
        "population_terms": _semantic_terms(semantic_claim, "population_constraints") or _semantic_terms(semantic_claim, "population_terms") or _semantic_terms(semantic_claim, "population_raw"),
        "item_constraint_terms": _semantic_terms(semantic_claim, "item_constraints") or _semantic_terms(semantic_claim, "item_constraint_terms"),
        "period_constraint_terms": _semantic_terms(semantic_claim, "period_constraints"),
        "comparison_constraint_terms": _semantic_terms(semantic_claim, "comparison_constraints"),
        "relation_status": relation.get("dimension_pairing"),
        "context_sentence_ids": semantic_claim.get("context_sentence_ids", []),
        "observation_sentence_ids": semantic_claim.get("observation_sentence_ids", []),
    }
    if scope_validation.get("claim_status") != "PASS" or not isinstance(binding, dict) or not isinstance(binding_validation, dict):
        return [], {
            **common,
            "retrieval_eligibility": False,
            "block_reasons": _block_reasons(scope_validation, binding_validation),
        }

    source_observations = binding.get("observations") if isinstance(binding.get("observations"), list) else []
    binding_by_index = {
        item.get("observation_index"): item for item in binding_validation.get("observations", [])
        if isinstance(item, dict)
    }
    scope_by_index = {
        item.get("observation_index"): item for item in scope_validation.get("observations", [])
        if isinstance(item, dict)
    }
    output = []
    for observation_index, source_observation in enumerate(source_observations):
        binding_report = binding_by_index.get(observation_index, {})
        scope_report = scope_by_index.get(observation_index, {})
        if binding_report.get("status") != "PASS" or scope_report.get("status") != "PASS":
            continue
        value_span = binding_report.get("value_span") if isinstance(binding_report.get("value_span"), dict) else {}
        if not value_span.get("span_id"):
            continue
        period_span = binding_report.get("period_span") if isinstance(binding_report.get("period_span"), dict) else None
        dimensions = _dimension_constraints(binding_report.get("dimension_spans"))
        output.append({
            **common,
            "feature_id": f"{claim_id}:{observation_index}",
            "observation_id": f"{claim_id}:{observation_index}",
            "observation_index": observation_index,
            "retrieval_eligibility": True,
            "block_reasons": [],
            "measurement_type": binding_report.get("measurement_type") or semantic_claim.get("measurement_type"),
            "value": value_span.get("value"),
            "value_raw": value_span.get("text"),
            "unit": value_span.get("unit"),
            "period": {
                "raw": period_span.get("text") if period_span else None,
                "source_span_id": period_span.get("span_id") if period_span else None,
                "sentence_id": period_span.get("sentence_id") if period_span else None,
            },
            "dimension_constraints": dimensions,
            "dimension_terms": _unique_strings(item["normalized"] for item in dimensions),
            "source_span_ids": _span_ids(value_span, period_span, dimensions),
            "value_sentence_id": value_span.get("sentence_id"),
            "value_sentence_text": sentence_text.get(value_span.get("sentence_id"), ""),
            "context_linked": bool(scope_report.get("context_linked")),
            "source_value_role": scope_report.get("source_value_role"),
            "value_role": binding_report.get("value_role"),
            "indicator_value_relation": binding_report.get("indicator_value_relation"),
            "relation_contract_status": binding_report.get("relation_contract_status"),
            "source_binding": {
                "value_span_id": source_observation.get("value_span_id"),
                "period_span_id": source_observation.get("period_span_id"),
                "dimension_span_ids": source_observation.get("dimension_span_ids", []),
                "value_role": source_observation.get("value_role"),
                "indicator_value_relation": source_observation.get("indicator_value_relation"),
                "relation_evidence_sentence_ids": source_observation.get("relation_evidence_sentence_ids", []),
            },
        })
    if output:
        return output, None
    return [], {
        **common,
        "retrieval_eligibility": False,
        "block_reasons": ["NO_SCOPE_VALIDATED_OBSERVATION"],
    }
