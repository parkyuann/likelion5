"""안전한 URL 기사 추출 결과를 전체 ArticleDocument 계약으로 반환한다."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable

from backend.errors import BackendError

ROOT = Path(__file__).resolve().parents[1]
URL_PROTOTYPE = ROOT / "archive" / "260817"


def _load_fetch_and_extract() -> Callable[..., Any]:
    """검증된 격리 프로토타입을 지연 로드한다."""
    path = str(URL_PROTOTYPE)
    if path not in sys.path:
        sys.path.insert(0, path)
    module = importlib.import_module("run_url")
    return module.fetch_and_extract


def _pipeline_error_to_backend(exc: Exception) -> BackendError:
    code = str(getattr(getattr(exc, "code", None), "value", "URL_PROCESSING_FAILED"))
    status_by_code = {
        "INVALID_URL": 422,
        "BLOCKED_TARGET": 403,
        "FETCH_TIMEOUT": 504,
        "UNSUPPORTED_CONTENT_TYPE": 415,
        "CONTENT_TOO_LARGE": 413,
        "ACCESS_RESTRICTED": 422,
        "EXTRACTION_LOW_QUALITY": 422,
        "TOO_MANY_REDIRECTS": 422,
        "FETCH_ERROR": 502,
    }
    return BackendError(
        code,
        str(getattr(exc, "message", str(exc))),
        status_code=status_by_code.get(code, 502),
        detail=dict(getattr(exc, "detail", {}) or {}),
    )


def prepare_url_article(
    url: str,
    *,
    fetch_and_extract_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """URL을 ArticleDocument로 변환하고 본문 전체를 다음 단계에 전달한다.

    팀의 고도화 기사 분석 프로세스가 백엔드 호출 함수로 확정되기 전까지 이 계층은
    문장 분리나 검색을 수행하지 않는다. ``article_document.text``가 추출된 전체 본문이다.
    """
    fetcher = fetch_and_extract_fn or _load_fetch_and_extract()
    try:
        document = fetcher(url)
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "message"):
            raise _pipeline_error_to_backend(exc) from exc
        raise BackendError(
            "URL_PROCESSING_FAILED",
            "기사 URL 처리 중 오류가 발생했습니다.",
            status_code=502,
        ) from exc

    extraction = (
        document.extraction_quality.to_dict()
        if document.extraction_quality is not None
        else None
    )
    return {
        "type": "article_document",
        "status": "ready_for_verification",
        "source": document.to_source_dict(),
        "extraction": extraction,
        "article_document": document.to_dict(),
    }


# 이전 내부 import 이름을 사용하던 코드의 호환을 위한 별칭. 동작은 전체 문서 준비다.
verify_url_article = prepare_url_article
