"""입력 라우팅 후 KOSIS MCP 또는 기사 검증 경로를 실행한다."""

from __future__ import annotations

from typing import Any, Callable

from backend.errors import BackendError
from backend.input_router import RouteType
from backend.kosis_mcp_client import KosisMcpClient, KosisMcpConfig, content_text
from backend.llm_router import route_input_llm
from backend.url_article_service import prepare_url_article


def _default_kosis_query(question: str) -> tuple[dict[str, Any], str, str]:
    config = KosisMcpConfig.from_env()
    with KosisMcpClient(config) as client:
        result = client.query(question)
    return result, content_text(result), config.tool_name


def analyze(
    text: str,
    *,
    input_type: str = "auto",
    max_claims: int = 10,
    explain: bool = False,
    kosis_query_fn: Callable[[str], tuple[dict[str, Any], str, str]] | None = None,
    prepare_url_fn: Callable[..., dict[str, Any]] = prepare_url_article,
) -> dict[str, Any]:
    """한 입력을 판별하고 첫 백엔드 목표의 실행 경로로 전달한다."""
    decision = route_input_llm(text, input_type=input_type)
    route = decision.to_dict()

    if decision.route is RouteType.SIMPLE_KOSIS_QUERY:
        # LLM이 KOSIS 질의로 재구성한 문장이 있으면 그걸 우선 사용(품질↑).
        questions = decision.extra.get("questions") if decision.extra else None
        query_text = questions[0] if questions else text
        result, answer, tool_name = (kosis_query_fn or _default_kosis_query)(query_text)
        return {
            "type": "simple_query",
            "status": "completed",
            "route": route,
            "question": query_text,
            "answer": answer,
            "mcp": {
                "tool": tool_name,
                "structured_content": result.get("structuredContent"),
                "content": result.get("content", []),
            },
        }

    if decision.route is RouteType.ARTICLE_URL:
        response = prepare_url_fn(text)
        response["route"] = route
        return response

    if decision.route is RouteType.ARTICLE_TEXT:
        return {
            "type": "article_document",
            "status": "ready_for_verification",
            "route": route,
            "source": {"source_type": "text"},
            "extraction": {
                "status": "success",
                "character_count": len(text),
                "warning_codes": [],
            },
            "article_document": {
                "source_type": "text",
                "text": text,
                "title": None,
                "published_date": None,
            },
        }

    # 사용자를 탓하지 않는 안내. LLM의 내부 판정 사유는 노출하지 않는다.
    raise BackendError(
        "OUT_OF_SCOPE",
        "통계 수치가 담긴 질문이나 검증할 기사 내용을 넣어 주시면 확인해 드릴게요. "
        "예를 들어 “2024년 청년 실업률은 얼마야?” 처럼 물어보실 수 있어요.",
        status_code=422,
        detail={"route": route},
    )
