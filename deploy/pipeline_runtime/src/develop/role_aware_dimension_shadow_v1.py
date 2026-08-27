"""Opt-in role-aware helpers for article-body retrieval and late binding.

The shadow never names a table, article, candidate rank, or observed value.
It preserves report/source terms as independent retrieval paths, derives a
unit only from an explicit terminal label qualifier, and permits one disclosed
nationwide default only on a geographic axis.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Sequence

from src.develop.article_body_sentence_splitter_v1 import iter_article_body_sentence_spans


CONTRACT_VERSION = "role-aware-dimension-shadow-v1"
_AGENCY = re.compile(
    r"(?P<name>[가-힣A-Za-z0-9·]{2,40}(?:청|처|부|원|기관|위원회|공단|공사|연구원|협회|은행))"
    r"(?=[이가은는]\s*(?:\d{1,2}일\s*)?(?:발표|공표|공개))"
)
_QUOTED = re.compile(r"['\"‘’“”]([^'\"‘’“”]{2,80})['\"‘’“”]")
_UNIT = re.compile(
    r"^(?:명|건|개|가구|세대|호|원|천원|만원|백만원|억원|조원|%|％|퍼센트|"
    r"천명당|십만명당|백만명당|명당|건당|개당|회|대|톤|kg|㎏|km|㎞|m²|㎡)$",
    re.IGNORECASE,
)
_ENGLISH_UNIT_MAP = {
    "person": "명",
    "persons": "명",
    "case": "건",
    "cases": "건",
    "per 1000 population": "천명당",
}


def source_sentence(article_text: str, sentence_id: Any) -> str:
    try:
        wanted = int(sentence_id)
    except (TypeError, ValueError):
        return ""
    return next(
        (text for sid, _start, _end, text in iter_article_body_sentence_spans(str(article_text or "")) if sid == wanted),
        "",
    )


def extract_source_terms(text: str) -> tuple[dict[str, str], ...]:
    """Extract inspectable agency/report terms without a model or table data."""
    sentence = str(text or "")
    terms: list[dict[str, str]] = []
    for match in _AGENCY.finditer(sentence):
        terms.append({"role": "agency", "text": match.group("name")})
    for quoted in _QUOTED.findall(sentence):
        cleaned = re.sub(r"^\s*\d{4}\s*년(?:\s*\d{1,2}\s*월)?\s*", "", quoted).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned and not re.fullmatch(r"[\d\s./~-]+", cleaned):
            terms.append({"role": "report", "text": cleaned})
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for term in terms:
        unique.setdefault((term["role"], term["text"]), term)
    return tuple(unique.values())


def reranker_query(indicator: Any, source_terms: Sequence[Mapping[str, Any]], period: Any) -> str:
    parts = [str(indicator or "").strip()]
    parts.extend(str(term.get("text") or "").strip() for term in source_terms if isinstance(term, Mapping))
    period_text = str(period or "").strip()
    if period_text:
        parts.append(period_text)
    return " ".join(dict.fromkeys(part for part in parts if part))


def infer_profile_units(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a profile and fill only blank units explicit in terminal labels."""
    result = deepcopy(dict(profile))
    for item in result.get("items") or []:
        if isinstance(item, dict):
            _fill_label_unit(item, "itm_nm")
    for dimension in result.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        for value in dimension.get("values") or []:
            if isinstance(value, dict):
                _fill_label_unit(value, "value_name")
    return result


