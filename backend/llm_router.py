"""HCX-005 function calling 기반 입력 라우터.

텍스트 입력을 URL / 통계 질의 / 기사 본문 / 범위 밖(out-of-scope)으로 분류한다.
URL은 LLM 이전에 정규식으로 컷하고(무료·정확), 그 외에는 HCX-005의
function calling으로 answer_with_kosis / treat_as_article / reject 중 하나를
모델이 직접 호출하게 한다. 어떤 함수를 부르느냐 = 분류, 인자 = 재구성된 질문.

HCX 호출이 실패(키 미설정·타임아웃·오류)하면 규칙 기반 route_input 으로 폴백한다.
그림 입력은 별도 엔드포인트(/v1/analyze/image)가 처리하므로 여기서 다루지 않는다.

전제: .env 의 NCP_CLOVASTUDIO_API_KEY. (function calling은 HCX-005 / HCX-DASH-002 지원)
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

from backend.input_router import (
    RouteDecision,
    RouteType,
    is_http_url,
    is_obvious_junk,
    route_input,
)

# --- function calling 도구 정의 --------------------------------------------
_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "answer_with_kosis",
            "description": (
                "입력이 통계 수치를 묻거나 통계로 검증 가능한 '짧은 질의/주장'일 때 호출한다. "
                "지저분한 문장을 KOSIS에 물을 수 있는 명확한 질문으로 재구성한다. "
                "비교 주장이면 필요한 만큼 여러 개의 질문으로 나눈다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "연도·대상·지표가 명확한 KOSIS 질의 문장들. 예: '2024년 15~29세 실업률'",
                    }
                },
                "required": ["questions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "treat_as_article",
            "description": "입력이 여러 문장으로 된 기사 본문이라 먼저 주장 추출이 필요할 때 호출한다. 인자는 없다.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reject",
            "description": (
                "입력이 통계 질의도, 검증할 기사 본문도 아니어서 이 서비스로 처리할 수 없을 때 호출한다. "
                "예: 인사말, 잡담, 의미 없는 문자열, 통계와 무관한 요청."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "짧은 거절 사유(한국어)"}
                },
            },
        },
    },
]

_SYSTEM_PROMPT = (
    "너는 뉴스 사실검증 파이프라인의 라우터다. 사용자 입력을 보고 정확히 하나의 도구를 호출한다.\n"
    "- 통계 수치를 묻거나 통계로 검증 가능한 짧은 질의/주장이면 answer_with_kosis 를 호출하고, "
    "KOSIS에 물을 명확한 질문으로 재구성한다.\n"
    "- 여러 문장으로 된 기사 본문이면 treat_as_article 을 호출한다.\n"
    "- 통계 질의도 기사도 아니면(인사말·잡담·무의미한 입력 등) reject 를 호출한다.\n"
    "설명 없이 도구 호출만 한다."
)

_ENDPOINT = "https://clovastudio.stream.ntruss.com/v3/chat-completions/{model}"

_TOOL_TO_ROUTE = {
    "answer_with_kosis": RouteType.SIMPLE_KOSIS_QUERY,
    "treat_as_article": RouteType.ARTICLE_TEXT,
    "reject": RouteType.OUT_OF_SCOPE,
}


def _model() -> str:
    return (os.getenv("HCX_MODEL", "HCX-005").strip() or "HCX-005").upper()


def _api_key() -> str:
    return os.getenv("NCP_CLOVASTUDIO_API_KEY", "").strip()


def _call_hcx(text: str, *, timeout: float) -> dict[str, Any]:
    """Clova Studio v3 chat-completions function calling 실호출. result dict 반환."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("NCP_CLOVASTUDIO_API_KEY 미설정")
    url = _ENDPOINT.format(model=_model())
    body = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "tools": _TOOLS,
        "toolChoice": "auto",  # function calling은 maxTokens >= 1024 필요
        "maxTokens": 1024,
        "temperature": 0.1,
        "topP": 0.8,
        "topK": 0,
        "repetitionPenalty": 1.1,
    }
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HCX {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("result", {})


def _parse_tool_call(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """result.message.toolCalls[0] 에서 (함수명, 인자dict) 추출.
    arguments 는 dict 또는 문자열 둘 다 올 수 있어 모두 처리한다."""
    calls = (result.get("message") or {}).get("toolCalls") or []
    if not calls:
        raise ValueError("toolCalls 없음")
    fn = calls[0]["function"]
    args = fn.get("arguments", {})
    if isinstance(args, str):
        args = json.loads(args or "{}")
    return fn["name"], args


def route_input_llm(text: str, *, input_type: str = "auto", timeout: float = 20.0) -> RouteDecision:
    """LLM으로 입력을 분류한다. 명시적 input_type 또는 URL은 규칙으로 선처리하고,
    HCX 호출 실패 시 규칙 기반 route_input 으로 폴백한다."""
    value = text.strip()

    # 명시적 유형은 결정론적으로 처리(LLM 불필요) — 기존 규칙 라우터에 위임
    if input_type != "auto":
        return route_input(value, input_type=input_type)

    # URL은 LLM 이전에 정규식으로 컷(무료·정확)
    if is_http_url(value):
        return RouteDecision(RouteType.ARTICLE_URL, 1.0, "HTTP_URL")

    # 명백한 비통계 잡담은 LLM 호출 없이 즉시 거절(지연·비용 절감)
    if is_obvious_junk(value):
        return RouteDecision(RouteType.OUT_OF_SCOPE, 0.9, "PREFILTER_JUNK")

    try:
        result = _call_hcx(value, timeout=timeout)
        name, args = _parse_tool_call(result)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 규칙 라우터로 폴백
        fallback = route_input(value, input_type="auto")
        return RouteDecision(
            fallback.route,
            fallback.confidence,
            f"LLM_FALLBACK_{fallback.reason_code}",
            extra={"llm_error": str(exc)[:200]},
        )

    route = _TOOL_TO_ROUTE.get(name)
    if route is None:  # 알 수 없는 도구 → 폴백
        fallback = route_input(value, input_type="auto")
        return RouteDecision(
            fallback.route,
            fallback.confidence,
            f"LLM_FALLBACK_{fallback.reason_code}",
            extra={"llm_unknown_tool": name},
        )

    extra: dict[str, Any] = {"via": "hcx", "tool": name}
    if route is RouteType.SIMPLE_KOSIS_QUERY:
        questions = [q for q in args.get("questions", []) if isinstance(q, str) and q.strip()]
        if not questions:  # 재구성 질문이 비면 원문으로 대체
            questions = [value]
        extra["questions"] = questions
    elif route is RouteType.OUT_OF_SCOPE and args.get("reason"):
        extra["reason"] = str(args["reason"])[:200]

    return RouteDecision(route, 0.9, f"LLM_{name.upper()}", extra=extra)
