# -*- coding: utf-8 -*-
"""KOSIS 공식 MCP 서버 클라이언트 — JSON-RPC / Streamable HTTP.

공식 MCP 서버에 접속해 도구 목록을 받고 도구를 호출한다(키 불필요).
서버가 SSE(text/event-stream)로 응답하며 charset을 지정하지 않아 utf-8을 강제한다.
노출 도구: 통계표 검색 · 표 구조 조회 · 데이터 조회 등.
"""
from __future__ import annotations
import json
import requests

from config import MCP_URL

_HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_rid = [0]


def _parse(resp):
    """일반 JSON 또는 SSE 이벤트 스트림에서 마지막 JSON-RPC 응답을 뽑는다."""
    resp.encoding = "utf-8"
    if "text/event-stream" in resp.headers.get("Content-Type", ""):
        events, buf = [], []
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())
            elif not line.strip() and buf:
                events.append(buf); buf = []
        if buf:
            events.append(buf)
        for b in reversed(events):
            for joined in ("\n".join(b), "".join(b)):
                try:
                    return json.loads(joined)
                except Exception:
                    continue
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _rpc(method: str, params: dict):
    _rid[0] += 1
    r = requests.post(MCP_URL, headers=_HDR,
                      json={"jsonrpc": "2.0", "id": _rid[0], "method": method, "params": params},
                      timeout=90)
    return _parse(r)


def init() -> None:
    """세션 초기화(initialize + initialized 통지)."""
    _rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "kosis-factcheck", "version": "1.0"}})
    requests.post(MCP_URL, headers=_HDR,
                  json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30)


def list_tools() -> list[dict]:
    body = _rpc("tools/list", {})
    return (body or {}).get("result", {}).get("tools", [])


def call_tool(name: str, arguments: dict) -> str:
    """도구 호출 결과의 텍스트 본문을 반환."""
    body = _rpc("tools/call", {"name": name, "arguments": arguments or {}})
    if not body:
        return "(MCP 응답 파싱 실패)"
    if "error" in body:
        return f"(MCP 오류) {json.dumps(body['error'], ensure_ascii=False)[:500]}"
    content = (body.get("result") or {}).get("content") or []
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    return "\n".join(texts) if texts else json.dumps(body.get("result"), ensure_ascii=False)


def as_function_tools(mcp_tools: list[dict]) -> list[dict]:
    """MCP 도구 스키마 → function calling 도구 스키마로 변환."""
    return [{"type": "function", "function": {
        "name": t["name"],
        "description": (t.get("description") or "")[:700],
        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
    }} for t in mcp_tools]
