"""agent_query_parser.py — 자연어 질문 → 구조화(규칙기반).

시점(연/분기/월 + 상대시점)·연산종류를 문장에서 뽑는다. 분류/항목 값 매칭은 agent_slots가
문장 원문과 대조하므로 여기선 시점·연산만 확정한다.

확장 — 분기(Q)·월(M) 주기와 "작년/전년/1년 전" 상대시점을 해석해
KOSIS PRD_DE 형식의 target_period/base_period 문자열을 만든다.
  연: '2024'  ·  분기: '202404'(=2024 4분기)  ·  월: '202412'
데이터셋이 조선일보 2025년 기사로 고정이므로 상대시점의 기준연도(REF_YEAR)는 2025로 둔다
(기사 작성일이 있으면 그 연도로 대체 가능 — ref_year 인자).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CHANGE = ("늘", "증가", "증감", "줄", "감소", "전년", "전년대비", "전년비", "전년동기")
_SHARE = ("비율", "구성비", "퍼센트", "%", "비중")
_TOTAL = ("합계", "총", "전체 합")
_RATIO = ("배",)

# 데이터셋(조선일보 2025년)의 상대시점 기준연도. "올해"=2025, "작년"=2024.
REF_YEAR = 2025


@dataclass
class ParsedQuery:
    years: list[int]
    base_year: int | None
    target_year: int | None
    op: str                 # change | value_at | share | total | ratio
    text: str
    prd_se: str = "Y"       # Y/Q/M/H/D
    target_period: str | None = None   # KOSIS PRD_DE (예: '2024', '202404', '202412')
    base_period: str | None = None


def _detect_op(q: str) -> str:
    if any(k in q for k in _CHANGE):
        return "change"
    if any(k in q for k in _SHARE):
        return "share"
    if any(k in q for k in _TOTAL):
        return "total"
    if any(k in q for k in _RATIO):
        return "ratio"
    return "value_at"


def _period_str(year: int, prd_se: str, sub: int | None) -> str:
    """연도+주기+하위번호 → KOSIS PRD_DE 문자열."""
    if prd_se in ("Q", "M", "H") and sub:
        return f"{year}{sub:02d}"
    return f"{year}"


def parse(question: str, ref_year: int = REF_YEAR) -> ParsedQuery:
    q = str(question or "")
    years = sorted(int(y) for y in re.findall(r"(20\d{2})", q))
    op = _detect_op(q)

    # 주기·하위번호(분기/월)
    prd_se, sub = "Y", None
    if "분기" in q:
        prd_se = "Q"
        m = re.search(r"([1-4])\s*분기", q)
        sub = int(m.group(1)) if m else None
    elif re.search(r"(\d{1,2})\s*월", q):
        prd_se = "M"
        m = re.search(r"(\d{1,2})\s*월", q)
        sub = int(m.group(1)) if m else None
    elif "반기" in q:
        prd_se = "H"
        sub = 2 if "하반기" in q else (1 if "상반기" in q else None)

    # 대상 연도: 절대연도 우선 → 상대시점(작년/올해/재작년) → 기본 최신(ref_year)
    if years:
        target_year = years[-1]
    elif any(w in q for w in ("재작년", "지지난해")):
        target_year = ref_year - 2
    elif any(w in q for w in ("작년", "지난해", "전년")):
        target_year = ref_year - 1
    elif any(w in q for w in ("올해", "금년", "당해")):
        target_year = ref_year
    else:
        target_year = ref_year

    base_year: int | None = None
    if op == "change":
        if len(years) >= 2:
            base_year, target_year = years[0], years[-1]
        elif any(w in q for w in ("전년", "작년", "1년 전", "1년전", "일년", "지난해", "전년동기")):
            base_year = target_year - 1
        else:
            base_year = target_year - 1

    target_period = _period_str(target_year, prd_se, sub)
    base_period = _period_str(base_year, prd_se, sub) if base_year is not None else None
    return ParsedQuery(years=years, base_year=base_year, target_year=target_year, op=op,
                       text=q, prd_se=prd_se, target_period=target_period, base_period=base_period)