def _fill_label_unit(row: dict[str, Any], label_key: str) -> None:
    if str(row.get("unit_nm") or "").strip():
        return
    match = re.search(r"\(([^()]*)\)\s*$", str(row.get(label_key) or ""))
    unit = str(match.group(1) if match else "").strip()
    if unit and _UNIT.fullmatch(unit):
        row["unit_nm"] = unit
        row["unit_id"] = str(row.get("unit_id") or f"LABEL:{unit}")
        row["unit_inference"] = {
            "rule_id": "terminal-parenthetical-unit",
            "rule_version": 1,
            "source_label": str(row.get(label_key) or ""),
        }
        return
    english_key = {"value_name": "value_name_eng", "itm_nm": "itm_nm_eng"}.get(label_key, "")
    english_label = str(row.get(english_key) or "")
    english_match = re.search(r"\(([^()]*)\)\s*$", english_label)
    mapped = _ENGLISH_UNIT_MAP.get(str(english_match.group(1) if english_match else "").strip().casefold())
    if mapped:
        row["unit_nm"] = mapped
        row["unit_id"] = str(row.get("unit_id") or f"LABEL_EN:{mapped}")
        row["unit_inference"] = {
            "rule_id": "terminal-parenthetical-english-unit-map",
            "rule_version": 1,
            "source_label": english_label,
        }


def select_query_target(
    rows: Sequence[Mapping[str, Any]], query: str,
    user_intent: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select one routed claim by indicator and period language, never value."""
    if user_intent and user_intent.get("execution_plan", {}).get("target_selection") == "ALL":
        selected = [dict(row) for row in rows]
        return selected, {
            "contract_version": CONTRACT_VERSION, "status": "SELECTED_ALL",
            "query": query, "candidate_count": len(selected),
            "query_intent": user_intent.get("task_intent"),
            "user_intent_sha256": user_intent.get("sha256"), "value_used": False,
        }
    query_norm = _norm(query)
    query_years = set(re.findall(r"(?<!\d)\d{4}(?!\d)", str(query or "")))
    primary_measurement = str((user_intent or {}).get("primary_target_measurement") or "")
    asks_change = (
        primary_measurement != "LEVEL"
        if primary_measurement
        else bool(re.search(r"증가|감소|증감|변화|차이|대비", str(query or "")))
    )
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for source in rows:
        row = dict(source)
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), Mapping) else {}
        indicator = str(fields.get("indicator") or row.get("indicator_label") or "")
        indicator_norm = _norm(indicator)
        indicator_base = re.sub(r"(?:연간|연도별|월간|월별|분기별|분기)", "", indicator_norm)
        indicator_base = re.sub(r"(?:증가|감소|증감|변화)(?:량|율)$", "", indicator_base)
        if not indicator_norm or not indicator_base or indicator_base not in query_norm:
            continue
        period_text = str(fields.get("period_absolute") or row.get("period_raw") or "")
        years = set(re.findall(r"(?<!\d)\d{4}(?!\d)", period_text))
        measurement = str(fields.get("measurement_type") or "")
        intent_matches = (
            measurement in {"CHANGE_RATE", "CHANGE_POINT"}
            if asks_change else measurement in {"LEVEL", "INDEX_LEVEL"}
        )
        score = (
            100 + len(indicator_base)
            + (30 if query_years and query_years == years else 0)
            + (20 if intent_matches else 0)
        )
        target = str(row.get("target_id") or row.get("value_span_id") or "")
        scored.append((score, target, row))
    if not scored:
        return [], {"contract_version": CONTRACT_VERSION, "status": "NO_MATCH", "query": query, "value_used": False}
    scored.sort(key=lambda value: (-value[0], value[1]))
    best = scored[0]
    return [best[2]], {
        "contract_version": CONTRACT_VERSION,
        "status": "SELECTED",
        "query": query,
        "target_id": best[1],
        "score": best[0],
        "candidate_count": len(scored),
        "query_intent": "CHANGE" if asks_change else "LEVEL",
        "user_intent_sha256": (user_intent or {}).get("sha256"),
        "value_used": False,
    }


def _norm(value: Any) -> str:
    return re.sub(r"[\s\-_./:(),]+", "", str(value or "")).casefold()


__all__ = [
    "CONTRACT_VERSION", "extract_source_terms", "infer_profile_units", "reranker_query",
    "select_query_target", "source_sentence",
]
