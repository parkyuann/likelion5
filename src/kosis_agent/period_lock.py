# -*- coding: utf-8 -*-
"""시점 잠금 — 주장 문장 + 발행일 → 비교 시점(주기, 대상, 기준) 확정.

값순회가 "그 시점의 셀"만 조회하도록 비교 시점을 먼저 잠근다. 시점을 잠그지
않으면 다른 연도의 우연히 같은 숫자에 오매칭되므로, 이 단계가 값 검증의 전제다.

발행일은 외부에서 주입받는다(기사 메타의 작성일). 상대 시점 표현
("지난달"·"올해 1분기"·"작년 3분기")을 발행일 기준 절대 시점으로 해석한다.
발행일 미상 시에만 코퍼스 연도로 폴백한다.
"""
from __future__ import annotations
import re

# 상대 시점 해석기(기존 모듈) — 발행일 기준 지난달/N월/작년/분기 정규화
from period_resolver import resolve_period

_CORPUS_YEAR = "2025"  # 발행일 미상 시 최후 폴백 연도(수집 데이터 범위)


def lock_periods(claim: str, pub_date: str | None) -> tuple[str, str, str] | None:
    """(주기 코드, 대상 시점, 기준 시점) 또는 None(시점 해석 실패).

    주기 코드: "Y"(연) / "Q"(분기) / "M"(월).
    시점 문자열: 연="YYYY", 분기="YYYYQ"(예 202501=25년 1분기), 월="YYYYMM".
    """
    pub = pub_date or None
    ov = resolve_period(claim, pub or "")

    # 명시 연도·분기가 있으면 발행일 없이도 해석(더미 날짜 허용) — 단 순수 상대어면 금지
    if ov is None and re.search(r"20\d{2}\s*년|분기", claim):
        if pub is not None or not re.search(r"지난\s*달|지난달|전월|이달", claim):
            ov = resolve_period(claim, f"{_CORPUS_YEAR}-06-15")
    if not ov:
        return None

    # 배경 연도 함정 보정: "작년/재작년 N분기" 는 발행 연도 기준 상대
    mrq = re.search(r"(재작년|작년|지난해)\s*([1-4])\s*분기", claim)
    if mrq and pub:
        ay = int(pub[:4])
        ty = ay - (2 if mrq.group(1) == "재작년" else 1)
        qq = int(mrq.group(2))
        ov = {"prd_se": "Q", "target_period": f"{ty}{qq:02d}", "base_period": f"{ty-1}{qq:02d}"}

    # 전분기 비교: "올해 M분기 ... 지난해 N분기보다" → 대상 올해M, 기준 작년N
    cmpq = re.search(r"올해\s*([1-4])\s*분기", claim)
    baseq = re.search(r"(?:지난해|작년)\s*([1-4])\s*분기\s*(?:보다|대비|에\s*비해)", claim)
    if cmpq and baseq:
        ay = int(pub[:4]) if pub else int(_CORPUS_YEAR)
        ov = {"prd_se": "Q", "target_period": f"{ay}{int(cmpq.group(1)):02d}",
              "base_period": f"{ay-1}{int(baseq.group(1)):02d}"}

    # "지난 N분기"(지난해 아님) = 발행 연도의 N분기, 기준 전년 동분기
    lq = re.search(r"지난\s+([1-4])\s*분기", claim) or re.search(r"지난\s*([1-4])\s*분기\s*\(", claim)
    if lq and pub and not mrq and not (cmpq and baseq):
        ay = int(pub[:4]); qq = int(lq.group(1))
        if (qq * 3) > int(pub[5:7]):   # 그 분기가 발행 시점에 아직 안 끝났으면 전년도
            ay -= 1
        ov = {"prd_se": "Q", "target_period": f"{ay}{qq:02d}", "base_period": f"{ay-1}{qq:02d}"}

    return ov["prd_se"], str(ov["target_period"]), str(ov["base_period"])


def year_upgrade_needed(claim: str, prd_se: str) -> bool:
    """'연말/말 기준' 주장이 월·분기로 잠겼을 때 연간표 승급 재시도가 필요한지."""
    return prd_se in ("M", "Q") and bool(re.search(r"말\s*기준|연말", claim))
