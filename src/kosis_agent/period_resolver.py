"""period_resolver.py — 기사 발행일 기반 시점 정규화(앞단 없이).

주장 문장 + 기사 작성일(YYYY-MM-DD)로 KOSIS 조회 시점(prd_se/target/base)을 계산한다.
파서가 표 제목의 연도를 월 수치에 잘못 붙이던 버그(2025 기사인데 "지난달"을 2024로)를
발행일 기준으로 해결한다. run(period_override=resolve_period(...))로 주입.

"""
from __future__ import annotations

import re


def resolve_period(claim: str, date: str) -> dict | None:
    """기사 작성일(YYYY-MM-DD) 기준으로 주장의 시점을 정규화 → period_override dict.

    반환: {prd_se, target_period, base_period, target_year, base_year}. base는 전년동기(YoY).
    """
    if not date or len(str(date)) < 7:
        return None
    ay, am = int(str(date)[:4]), int(str(date)[5:7])
    q = claim or ""
    quarter = re.search(r"([1-4])\s*분기", q)
    m_ym = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월", q)               # 명시 연+월
    # 상대어가 '월'에 바로 붙은 경우("작년 3월")만 그 월의 연도를 상대로 — 떨어진 "작년보다"는 base 신호
    m_rel_ym = re.search(r"(재작년|작년|지난해|전년|올해|금년)\s*(\d{1,2})\s*월", q)
    m_month = re.search(r"(\d{1,2})\s*월", q)
    prev_month = bool(re.search(r"지난\s*달|전월", q))
    half = re.search(r"(상|하)반기", q)
    m_year_only = re.search(r"(20\d{2})\s*년(?!\s*\d{1,2}\s*월)", q)       # 연도만(연간)
    # 연간(월 없음) 상대 대상연도 — 발행연도(ay) 기준
    if re.search(r"재작년|지지난해", q):
        ry = ay - 2
    elif re.search(r"작년|지난해|전년", q):
        ry = ay - 1
    elif re.search(r"올해|금년|당해", q):
        ry = ay
    else:
        ry = ay

    def mk(prd: str, ty: int, sub: int | None = None) -> dict:
        tp = f"{ty}{sub:02d}" if (prd in ("M", "Q", "H") and sub) else str(ty)
        bp = f"{ty - 1}{sub:02d}" if (prd in ("M", "Q", "H") and sub) else str(ty - 1)
        return {"prd_se": prd, "target_period": tp, "base_period": bp,
                "target_year": ty, "base_year": ty - 1}

    if quarter:
        ty = int(m_year_only.group(1)) if m_year_only else ry
        return mk("Q", ty, int(quarter.group(1)))
    if half:
        ty = int(m_year_only.group(1)) if m_year_only else ry
        return mk("H", ty, 2 if half.group(1) == "하" else 1)
    if m_ym:
        return mk("M", int(m_ym.group(1)), int(m_ym.group(2)))
    if prev_month:
        mm, ty = am - 1, ay
        if mm == 0:
            mm, ty = 12, ay - 1
        return mk("M", ty, mm)
    if m_rel_ym:                          # "작년 3월" — 상대어가 월에 붙음 → 그 연도
        rel = m_rel_ym.group(1); mm = int(m_rel_ym.group(2))
        ty = ay - 2 if rel == "재작년" else (ay if rel in ("올해", "금년") else ay - 1)
        return mk("M", ty, mm)
    if m_month:                          # 붙지 않은 "N월" → 발행연도 기준(떨어진 '작년'은 base용)
        mm, ty = int(m_month.group(1)), ay
        if mm > am:                       # 1월 기사에 "12월" → 전년
            ty = ay - 1
        return mk("M", ty, mm)
    ty = int(m_year_only.group(1)) if m_year_only else ry
    return mk("Y", ty)
