# -*- coding: utf-8 -*-
"""앞단(routed) → 사실검증 입력 어댑터.

앞단(별도 저장소에서 고도화)이 낸 routed 레코드(KOSIS_CANDIDATE)를 사실검증 진입점
factcheck_claim의 입력으로 번역한다.
  routed record → {claim(문장), value_text, op, period_absolute, indicator, item, dimension}

앞단 본체 코드는 이 저장소에 포함되지 않는다(연결 지점만 제공). routed 레코드는 호출자가 넘긴다.
"""
from __future__ import annotations

# 앞단 measurement_type → 검증 연산
_OP_MAP = {
    "LEVEL": "value_at",
    "CHANGE": "change", "CHANGE_RATE": "change", "CHANGE_POINT": "change",
    "SHARE": "share", "RATIO": "ratio",
}


def claims_from_routed(rows: list[dict], article_idx: str | None = None) -> list[dict]:
    """routed 레코드 목록 → 사실검증 대상 주장 목록(문장 단위 중복 제거)."""
    seen: set = set()
    out: list[dict] = []
    for r in rows:
        if r.get("routing_class") != "KOSIS_CANDIDATE":
            continue
        if article_idx is not None and str(r.get("article_idx")) != str(article_idx):
            continue
        sid = r.get("article_sentence_id")
        if sid in seen:
            continue
        seen.add(sid)
        rf = r.get("retrieval_fields") or {}
        per = rf.get("period") or {}
        meas = (per.get("measurement") or {}).get("absolute") or rf.get("period_absolute") or ""
        out.append({
            "claim": r.get("sentence_text") or "",
            "value_text": r.get("value_text") or "",
            "indicator": rf.get("indicator") or "",
            "op": _OP_MAP.get(str(rf.get("measurement_type") or "").upper(), ""),
            "period_absolute": meas,
            "item": rf.get("item") or [],
            "dimension": rf.get("dimension") or [],
        })
    return out


def factcheck_routed(rows: list[dict], pub_date: str | None, article_idx: str | None = None,
                     with_rationale: bool = False, factcheck_fn=None) -> list[dict]:
    """routed → 각 주장 사실검증 결과 리스트.

    factcheck_fn 미지정 시 run_pipeline.factcheck_claim 사용.
    반환: [{claim, frontend(앞단 구조화), verdict(판정 dict)}...].
    """
    if factcheck_fn is None:
        from run_pipeline import factcheck_claim as factcheck_fn
    results: list[dict] = []
    for c in claims_from_routed(rows, article_idx):
        res = factcheck_fn(c["claim"], pub_date, with_rationale=with_rationale)
        results.append({"claim": c["claim"], "frontend": c, "verdict": res})
    return results
