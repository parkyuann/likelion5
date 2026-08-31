"""Deterministic user-intent routing for the article verification shadow.

The router decides what the user wants before retrieval.  It never inspects
official cell values and never chooses a table.  Missing semantic slots become
an explicit user question instead of an inferred answer.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "user-intent-router-shadow-v1"

TASK_INTENTS = frozenset({
    "VERIFY_CLAIM", "LOOKUP_VALUE", "COMPARE_PERIODS", "VERIFY_ARTICLE",
    "EXPLAIN_MISMATCH", "CLARIFICATION_REQUIRED", "UNSUPPORTED",
})
MEASUREMENT_INTENTS = frozenset({
    "LEVEL", "CHANGE_RATE", "CHANGE_POINT", "DIFFERENCE", "RATIO",
    "CHANGE_UNSPECIFIED",
})

_VERIFY = re.compile(r"검증|사실인지|맞는지|맞나요|진위|주장.{0,8}(?:확인|판정)")
_ARTICLE_ALL = re.compile(r"기사\s*전체|전체\s*(?:수치|주장)|모든\s*(?:수치|주장)|기사에\s*나온\s*(?:수치|주장)")
_EXPLAIN = re.compile(r"왜|이유|원인|불일치|다르(?:게|지|나요|다)")
_COMPARE = re.compile(r"비교|전년\s*대비|증가|감소|증감|변화|차이")
_POINT = re.compile(r"%p|퍼센트\s*포인트|%\s*포인트|증가폭|감소폭")
_RATE = re.compile(r"%|퍼센트|증가율|감소율|증감률|변화율")
_DIFFERENCE = re.compile(r"증가량|감소량|증감량|차이|비교|몇\s*(?:명|건|개|원)")
_RATIO = re.compile(r"비율\s*(?:계산|비교)|몇\s*배")
_CHANGE = re.compile(r"전년\s*대비|증가|감소|증감|변화|차이")
_REGION = re.compile(r"전국|대한민국|서울(?:특별시)?|부산(?:광역시)?|대구(?:광역시)?|인천(?:광역시)?|광주(?:광역시)?|대전(?:광역시)?|울산(?:광역시)?|세종(?:특별자치시)?|경기(?:도)?|강원(?:특별자치도|도)?|충청북도|충청남도|전북특별자치도|전라북도|전라남도|경상북도|경상남도|제주특별자치도")


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _norm(value: Any) -> str:
    return re.sub(r"[\s\-_./:(),]+", "", str(value or "")).casefold()


def _fields(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("retrieval_fields")
    return value if isinstance(value, Mapping) else {}


def _indicator_base(value: Any) -> str:
    text = _norm(value)
    text = re.sub(r"(?:연간|연도별|월간|월별|분기별|분기)", "", text)
    return re.sub(r"(?:전년대비)?(?:증가|감소|증감|변화)?(?:량|률|율|폭)$", "", text)


def _candidate_rows(rows: Sequence[Mapping[str, Any]], query: str) -> list[dict[str, Any]]:
    query_norm = _norm(query)
    matched: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        fields = _fields(row)
        indicator = fields.get("indicator") or row.get("indicator_label")
        base = _indicator_base(indicator)
        if base and base in query_norm:
            matched.append(row)
    return matched


def _periods(query: str, rows: Sequence[Mapping[str, Any]], asks_change: bool) -> tuple[list[str], str]:
    explicit = sorted(set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", query)))
    source = "QUERY_EXPLICIT"
    if not explicit:
        observed: set[str] = set()
        for row in rows:
            fields = _fields(row)
            period = fields.get("period") if isinstance(fields.get("period"), Mapping) else {}
            measurement = period.get("measurement") if isinstance(period.get("measurement"), Mapping) else {}
            baseline = period.get("baseline") if isinstance(period.get("baseline"), Mapping) else {}
            for value in (
                fields.get("period_absolute"), measurement.get("absolute"), baseline.get("absolute"),
            ):
                if re.fullmatch(r"(?:19|20)\d{2}", str(value or "")):
                    observed.add(str(value))
        explicit = sorted(observed)
        source = "ARTICLE_CLAIM_PERIOD"
    if asks_change and len(explicit) == 1:
        explicit = [str(int(explicit[0]) - 1), explicit[0]]
        source += "+DERIVED_PREVIOUS_YEAR"
    return explicit, source


def _measurement_intents(query: str, candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    if _POINT.search(query):
        return ["LEVEL", "CHANGE_POINT"]
    if _RATE.search(query):
        return ["LEVEL", "CHANGE_RATE"]
    if _RATIO.search(query):
        return ["LEVEL", "RATIO"]
    if _DIFFERENCE.search(query):
        return ["LEVEL", "DIFFERENCE"]
    if _CHANGE.search(query):
        observed = sorted({
            str(_fields(row).get("measurement_type") or "")
            for row in candidates
            if str(_fields(row).get("measurement_type") or "") in {"CHANGE_RATE", "CHANGE_POINT", "DIFFERENCE"}
        })
        if len(observed) == 1:
            return ["LEVEL", observed[0]]
        return ["LEVEL", "CHANGE_UNSPECIFIED"]
    return ["LEVEL"]


def _question(slot: str) -> dict[str, Any]:
    prompts = {
        "indicator": "확인하려는 수치를 구체적으로 알려주세요. 예: ‘대통령 국정수행 긍정평가 비율’, ‘A정당 정당지지도’, ‘전국 합계출산율’, ‘전국 출생아 수’.",
        "period": "확인할 기준 연도 또는 비교할 두 연도를 알려주세요.",
        "change_measure": "증가량·증가율·퍼센트포인트 중 무엇을 확인할까요?",
    }
    options = {
        "change_measure": ["증가량 또는 감소량", "증가율 또는 감소율", "퍼센트포인트 변화"],
    }.get(slot, [])
    return {
        "question_id": f"intent-{slot}", "slot": slot,
        "prompt": prompts[slot], "input_mode": "OPTIONS" if options else "FREE_TEXT",
        "options": options, "options_complete": bool(options), "answer": None,
        "model_prefill": False, "internal_ids_exposed": False,
    }


def route_user_intent(
    query: str,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return an auditable task, measurement, target, and execution contract."""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    candidates = _candidate_rows(rows, text)
    article_scope = bool(_ARTICLE_ALL.search(text))
    asks_change = bool(_COMPARE.search(text))

    if article_scope:
        task_intent = "VERIFY_ARTICLE"
    elif _EXPLAIN.search(text):
        task_intent = "EXPLAIN_MISMATCH"
    elif _VERIFY.search(text):
        task_intent = "VERIFY_CLAIM"
    elif _COMPARE.search(text):
        task_intent = "COMPARE_PERIODS"
    else:
        task_intent = "LOOKUP_VALUE"

    measurements = _measurement_intents(text, candidates)
    periods, period_source = _periods(text, candidates, asks_change)
    region_match = _REGION.search(text)
    region = "전국" if region_match and region_match.group(0) in {"전국", "대한민국"} else (region_match.group(0) if region_match else None)
    missing: list[str] = []
    if not article_scope and not candidates:
        missing.append("indicator")
    if asks_change and len(periods) < 2:
        missing.append("period")
    if "CHANGE_UNSPECIFIED" in measurements:
        missing.append("change_measure")

    target_ids = [str(row.get("target_id") or row.get("value_span_id") or "") for row in candidates]
    target_ids = [value for value in target_ids if value]
    required_operations = ["SELECT_TARGET"]
    if task_intent in {"VERIFY_CLAIM", "VERIFY_ARTICLE", "EXPLAIN_MISMATCH"}:
        required_operations.append("COMPARE_CLAIM_TO_OFFICIAL")
    else:
        required_operations.append("RETURN_OFFICIAL_VALUE")
    if asks_change:
        required_operations.extend(["FETCH_BASELINE_SAME_SERIES", "COMPUTE_CHANGE"])
    if task_intent == "EXPLAIN_MISMATCH":
        required_operations.append("EXPLAIN_WITHOUT_CAUSAL_OVERCLAIM")

    status = "CLARIFICATION_REQUIRED" if missing else "READY"
    result = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "task_intent": "CLARIFICATION_REQUIRED" if missing else task_intent,
        "resolved_task_intent": task_intent,
        "measurement_intents": measurements,
        "target_scope": "ALL_ARTICLE_CLAIMS" if article_scope else "SINGLE_CLAIM",
        "primary_target_measurement": "LEVEL",
        "candidate_target_ids": target_ids,
        "requested_periods": periods,
        "period_source": period_source,
        "requested_region": region,
        "missing_slots": missing,
        "questions": [_question(slot) for slot in missing],
        "execution_plan": {
            "target_selection": "ALL" if article_scope else "PRIMARY_LEVEL",
            "required_operations": required_operations,
            "annual_requery_required": asks_change,
            "web_search_allowed": False,
        },
        "decision_source": "DETERMINISTIC_RULE",
        "model_calls": 0,
        "value_used_for_target_selection": False,
    }
    result["sha256"] = _sha(result)
    return result


__all__ = [
    "CONTRACT_VERSION", "MEASUREMENT_INTENTS", "TASK_INTENTS", "route_user_intent",
]
