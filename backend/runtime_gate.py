"""Shared feature gates for the EC2 integration development environment."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import Request

from backend.errors import BackendError


SEARCH_ADAPTER_PENDING = "SEARCH_ADAPTER_PENDING"
APPLICATION_PRODUCT_STATE_PENDING = "APPLICATION_PRODUCT_STATE_PENDING"
PIPELINE_RUNTIME_PENDING = "PIPELINE_RUNTIME_PENDING"
PIPELINE_NATURAL_QUERY_PENDING = "PIPELINE_NATURAL_QUERY_PENDING"
PIPELINE_URL_PENDING = "PIPELINE_URL_PENDING"
PIPELINE_IMAGE_PENDING = "PIPELINE_IMAGE_PENDING"
PIPELINE_RUNTIME_ENABLED_ENV = "PIPELINE_RUNTIME_ENABLED"
PIPELINE_LIVE_STAGE_ENABLED_ENV = "PIPELINE_LIVE_STAGE_ENABLED"


def pipeline_runtime_enabled() -> bool:
    """The runtime flag is deliberately exact: only the literal ``true`` opens it."""

    return os.getenv(PIPELINE_RUNTIME_ENABLED_ENV, "") == "true"


def pipeline_live_stage_enabled() -> bool:
    """Live stage is independently gated and never inferred from service URLs."""

    return os.getenv(PIPELINE_LIVE_STAGE_ENABLED_ENV, "") == "true"


def require_search_adapter() -> None:
    """Compatibility guard for services that are outside the table-search route."""

    raise BackendError(
        SEARCH_ADAPTER_PENDING,
        "검색 adapter 계약과 구현이 아직 연결되지 않았습니다.",
        status_code=503,
    )


def require_application_product_state() -> None:
    """Product-state tables require the separately approved application 002."""

    raise BackendError(
        APPLICATION_PRODUCT_STATE_PENDING,
        "application product-state migration이 아직 연결되지 않았습니다.",
        status_code=503,
    )


def require_pipeline_runtime() -> None:
    """Open the article pipeline only when the exact runtime feature flag is true."""

    if not pipeline_runtime_enabled():
        raise BackendError(PIPELINE_RUNTIME_PENDING, "검증 pipeline runtime이 아직 활성화되지 않았습니다.", status_code=503)


def raise_pipeline_pending(code: str, message: str) -> None:
    raise BackendError(code, message, status_code=503)


def _origin(value: str | None, *, from_referer: bool = False) -> str:
    if not value or value.strip().casefold() == "null":
        return ""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"}:
            return ""
        if parsed.username or parsed.password or not parsed.hostname:
            return ""
        port = parsed.port
    except ValueError:
        return ""
    if not from_referer and parsed.path != "":
        return ""
    if not from_referer and (parsed.query or parsed.fragment):
        return ""
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def require_csrf(request: Request) -> None:
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site not in {"same-origin", "same-site"}:
        raise BackendError("CSRF_REJECTED", "요청 출처를 확인할 수 없습니다.", status_code=403)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    supplied = _origin(origin) if origin is not None else _origin(referer, from_referer=True)
    configured = os.getenv("AUTH_ALLOWED_ORIGINS", "")
    allowed = {_origin(item) for item in configured.split(",") if _origin(item)}
    if not supplied or supplied not in allowed:
        raise BackendError("CSRF_REJECTED", "허용되지 않은 요청 출처입니다.", status_code=403)
