# -*- coding: utf-8 -*-
"""축 바인딩 + 값 검증 — 표 하나를 열어 기사 숫자가 실재하는지 확인한다.

표를 연 뒤(getMeta)에야 그 표의 축 구조를 보고 "기사의 어떤 의미가 어느 축값에
대응하는지"를 결정한다(late binding). 그 좌표의 셀을 잠긴 시점으로 조회(getData)해
기사 숫자를 직접값·증감량·증감률 세 방식으로 대조한다.

값은 "표 고르기"용 스크리닝 증거로만 쓰며, 최종 판정은 판정 모듈이 다시 수행한다.
우연 일치를 막는 가드: 시점 고정(호출부) · 단위 배율 제한 · 약한 증거 강등.
"""
from __future__ import annotations
import re
import itertools

from kosis_call_tool import fetch_meta, fetch_cells
from tolerance_judge import parse_large_number
from axis_contract import axis_role, value_satisfies

from config import (SCALES, TOLERANCE_EXACT, TOLERANCE_YOY_PP,
                    TOLERANCE_REVISION, TOLERANCE_REVISION_YOY_PP)

# 단서 없는 축에 기본 선택하는 집계 축값
AGG_LABELS = ("전국", "계", "총계", "전체", "총지수", "전 산업", "전산업", "합계", "대한민국")
MAX_COMBOS = 16  # 표당 시도하는 (축값 조합) 상한


def _norm(s: str) -> str:
    return re.sub(r"[\s()·,]", "", str(s or ""))


def _close(a: float, b: float, tol: float) -> bool:
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / max(abs(a), abs(b)) <= tol


def claim_numbers(claim: str) -> list[tuple[str, float]]:
    """주장에서 대조할 수치 토큰 추출(연도·월일·서수 노이즈 제거).

    단위가 붙은 소수는 작아도 유효(0.1%·0.82명), 무단위 소수는 3 이상만
    (연도 조각·서수 노이즈 차단).
    """
    toks = re.findall(
        r"\d[\d,.]*\s*(?:조\s*\d*[\d,.]*)?\s*(?:억\s*\d*[\d,.]*)?\s*(?:만\s*\d*[\d,.]*)?"
        r"\s*(?:원|명|가구|개|건|%|달러|톤)?", claim)
    out, seen = [], set()
    for t in (x.strip() for x in toks):
        if re.fullmatch(r"(19|20)\d{2}\s*년?", t) or re.fullmatch(r"\d{1,2}\s*(월|일|분기|분|개)?", t):
            continue
        v = parse_large_number(t.replace(",", ""))
        if v is None:
            continue
        has_unit = bool(re.search(r"%|명|가구|원|건|개|달러|톤", t))
        if (has_unit and abs(v) >= 0.01) or (not has_unit and abs(v) >= 3):
            if v not in seen:
                seen.add(v); out.append((t, float(v)))
    return out[:6]


def is_weak_claim(nums: list[tuple[str, float]]) -> bool:
    """수치가 %값 하나뿐 — 아무 지수 표에서나 우연 일치할 위험이 큰 주장."""
    return len(nums) == 1 and "%" in nums[0][0]


def _axis_options(dim_name: str, dim_values: list[tuple[str, str]],
                  claim: str, contract: list[dict]) -> tuple[list, str]:
    """한 축에서 후보 축값을 우선순위 4단으로 고른다.
       ① 계약 매칭 > ② 지표어 바인딩 > ③ 집계값 > ④ 첫값.
       contract: [{'role','value'}...] — 축 이름의 역할과 일치하는 계약만 적용.
    """
    # ① 기사에 명시된 축=값 조건과 매칭(역할 분류 + 역할별 값 충족)
    role = axis_role(dim_name or "")
    hit = []
    for c in contract:
        if c.get("role") != role:
            continue
        for cd, lb in dim_values:
            if value_satisfies(role, c["value"], lb):
                hit.append((cd, lb))
    if hit:
        return list(dict.fromkeys(hit))[:4], "contract"
    # ② 축값 라벨(2자 이상)이 기사에 그대로 등장
    cclaim = _norm(claim)
    term = [(cd, lb) for cd, lb in dim_values if len(_norm(lb)) >= 2 and _norm(lb) in cclaim]
    if term:
        return term[:4], "term"
    # ③ 집계값
    agg = [(cd, lb) for cd, lb in dim_values if lb.strip() in AGG_LABELS]
    if agg:
        return agg[:1], "agg"
    # ④ 첫값
    return dim_values[:1], "first"


def verify_table(table_key: str, claim: str, nums: list[tuple[str, float]],
                 prd_se: str, target: str, base: str,
                 contract: list, tol: float = TOLERANCE_EXACT):
    """표 하나에서 기사 숫자가 재현되는지 검증.

    반환: (재현 토큰 수, 전체 토큰 수, 매칭 상세, 축 채움 방식 목록).
    매칭 상세[토큰] = (항목명, [축값 라벨], 시점, 공식값, 대조방식).
    """
    try:
        meta = fetch_meta(table_key)
    except Exception:
        return 0, len(nums), {}, []

    dim_opts, modes = [], []
    for d in meta.dimensions:
        vals = [(v.code, v.label) for v in d.values]
        opts, mode = _axis_options(d.obj_nm or "", vals, claim, contract)
        dim_opts.append(opts); modes.append(mode)

    combos = list(itertools.product(*dim_opts))[:MAX_COMBOS]
    matched = {}
    yoy_band = TOLERANCE_YOY_PP if tol <= TOLERANCE_EXACT else TOLERANCE_REVISION_YOY_PP
    for itm_id, itm_nm, _unit in meta.items[:10]:
        for cb in combos:
            obj = {f"objL{i+1}": cd for i, (cd, _l) in enumerate(cb)}
            try:
                cells = fetch_cells(table_key, itm_id, obj, prd_se=prd_se,
                                    start=min(base, target), end=max(base, target))
            except Exception:
                cells = []
            bp = {c.period: c.value_num for c in cells if c.value_num is not None}
            vt, vb = bp.get(target), bp.get(base)
            deltas = {}
            if vt is not None and vb is not None:
                deltas["diff"] = vt - vb
                if vb:
                    deltas["yoy"] = round((vt - vb) / vb * 100, 2)
            for tkn, tv in nums:
                if tkn in matched:
                    continue
                for per, v in ((target, vt), (base, vb)):
                    if v is None:
                        continue
                    for s in SCALES:
                        if _close(abs(tv), abs(v) * s, tol):
                            matched[tkn] = (itm_nm, [l for _c, l in cb], per, v, f"x{int(s)}"); break
                    if tkn in matched:
                        break
                if tkn not in matched and "yoy" in deltas and "%" in tkn:
                    if abs(abs(tv) - abs(deltas["yoy"])) <= yoy_band:
                        matched[tkn] = (itm_nm, [l for _c, l in cb], f"{base}->{target}", deltas["yoy"], "YoY%")
                if tkn not in matched and "diff" in deltas and "%" not in tkn:
                    for s in SCALES:
                        if _close(abs(tv), abs(deltas["diff"]) * s, tol):
                            matched[tkn] = (itm_nm, [l for _c, l in cb], f"{base}->{target}", deltas["diff"], f"dx{int(s)}"); break
        if len(matched) == len(nums):
            break
    return len(matched), len(nums), matched, modes
