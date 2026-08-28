# -*- coding: utf-8 -*-
"""판정 출력 — 표 선택 결과를 6분류 판정 + 근거문으로 조립한다.

6분류: 일치 / 반올림-일치 / 개정-확인 / 개정-추정 / 정의불일치 / 불일치.
근거문에는 판정과 함께 수치·좌표·시점, 그리고 값이 동일하게 확인된 형제 표가
여럿이면 그 목록을, 개정·반올림 관계면 그 표기를 명시한다.
"""
from __future__ import annotations


def _kosis_url(table_key: str) -> str:
    """orgId:tblId → 통계표 열람 URL(결정론 생성)."""
    if not table_key or ":" not in table_key:
        return ""
    org, tbl = table_key.split(":", 1)
    return f"https://kosis.kr/statHtml/statHtml.do?orgId={org}&tblId={tbl}"


def build_output(select_result: dict, sibling_tables: list[str] | None = None) -> dict:
    """표 선택 결과 → 최종 출력 dict.

    반환: {"verdict","numeric","evidence","table","url","siblings"}.
    """
    verdict = select_result.get("verdict")
    table = select_result.get("table")
    matched = select_result.get("matched") or {}
    periods = select_result.get("periods")

    # 매칭 상세를 근거 문장으로 — 토큰별 [공식값·시점·대조방식]
    parts = []
    for tkn, info in matched.items():
        itm_nm, labels, per, official, how = info
        parts.append(f"주장 {tkn} vs 공식 {official} (항목 {itm_nm}, {'·'.join(labels)}, {per}, {how})")
    evidence = " / ".join(parts) if parts else "값 재현 없음"

    # 표 선택 verdict → 6분류 판정 라벨
    label = {
        "완전": "일치",
        "부분": "부분일치(일부 수치 미확인)",
        "개정-추정": "개정-추정(속보치 대비 공식 개정 가능성)",
        "약한증거": "판단보류(약한 증거 — 표명 근거 부족)",
        "불가": "불일치 또는 판단보류",
        "시점미해결": "판단보류(시점 해석 실패)",
        "수치토큰없음": "판단보류(검증 수치 없음)",
    }.get(verdict, "판단보류")

    out = {
        "verdict": label,
        "raw_verdict": verdict,
        "numeric": evidence,
        "table": table,
        "url": _kosis_url(table or ""),
        "periods": periods,
        "siblings": sibling_tables or [],
    }
    # 값이 동일 확인된 형제가 여럿이면 근거문에 명시(근거 투명화)
    if sibling_tables and len(sibling_tables) > 1:
        out["note"] = f"이 수치는 같은 통계 가족의 표 {len(sibling_tables)}개에서 동일하게 확인됨"
    return out
