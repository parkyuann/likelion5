# -*- coding: utf-8 -*-
"""판정 근거 생성 — RAG Reasoning으로 결정론 판정 결과를 자연어 근거로 정리한다.

판정·수치는 결정론 엔진이 확정한 값만 사용하고(변경 금지), RAG는 표현만 담당한다.
프로젝트 원칙 "설명은 KOSIS 원자료(수치·시점·출처) 근거 안에서만 생성" 준수:
확정 사실(표·항목·시점·실측값·오차·판정)을 참조문서로 주입하고, 검색·재판정을 금지한다.

출력 형식: ① KOSIS 공식값 vs 기사값(사실 제시) ② 차이 원인 추정(개정치·반올림 등)
          ③ 해당 수치의 KOSIS 링크·경로 안내.

안전장치: 판정 뒤집기·환각 수치 검출 시 결정론 근거문으로 폴백(무중단).
표선택용 tool-calling(agentic) 패턴은 사실 날조·판정 뒤집기를 유발하므로 쓰지 않고,
확정 사실을 프롬프트에 직접 주입하는 단일 제약 호출만 사용한다.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]   # 프로젝트 루트(.env 위치)
RAG_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/rag-reasoning"

_SYS = (
    "너는 통계 사실검증 결과를 독자에게 정리해 전달하는 역할이다. 스스로 새로 판정하거나 "
    "사실을 검색·추론하지 마라. 아래 제공된 '사실(참조문서)'의 숫자·지표·경로만 사용하고, "
    "참조에 없는 수치·지표를 지어내지 마라. 다음 순서의 3요소를 자연스러운 한 문단으로 써라: "
    "① KOSIS 공식값은 ○○이고 기사에는 ○○로 나왔다(두 값을 사실 그대로 제시). "
    "② 그 차이는 [원인추정]에 제시된 사유(개정치 반영·기사 반올림 등)로 보인다고 추정 어조로. "
    "③ 해당 수치는 KOSIS의 [확인경로]에서(링크가 있으면 링크로) 확인할 수 있다. "
    "값을 재현하지 못한 경우엔 ① 대신 '공식 수치를 확인/재현하지 못했다'고 밝히고 경로만 안내하라. "
    "머리말·번호·'생각:'·JSON 없이 문단만. 말줄임표(…, ...)를 쓰지 말고 각 문장은 마침표(.)로 끝맺어라."
)

# 판정별 금지어 — 생성 근거가 확정 판정을 뒤집으면 폐기(폴백)
_CONTRADICT = {
    "불가": ["사실에 부합", "사실로 확인", "일치합니다", "맞습니다", "사실입니다"],
    "약한증거": ["사실에 부합", "명확히 일치", "확실히 일치"],
    "부분": ["완전히 일치", "정확히 일치"],
}


def _api_key() -> str:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    key = (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("HCX_API_KEY 필요")
    return key


def _headers() -> dict:
    return {"Authorization": f"Bearer {_api_key()}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
            "Content-Type": "application/json"}


def _post(messages: list, max_tokens: int = 1024) -> dict:
    # tool 없이 단일 호출(toolChoice=none) — agentic 루프의 사실 날조를 피한다.
    body = {"messages": messages, "toolChoice": "none", "maxTokens": max_tokens}
    res = requests.post(RAG_URL, headers=_headers(), json=body, timeout=90)
    data = res.json()
    if res.status_code != 200:
        raise RuntimeError(f"RAG Reasoning 오류(status={res.status_code}): {data.get('status')}")
    return data.get("result") or {}


def kosis_url(table_key: str) -> str:
    """table_key(예: 301:DT_404Y016) → KOSIS 통계표 링크."""
    if not table_key or ":" not in table_key:
        return ""
    org, tbl = table_key.split(":", 1)
    return f"https://kosis.kr/statHtml/statHtml.do?orgId={org}&tblId={tbl}"


def _numbers(s: str) -> set:
    return set(re.findall(r"\d+(?:\.\d+)?", s or ""))


def _clean(s: str) -> str:
    """말줄임표(…/...)·중복 마침표를 하나의 마침표로 정리."""
    s = s.replace("…", ".").replace("⋯", ".")
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"\s+\.", ".", s)
    s = re.sub(r"\.(?=\S)(?![0-9])", ". ", s)
    return s.strip()


def _cause_hint(claim_val, official_val, claim_text: str) -> str | None:
    """차이 원인을 결정론으로 추정(모델 자의 판단 최소화용 힌트)."""
    if claim_val is None or official_val is None or official_val == 0:
        return None
    cv, ov = float(claim_val), float(official_val)
    rel = abs(cv - ov) / abs(ov)
    approx = any(w in (claim_text or "") for w in ["가량", "약", "안팎", "정도", "여 "])
    dec = len(str(claim_val).split(".")[1]) if "." in str(claim_val) else 0
    if abs(round(ov, dec) - cv) < 10 ** (-dec) / 2 + 1e-9 or approx:
        return f"기사가 공식값 {ov}을(를) '{cv}'로 반올림·근사 표기한 것으로 추정(표현상 반올림)."
    if rel <= 0.005:
        return f"공식 {ov}과 기사 {cv}의 차이가 매우 근소({round(rel*100,3)}%) — 속보치→개정치 반영 또는 표기 반올림으로 추정."
    if rel <= 0.12:
        return f"공식 {ov}과 기사 {cv}의 차이({round(rel*100,2)}%)는 속보치와 공식 개정치 차이(개정)로 추정."
    return f"공식 {ov}과 기사 {cv}의 차이({round(rel*100,2)}%)가 커 정의·범위 차이 가능성."


def _fact_docs(f: dict) -> list[dict]:
    """결정론 확정 사실 → RAG 참조문서."""
    docs = [
        {"id": "verdict", "doc": f"확정 판정(변경 금지): {f['verdict_label']} (6분류: {f['raw_verdict']})"},
        {"id": "claim", "doc": f"기사 주장: {f['claim']} / 발행일 {f.get('pub_date','')}"},
    ]
    if f.get("indicator"):
        docs.append({"id": "indicator", "doc": f"지표(무엇을 세는 통계인지): {f['indicator']}"})
    if f.get("table_key"):
        docs.append({"id": "table", "doc": f"KOSIS 표: {f.get('table_name','')} ({f['table_key']}), 출처 KOSIS"})
    if f.get("periods"):
        docs.append({"id": "period", "doc": f"대상 시점: {f['periods']}"})
    docs.append({"id": "evidence", "doc": f"실측 대조: {f.get('evidence','값 재현 없음')}"})
    ch = _cause_hint(f.get("claim_val"), f.get("official_val"), f.get("claim", ""))
    if ch:
        docs.append({"id": "원인추정", "doc": ch})
    url = f.get("kosis_url") or kosis_url(f.get("table_key", ""))
    path = f.get("nav_path", "")
    if url or path:
        docs.append({"id": "확인경로", "doc": f"KOSIS 링크: {url} · 경로: {path}".strip(" ·")})
    return docs


def generate(facts: dict) -> dict:
    """확정 사실 → 판정 근거. 반환 {rationale, source, citations}.

    실패·환각·판정뒤집기 시 결정론 근거문(facts['evidence'])으로 폴백한다.
    근거 끝에는 값이 실제로 조회된 통계표(table_key)의 KOSIS 링크를 결정론으로 덧붙인다
    (모델이 링크를 의역·누락해도 실제 URL을 보장. 표가 없으면 링크 없음).
    """
    # 값이 재현된(=값이 실제 있는) 표의 링크만 붙인다.
    url = facts.get("kosis_url") or kosis_url(facts.get("table_key", ""))

    def finalize(text: str, source: str, cites: list) -> dict:
        text = _clean(text)
        if url and url not in text:
            text = f"{text}\n출처: KOSIS 통계표에서 확인 → {url}"
        return {"rationale": text, "source": source, "citations": cites}

    fallback = facts.get("evidence") or "값 재현 없음"
    try:
        docs = _fact_docs(facts)
        body = "\n".join(f"- [{d['id']}] {d['doc']}" for d in docs)
        user = (
            "아래는 결정론 엔진이 이미 확정한 사실검증 결과다. 너는 판정 근거를 설명만 한다.\n"
            "새 사실을 검색·추론하지 말고 판정을 바꾸지 마라. 아래 사실에 없는 수치·지표는 쓰지 마라.\n"
            "'생각:'·'도구 실행'·JSON 형식을 쓰지 말고 자연스러운 한 문단만 출력하라.\n\n"
            f"[확정 사실]\n{body}\n\n"
            f"위 확정 판정('{facts['verdict_label']}')이 왜 그렇게 나왔는지 2~4문장으로 설명하라."
        )
        msgs = [{"role": "system", "content": _SYS}, {"role": "user", "content": user}]
        content = ((_post(msgs).get("message") or {}).get("content") or "").strip()
        if not content:
            return finalize(fallback, "fallback:빈응답", [])
        for bad in _CONTRADICT.get(facts.get("raw_verdict", ""), []):
            if bad in content:
                return finalize(fallback, f"fallback:판정뒤집기('{bad}')", [])
        # 환각 검사: 사실 수치 + 파생차이 + 연도조각만 허용, 유의미 수치만 검사
        fact_nums = _numbers(" ".join(d["doc"] for d in docs))
        floats = []
        for n in fact_nums:
            try:
                floats.append(float(n))
            except ValueError:
                pass
        allowed = set(fact_nums)
        for n in list(fact_nums):
            if len(n) == 6 and n.startswith("20"):
                allowed.add(n[:4]); allowed.add(str(int(n[4:6])))
        for i in range(len(floats)):
            for j in range(len(floats)):
                if i != j:
                    d = round(abs(floats[i] - floats[j]), 3)
                    allowed.add(str(d)); allowed.add(str(round(d, 2)))
        # URL에 든 숫자(orgId·tblId 등)는 허용(결정론 링크라 환각 아님)
        allowed |= _numbers(url)
        suspicious = {g for g in _numbers(content) if ("." in g or len(g) >= 3) and g not in allowed}
        if suspicious:
            return finalize(fallback, f"fallback:환각숫자{sorted(suspicious)}", [])
        return finalize(content, "rag-reasoning", [d["id"] for d in docs])
    except Exception as e:
        return finalize(fallback, f"fallback:{e}", [])


def _first_matched(matched: dict) -> tuple:
    """matched(token→(itm_nm,labels,per,official,how))에서 첫 (주장값, 공식값)."""
    for token, info in (matched or {}).items():
        try:
            official = float(str(info[3]).replace(",", ""))
            claim_v = float(str(token).replace(",", "").rstrip("%"))
            return claim_v, official
        except (ValueError, IndexError, TypeError):
            continue
    return None, None


def from_select_result(claim: str, pub_date: str | None, select_result: dict,
                       name_of=None, nav_path: str = "", indicator: str = "") -> dict:
    """표 선택 결과(select_table 반환) → 판정 근거.

    select_result: {verdict, table, matched, periods, ...}. name_of(table_key)->표명(선택).
    """
    from verdict_output import build_output
    out = build_output(select_result, sibling_tables=None)
    table = select_result.get("table") or ""
    matched = select_result.get("matched") or {}
    claim_v, official_v = _first_matched(matched)
    facts = {
        "claim": claim, "pub_date": pub_date or "",
        "raw_verdict": select_result.get("verdict") or out.get("raw_verdict") or "",
        "verdict_label": out.get("verdict") or "",
        "indicator": indicator,
        "table_key": table,
        "table_name": (name_of(table) if (name_of and table) else ""),
        "periods": select_result.get("periods") or "",
        "claim_val": claim_v, "official_val": official_v,
        "evidence": out.get("numeric") or "값 재현 없음",
        "nav_path": nav_path,
    }
    return generate(facts)
