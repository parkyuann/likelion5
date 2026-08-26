"""팀 KOSIS MCP 파이프라인을 FastAPI 응답 계약에 맞추는 어댑터."""

from __future__ import annotations

from typing import Any, Callable

from backend.errors import BackendError


PipelineFn = Callable[[str, str | None], dict[str, Any]]


def query(
    question: str,
    *,
    pipeline_fn: PipelineFn | None = None,
) -> tuple[dict[str, Any], str, str]:
    """신규 ``src/run_pipeline.py``의 자연어 질의 경로를 호출한다.

    무거운 검색·판정 모듈은 실제 통계 질의가 들어올 때만 import하고, 반환값은
    기존 프론트가 사용하는 ``(MCP 원문, 답변, 도구명)`` 계약으로 정규화한다.
    """
    if pipeline_fn is None:
        try:
            from src.run_pipeline import answer_natural_query
        except Exception as exc:  # noqa: BLE001 - 백엔드 경계에서 안전한 오류로 변환
            raise BackendError(
                "TEAM_MCP_IMPORT_FAILED",
                "KOSIS MCP 파이프라인을 불러오지 못했습니다.",
                status_code=503,
                detail={"reason": str(exc)[:300]},
            ) from exc
        pipeline_fn = answer_natural_query

    try:
        pipeline_result = pipeline_fn(question, None)
    except BackendError:
        raise
    except Exception as exc:  # noqa: BLE001 - 외부 MCP/HCX 오류를 API 오류로 변환
        raise BackendError(
            "TEAM_MCP_REQUEST_FAILED",
            "KOSIS MCP 질의 처리에 실패했습니다.",
            status_code=502,
            detail={"reason": str(exc)[:300]},
        ) from exc

    if not isinstance(pipeline_result, dict):
        raise BackendError(
            "TEAM_MCP_INVALID_RESPONSE",
            "KOSIS MCP가 올바르지 않은 응답을 반환했습니다.",
            status_code=502,
        )

    answer = str(
        pipeline_result.get("answer")
        or pipeline_result.get("numeric")
        or pipeline_result.get("evidence")
        or ""
    ).strip()
    if not answer:
        raise BackendError(
            "TEAM_MCP_EMPTY_ANSWER",
            "KOSIS MCP 응답에서 답변을 찾지 못했습니다.",
            status_code=502,
            detail={"stage": pipeline_result.get("stage")},
        )

    stage = str(pipeline_result.get("stage") or "team_pipeline")
    raw_result = {
        "content": [{"type": "text", "text": answer}],
        "structuredContent": pipeline_result,
    }
    return raw_result, answer, f"team_kosis_pipeline:{stage}"
