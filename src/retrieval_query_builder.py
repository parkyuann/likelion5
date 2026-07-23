"""검색 경로별 질의 문자열을 생성한다."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _value(claim: object, name: str) -> Any:
    if isinstance(claim, Mapping):
        return claim.get(name)
    return getattr(claim, name, None)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _first(claim: object, *names: str) -> str:
    for name in names:
        value = _clean(_value(claim, name))
        if value:
            return value
    return ""


def build_claim_dense_query(claim: object) -> str:
    """정규화 필드를 우선해 Claim Dense 검색 문장을 만든다."""
    fields = [
        _first(claim, "indicator_norm", "indicator_raw"),
        _first(claim, "population_norm", "population_raw"),
        _first(claim, "region_norm", "region_raw"),
        _first(claim, "claim_text"),
        _first(claim, "time_start", "time_ref_raw"),
        _first(claim, "period_type"),
        _first(claim, "unit_norm", "unit_raw"),
    ]
    return " ".join(dict.fromkeys(value for value in fields if value))


def build_hyde_input(claim: object) -> str:
    """HCX가 예상 표명을 만들 때 사용할 구조화된 입력을 만든다."""
    pairs = [
        ("지표", _first(claim, "indicator_norm", "indicator_raw")),
        ("모집단", _first(claim, "population_norm", "population_raw")),
        ("지역", _first(claim, "region_norm", "region_raw")),
        ("시점", _first(claim, "time_start", "time_ref_raw")),
        ("단위", _first(claim, "unit_norm", "unit_raw")),
        ("원문", _first(claim, "claim_text")),
    ]
    return "\n".join(f"{label}: {value}" for label, value in pairs if value)

