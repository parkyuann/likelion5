# -*- coding: utf-8 -*-
"""전체 파이프라인 단일 진입점.

세 가지 입력을 하나의 명령으로 처리한다.
  기사 원문 : python run_pipeline.py --article 기사.txt --date 2025-02-19
  주장 문장 : python run_pipeline.py --claim "작년 11월 출생아 수는 2만95명…" --date 2024-12-22
  자연어질의: python run_pipeline.py --query "2024년 출생아 수 몇 명이야?"

기사/주장 → 검색 → pool 합집합 → 표 선택(값순회·개정 티어) → 판정+근거문.
질의 → 분류 → MCP 사다리(자율→표힌트→자체 폴백).

앞단(기사 원문 → 주장 추출·구조화)은 별도 워크트리(ann)에서 고도화한 모듈을
연결해 수행한다.
"""
from __future__ import annotations
import sys
import json
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent   # src

# 모듈 경로: kosis_agent·kosis_mcp는 폴더 내 모듈을 직접, kosis_retriever는 패키지로 참조한다.
# src를 최우선에 두어 config·kosis_retriever 패키지가 정확히 해석되게 한다(kosis_retriever/config와 미충돌).
sys.path.insert(0, str(HERE / "kosis_agent"))
sys.path.insert(0, str(HERE / "kosis_mcp"))
sys.path.insert(0, str(HERE))

from config import CATALOG
from table_select import select_table
from verdict_output import build_output
from kosis_retriever.pool import build_pool

_CATALOG = {"sid": {}, "name": {}, "by_sid": {}, "path": {}}


def _core(k: str) -> str:
    t = str(k).split(":", 1)[-1]
    return t.split("_", 1)[1] if "_" in t else t


def load_catalog() -> None:
    """카탈로그를 (stat_id / 표명 / 통계군별 표목록 / 분류경로) 인덱스로 적재."""
    if _CATALOG["sid"]:
        return
    with CATALOG.open(encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            tk = str(row.get("table_key") or "")
            if not tk:
                continue
            sid = str(row.get("stat_id") or "")
            _CATALOG["sid"][tk] = sid
            _CATALOG["name"][tk] = str(row.get("tbl_name") or "")
            _CATALOG["path"][tk] = row.get("primary_path") or []
            _CATALOG["by_sid"].setdefault(sid, []).append(tk)


def sid_of(tk: str) -> str:
    return _CATALOG["sid"].get(tk, "")


def name_of(tk: str) -> str:
    return _CATALOG["name"].get(tk, "")


def nav_path_of(tk: str) -> str:
    """통계표까지의 KOSIS 탐색 경로 문자열(근거문 안내용)."""
    p = _CATALOG["path"].get(tk) or []
    return "KOSIS 국내통계 > 주제별통계 > " + " > ".join(p) if p else ""


def siblings_of(sid: str) -> list[str]:
    return _CATALOG["by_sid"].get(sid, [])


def search(claim: str, top_k: int = 90) -> list[str]:
    """검색단 호출 — 6경로 하이브리드 검색기(정식 kosis_retriever)."""
    from kosis_retriever.retriever import retrieve
    return [h["table_key"] for h in retrieve(claim, top_k=top_k)]


def factcheck_claim(claim: str, pub_date: str | None, contract: list | None = None,
                    with_rationale: bool = False) -> dict:
    """주장 한 건 사실검증: 검색 → pool → 표 선택 → 판정+근거문.

    with_rationale=True면 RAG Reasoning으로 판정 근거를 자연어로 덧붙인다(실패 시 결정론 근거 유지).
    """
    load_catalog()
    if contract is None:
        from axis_contract import extract_contract_expanded
        contract = extract_contract_expanded(claim)
    hits = search(claim)
    pool = build_pool(hits, sid_of, siblings_of)
    result = select_table(claim, pub_date, pool, sid_of, name_of, contract)
    # 동률 형제(값이 동일 확인된 표) 목록은 값 검증으로 확정된 것만 채운다.
    # 통계군 전체(family)를 넣으면 오해를 주므로 넣지 않는다.
    out = build_output(result, sibling_tables=None)
    if with_rationale:
        try:
            from rag_rationale import from_select_result
            tbl = result.get("table") or ""
            r = from_select_result(claim, pub_date, result, name_of, nav_path_of(tbl))
            out["rationale"] = r["rationale"]
            out["rationale_source"] = r["source"]
        except Exception:
            pass  # 근거 생성 실패는 판정에 영향 없음(결정론 근거문 유지)
    return out


def answer_natural_query(query: str, pub_date: str | None = None) -> dict:
    """자연어 질의: 분류 → MCP 사다리(3차 폴백 = factcheck_claim)."""
    # MCP 자율 질의에는 로컬 검색 카탈로그가 필요 없다. 자체 검증 폴백이
    # 선택될 때 factcheck_claim()이 필요한 카탈로그를 지연 로딩한다.
    from query_router import classify
    if classify(query) == "route_out_of_scope":
        return {"stage": "out_of_scope", "answer": "공식 통계로 답하기 어려운 질의입니다(전망·추천 등)."}
    from ladder import answer_query
    return answer_query(
        question=query, claim_for_check=query, pub_date=pub_date,
        our_table=None, contract=[],
        self_fallback_fn=lambda: factcheck_claim(query, pub_date),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--article"); ap.add_argument("--claim")
    ap.add_argument("--query"); ap.add_argument("--date", default=None)
    ap.add_argument("--rationale", action="store_true", help="RAG Reasoning 판정 근거 생성")
    a = ap.parse_args()

    if a.query:
        out = answer_natural_query(a.query, a.date)
    elif a.claim:
        out = factcheck_claim(a.claim, a.date, with_rationale=a.rationale)
    elif a.article:
        text = Path(a.article).read_text(encoding="utf-8")
        # 앞단(주장 추출)은 별도 모듈 — 여기서는 원문을 주장으로 직접 검증(앞단 미연결 시)
        out = factcheck_claim(text.strip(), a.date, with_rationale=a.rationale)
    else:
        ap.error("--article / --claim / --query 중 하나가 필요합니다")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
