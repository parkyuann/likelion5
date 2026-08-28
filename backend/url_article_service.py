"""Release-bound, fail-closed URL article acquisition for the HTTP API.

Only an explicitly approved publisher adapter is exposed. This keeps a
browser-facing URL from becoming a generic server-side request primitive and
hands the existing article verification pipeline an ArticleDocument with
title/date provenance rather than an inferred value.
"""

from __future__ import annotations

from datetime import datetime
import ipaddress
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from backend.errors import BackendError


_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "pipeline_runtime"
_ALLOWED_HOSTS = frozenset({"khan.co.kr", "www.khan.co.kr"})
_MAX_REDIRECTS = 3
_MAX_HTML_BYTES = 2 * 1024 * 1024
_TIMEOUT_SECONDS = 15.0


def _backend_error(code: str, message: str, status_code: int) -> BackendError:
    return BackendError(code, message, status_code=status_code)


def _validated_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise _backend_error("INVALID_URL", "올바른 HTTPS 기사 URL을 입력해 주세요.", 422) from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or (port not in (None, 443))
    ):
        raise _backend_error(
            "URL_NOT_SUPPORTED",
            "현재는 경향신문(khan.co.kr) HTTPS 기사 URL만 지원합니다.",
            422,
        )
    return parsed.geturl()


def _require_public_dns(host: str) -> None:
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _backend_error("URL_DNS_FAILED", "기사 주소를 확인할 수 없습니다.", 502) from exc
    values = {entry[4][0] for entry in addresses}
    if not values:
        raise _backend_error("URL_DNS_FAILED", "기사 주소를 확인할 수 없습니다.", 502)
    try:
        if any(not ipaddress.ip_address(value).is_global for value in values):
            raise _backend_error("URL_TARGET_BLOCKED", "허용되지 않은 기사 주소입니다.", 403)
    except ValueError as exc:
        raise _backend_error("URL_DNS_FAILED", "기사 주소를 확인할 수 없습니다.", 502) from exc


def _load_khan_parser() -> tuple[Any, type[Exception]]:
    root = str(_RUNTIME_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from src.news_verification.runtime.operational_article_acquisition_v2 import (
            ArticleAcquisitionError,
            parse_khan_article,
        )
    except ImportError as exc:
        raise _backend_error("URL_ADAPTER_UNAVAILABLE", "URL 기사 추출 모듈을 준비하지 못했습니다.", 503) from exc
    return parse_khan_article, ArticleAcquisitionError


def _fetch_html(source_url: str) -> tuple[bytes, str, str]:
    current = _validated_url(source_url)
    timeout = httpx.Timeout(_TIMEOUT_SECONDS, connect=5.0)
    headers = {"User-Agent": "news-verification/1.0 (+article-source-adapter)"}
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                parsed = urlsplit(current)
                _require_public_dns(parsed.hostname or "")
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise _backend_error("URL_REDIRECT_INVALID", "기사 주소 이동 정보를 확인할 수 없습니다.", 502)
                        current = _validated_url(urljoin(current, location))
                        continue
                    if response.status_code != 200:
                        raise _backend_error("URL_FETCH_FAILED", "기사 페이지를 가져오지 못했습니다.", 502)
                    content_type = response.headers.get("content-type", "")
                    if "text/html" not in content_type.casefold():
                        raise _backend_error("URL_CONTENT_TYPE_UNSUPPORTED", "HTML 기사 페이지만 지원합니다.", 415)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_HTML_BYTES:
                            raise _backend_error("URL_CONTENT_TOO_LARGE", "기사 페이지 크기가 허용 범위를 초과합니다.", 413)
                        chunks.append(chunk)
                    return b"".join(chunks), current, content_type
            raise _backend_error("URL_TOO_MANY_REDIRECTS", "기사 주소 이동이 너무 많습니다.", 422)
    except BackendError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise _backend_error("URL_FETCH_FAILED", "기사 페이지를 가져오지 못했습니다.", 502) from exc


def prepare_url_article(url: str) -> dict[str, Any]:
    """Acquire one supported URL and return a provenance-bearing ArticleDocument.

    The raw HTML stays in request memory only. The response retains its SHA-256,
    source URL, extractor identity, and article-text SHA so the downstream
    verification receives only the extracted text and metadata.
    """

    raw_html, final_url, content_type = _fetch_html(url)
    parse_khan_article, acquisition_error = _load_khan_parser()
    try:
        article, extraction = parse_khan_article(raw_html, source_url=url, final_url=final_url)
    except acquisition_error as exc:
        raise _backend_error("URL_EXTRACTION_FAILED", "기사 본문·제목·발행일을 추출하지 못했습니다.", 422) from exc
    try:
        published_date = datetime.fromisoformat(str(article["date"]).replace("Z", "+00:00")).date().isoformat()
    except (KeyError, TypeError, ValueError) as exc:
        raise _backend_error("URL_DATE_INVALID", "기사 발행일을 확인하지 못했습니다.", 422) from exc
    text = str(article.get("article_text") or "").strip()
    title = str(article.get("title") or "").strip()
    if not text or not title:
        raise _backend_error("URL_EXTRACTION_FAILED", "기사 본문 또는 제목을 추출하지 못했습니다.", 422)
    import hashlib

    raw_html_sha256 = hashlib.sha256(raw_html).hexdigest()
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    receipt = {
        "contract": "release-bound-url-article-adapter-v1",
        "source_url": url,
        "final_url": final_url,
        "content_type": content_type.split(";", 1)[0].strip().lower(),
        "raw_html_sha256": raw_html_sha256,
        "raw_html_bytes": len(raw_html),
        "extractor": extraction,
        "article_text_sha256": text_sha256,
    }
    return {
        "type": "article_document",
        "status": "ready_for_verification",
        "source": {"source_type": "url", **receipt},
        "extraction": {"status": "success", "receipt": receipt},
        "article_document": {
            "source_type": "url",
            "text": text,
            "title": title,
            "published_date": published_date,
            "extractor": "KHAN_ARTICLE_V1",
            "content_hash": f"sha256:{text_sha256}",
        },
    }


verify_url_article = prepare_url_article
