"""agent_reason.py — LLM 표선택 = NCP 리랭커(재정렬) + RAG Reasoning(최종 1개 선택).

원래 agent_rerank.py(리랭커)와 agent_reason.py(RAG)로 나뉘어 있던 걸, 둘 다 NCP LLM으로
표 후보를 좁히는 한 단위라 여기 합쳤다(2026-07-28). 라이브 표선택에서 함께 돈다:
  하이브리드/getMeta로 좁힌 후보 → rerank()로 재정렬 → choose_table()로 최종 1개+근거.

API(공식문서·실측으로 확정):
  리랭커  POST /v1/api-tools/reranker      요청 {query, documents:[{id,doc}]} → result.citedDocuments
  RAG    POST /v1/api-tools/rag-reasoning  1단계 tools+auto → toolCalls, 2단계 role=tool 주입 + toolChoice=none
  ※ RAG는 tools와 maxTokens 동시 사용 불가(400).
  ※ 리랭커·RAG 모두 doc에 항목·수록기간까지 넣고 "수치는 이후 조회"라고 알려줘야 후보를 기각 안 한다.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG = PROJECT_ROOT / "data" / "kosis_catalog_enriched_sample600.jsonl"
RERANK_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/reranker"
RAG_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/rag-reasoning"

_DOC_CACHE: dict[str, dict] | None = None

_TOOL = {
    "type": "function",
    "function": {
        "name": "search_kosis",
        "description": "KOSIS 통계표 메타데이터 검색",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

_PROMPT = (
    "검색 결과는 KOSIS 통계표의 메타데이터(표이름·분류·항목·수록기간)다. "
    "실제 수치는 이후 KOSIS API로 조회하므로, 지금 수치가 보이지 않는 것은 전혀 문제가 아니다. "
    "질문이 묻는 '지표(무엇을 세는 통계인지)'를 다루는 표를 후보 중에서 하나 고르고, "
    "그 표의 id를 반드시 그대로 적은 뒤 근거를 한 문장 덧붙여라. "
    "후보 전부가 질문 주제와 명백히 무관할 때만 '없음'이라고 답하라. 질문: {question}"
)


# ── 공통 ────────────────────────────────────────────────────────────────
def _api_key() -> str:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    key = (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY 필요")
    return key


def _catalog_docs() -> dict[str, dict]:
    """table_key → 카탈로그 레코드 전체. 최초 1회 로드."""
    global _DOC_CACHE
    if _DOC_CACHE is None:
        _DOC_CACHE = {}
        with CATALOG.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    _DOC_CACHE[r["table_key"]] = r
    return _DOC_CACHE


def _doc_text(cand: dict) -> str:
    """모델에 넣을 표 설명. 표명·분류경로·차원에 더해 **항목·수록기간**까지 붙인다.

    항목/기간이 없으면 모델이 "그 해 수치가 없다"며 후보를 전부 기각해버리므로,
    주제 적합성을 판단할 재료를 최대한 준다.
    """
    tk = cand.get("table_key", "")
    r = _catalog_docs().get(tk)
    if not r:
        return cand.get("tbl_name") or tk
    parts = [r.get("doc_meta_text") or r.get("tbl_name") or tk]
    items = [i.get("itm_nm") for i in (r.get("items") or []) if i.get("itm_nm")]
    if items:
        parts.append("항목: " + ", ".join(items[:12]))
    dims = [d.get("obj_nm") for d in (r.get("dimensions") or []) if d.get("obj_nm")]
    if dims:
        parts.append("분류: " + ", ".join(dims))
    period = r.get("period_types") or []
    latest = r.get("latest_period")
    if period or latest:
        parts.append(f"수록: {'/'.join(period)} ~ {latest or ''}".strip())
    return " | ".join(parts)


# ── 리랭커(재정렬) ──────────────────────────────────────────────────────
def build_retrieval_query(question: str) -> str:
    """리랭커에 넣을 '표 검색용' 질의를 만든다.

    실측(2026-07-23): 원문 사실질문("~2024년 전년보다 늘었나?")을 그대로 넣으면 리랭커가
    수치 없는 메타데이터로 답을 못 만들어 citedDocuments=0. 지표 키워드만 남기고
    '통계표 주제:'로 감싸면 인용이 정상적으로 나오고 정답 표가 1위로 올라온다.
    """
    try:
        from keyword_extractor import retrieval_keywords
        kws = retrieval_keywords(question)      # 불용어 + 카탈로그 어휘 필터까지 적용
    except Exception:
        kws = []
    return "통계표 주제: " + (" ".join(kws) if kws else question)


def rerank(query: str, candidates: list[dict], top_k: int | None = None,
           verbose: bool = False) -> list[dict]:
    """후보를 리랭커 관련도 순으로 재정렬하고, top_k개로 좁혀 반환.

    citedDocuments 순서를 새 랭킹으로 쓰고, 인용 안 된 후보는 기존(RRF) 순서로 뒤에 붙인다.
    인용이 0건이면 **재정렬이 없었다는 사실을 명시적으로 알린다**(조용히 원순서로 폴백해
    "리랭커가 동의한 것"처럼 보이는 착시를 막기 위함).
    """
    if not candidates:
        return []
    documents = [{"id": c["table_key"], "doc": _doc_text(c)} for c in candidates]
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    body = {"query": query, "documents": documents}
    res = requests.post(RERANK_URL, headers=headers, json=body, timeout=30)
    data = res.json()
    if res.status_code != 200:
        raise RuntimeError(f"리랭커 API 오류(status={res.status_code}): {data.get('status')}")
    cited = (data.get("result") or {}).get("citedDocuments") or []
    order = [d.get("id") for d in cited if d.get("id")]

    by_key = {c["table_key"]: c for c in candidates}
    ranked = [by_key[k] for k in order if k in by_key]          # 리랭커가 매긴 순서
    ranked += [c for c in candidates if c["table_key"] not in order]  # 나머지는 기존 순서로

    if verbose:
        if order:
            print(f"  [리랭커] {len(candidates)}개 → 인용 {len(order)}개로 재정렬")
        else:
            print(f"  [리랭커] 인용 0건 — 재정렬 없음(RRF 순서 유지)", file=sys.stderr)
    return ranked[:top_k] if top_k else ranked


# ── RAG Reasoning(최종 선택) ────────────────────────────────────────────
def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _post(messages: list, tool_choice: str, max_tokens: int | None = None) -> dict:
    # tools와 maxTokens는 동시 사용 불가(400). 그래서 최종 답변 단계(toolChoice="none")에선
    # tools를 빼고 maxTokens로 답변 길이를 늘린다(근거 문장이 잘리지 않게).
    body: dict = {"messages": messages, "toolChoice": tool_choice}
    if tool_choice == "none":
        if max_tokens:
            body["maxTokens"] = max_tokens
    else:
        body["tools"] = [_TOOL]
    res = requests.post(RAG_URL, headers=_headers(), json=body, timeout=90)
    data = res.json()
    if res.status_code != 200:
        raise RuntimeError(f"RAG Reasoning 오류(status={res.status_code}): {data.get('status')}")
    return data.get("result") or {}


def choose_table(question: str, candidates: list[dict], verbose: bool = False) -> tuple[str | None, str]:
    """후보 중 최종 표 1개를 고른다. 반환 (table_key | None, 근거문장)."""
    if not candidates:
        return None, "후보가 없습니다."

    msgs = [{"role": "user", "content": _PROMPT.format(question=question)}]
    step1 = _post(msgs, "auto")
    assistant = step1.get("message") or {}
    calls = assistant.get("toolCalls") or []

    if calls:
        payload = {"search_result": [
            {"id": c["table_key"], "doc": _doc_text(c)} for c in candidates
        ]}
        msgs2 = msgs + [assistant, {
            "role": "tool",
            "content": json.dumps(payload, ensure_ascii=False),
            "toolCallId": calls[0]["id"],
        }]
        step2 = _post(msgs2, "none", max_tokens=4096)   # 재검색 루프 방지 + 답변 길이 확보(근거 안 잘리게)
        content = (step2.get("message") or {}).get("content") or ""
    else:
        content = assistant.get("content") or ""

    content = content.strip()
    if verbose:
        print(f"  [RAG Reasoning] {content}")

    # 선택 파싱 — 모델이 id를 안 쓰고 표 이름으로만 답하는 경우가 잦아 3단계로 확인한다.
    for c in candidates:                                   # ① 전체 id (101:DT_104Y260)
        if c.get("table_key") and c["table_key"] in content:
            return c["table_key"], content
    for c in candidates:                                   # ② tbl_id만 (DT_104Y260)
        tbl_id = (c.get("table_key") or "").split(":")[-1]
        if tbl_id and tbl_id in content:
            return c["table_key"], content
    for c in candidates:                                   # ③ 표 이름
        name = (c.get("tbl_name") or "").strip()
        if name and name in content:
            return c["table_key"], content
    return None, content                       # 못 고름 → 상위에서 UNVERIFIABLE
