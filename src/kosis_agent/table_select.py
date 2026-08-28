# -*- coding: utf-8 -*-
"""표 선택 — 후보 pool에서 정답 표 하나를 확정한다.

원리: 확실한 것은 결정론이 즉시 잡고, 애매한 것은 값 검증이 확정한다.
  [1] 메타 선별(구조확정·모델 다수결·보정)로 1차 후보(챔피언) 순서 결정 — 외부 입력
  [2] 값순회: 발행일로 시점을 잠근 뒤 [챔피언 → 챔피언 형제 → pool 순서]로 표를
      열어(getData) 기사 숫자가 재현되는 표를 증거로 확정
  [3] 약한 증거 가드: %값 하나뿐인 주장은 표명 근거 있는 표만 확정
  [4] 개정-추정: 정확 일치가 전무할 때만 근사 재순회로 개정 케이스 회수

값은 표 고르기용 스크리닝 증거로만 쓰며, 최종 판정은 판정 모듈이 다시 수행한다.
"""
from __future__ import annotations
import re

from period_lock import lock_periods, year_upgrade_needed
from axis_binding import claim_numbers, is_weak_claim, verify_table
from config import (WALK_BUDGET, FAMILY_CAP, TOLERANCE_REVISION)


def _core(k: str) -> str:
    t = str(k).split(":", 1)[-1]
    return t.split("_", 1)[1] if "_" in t else t


def _name_supported(claim: str, table_name: str) -> bool:
    """표명 핵심 토큰(한글 4자+)이 기사에 등장하는지 — 약한 증거 가드용."""
    cc = re.sub(r"[\s()·,]", "", claim)
    if "GDP" in claim:
        cc += "국내총생산"
    toks = [t for t in re.split(r"[^가-힣]+", table_name or "") if len(t) >= 4]
    if not toks:
        return True
    return any(re.sub(r"[\s()·,]", "", t) in cc or re.sub(r"[\s()·,]", "", t)[:-1] in cc for t in toks)


def select_table(claim: str, pub_date: str | None, champion_order: list[str],
                 sid_of, name_of, contract: list) -> dict:
    """표 선택 실행.

    champion_order: 메타 선별로 정렬된 후보 표 키 목록(챔피언이 앞).
    sid_of(table_key)->stat_id, name_of(table_key)->표명: 카탈로그 조회 함수.
    반환: {"verdict","table","matched","periods"} — verdict ∈
          {완전, 부분, 약한증거, 개정-추정, 불가, 시점미해결, 수치토큰없음}.
    """
    lock = lock_periods(claim, pub_date)
    if not lock:
        return {"verdict": "시점미해결", "table": None, "matched": {}, "periods": None}
    prd_se, target, base = lock
    nums = claim_numbers(claim)
    if not nums:
        return {"verdict": "수치토큰없음", "table": None, "matched": {}, "periods": lock}

    # 순회 대상: [챔피언 → 챔피언 형제(≤6) → pool 순서]
    champ = champion_order[0] if champion_order else None
    fsid = sid_of(champ) if champ else ""
    family = [t for t in champion_order if fsid and sid_of(t) == fsid][:FAMILY_CAP]
    walk = list(dict.fromkeys(([champ] if champ else []) + family + champion_order))[:WALK_BUDGET]

    weak = is_weak_claim(nums)
    year_up = year_upgrade_needed(claim, prd_se)

    best = ("불가", 0, len(nums), None, {})
    best_any = ("불가", 0, len(nums), None, {})
    for tk in walk:
        n_m, n_t, mm, _md = verify_table(tk, claim, nums, prd_se, target, base, contract)
        if n_m == 0 and year_up:
            n_m, n_t, mm, _md = verify_table(tk, claim, nums, "Y", target[:4], base[:4], contract)
        v = "완전" if n_m == n_t else ("부분" if n_m else "불가")
        if n_m > best_any[1]:
            best_any = (v, n_m, n_t, tk, mm)
        # 약한 증거 주장은 표명 통과 표를 우선 트랙으로
        if weak and not _name_supported(claim, name_of(tk) or ""):
            continue
        if n_m > best[1]:
            best = (v, n_m, n_t, tk, mm)
        if best[0] == "완전":
            break

    # 약한 증거: 표명 통과 표가 전무한데 미통과 표만 재현 → '약한증거'로 강등(확정 안 함)
    if weak and best[1] == 0 and best_any[1] > 0:
        v, n_m, n_t, tk, mm = best_any
        best = ("약한증거", n_m, n_t, tk, mm)

    # 개정-추정: 정확 일치가 전무할 때만 근사 재순회
    if best[1] == 0:
        for tk in walk:
            n_m, n_t, mm, md = verify_table(tk, claim, nums, prd_se, target, base, contract,
                                            tol=TOLERANCE_REVISION)
            if n_m == 0 and year_up:
                n_m, n_t, mm, md = verify_table(tk, claim, nums, "Y", target[:4], base[:4], contract,
                                                tol=TOLERANCE_REVISION)
            enough = (n_m == n_t) or (n_t >= 2 and n_m >= n_t - 1)
            bound = any(x in ("contract", "term") for x in md) or _name_supported(claim, name_of(tk) or "")
            if n_m > 0 and enough and bound:
                best = ("개정-추정", n_m, n_t, tk, mm)
                break

    verdict, n_m, n_t, tk, mm = best
    return {"verdict": verdict, "table": tk, "matched": mm, "periods": lock,
            "reproduced": f"{n_m}/{n_t}"}
