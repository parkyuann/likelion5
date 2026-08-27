"""Fail-closed URL acquisition for operational article verification v2.

The acquired HTML and extracted article input are immutable run inputs.  This
module does not invoke L2, search, KOSIS, or any model.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib import parse, request


ACQUISITION_CONTRACT = "operational-article-acquisition-v2"
_KHAN_PATH = re.compile(r"^/article/(\d+)$")


class ArticleAcquisitionError(ValueError):
    pass


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite acquisition artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


class _KhanParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self._body_depth = 0
        self._capture_depth = 0
        self._capture_parts: list[str] = []
        self.paragraphs: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = content
        if self._body_depth:
            self._body_depth += 1
        elif values.get("id") == "articleBody":
            self._body_depth = 1
        classes = set(values.get("class", "").split())
        if self._body_depth and tag == "p" and "content_text" in classes:
            if self._capture_depth:
                raise ArticleAcquisitionError("KHAN_NESTED_CONTENT_TEXT")
            self._capture_depth = 1
            self._capture_parts = []
        elif self._capture_depth:
            self._capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._capture_depth:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                text = " ".join("".join(self._capture_parts).split())
                if text:
                    self.paragraphs.append(text)
                self._capture_parts = []
        if self._body_depth:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_depth:
            self._capture_parts.append(data)


def parse_khan_article(raw_html: bytes, *, source_url: str, final_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        html_text = raw_html.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArticleAcquisitionError("ARTICLE_HTML_UTF8_REQUIRED") from exc
    parser = _KhanParser()
    parser.feed(html_text)
    title = str(parser.meta.get("og:title") or "").strip()
    published = str(parser.meta.get("article:published_time") or "").strip()
    if not title:
        raise ArticleAcquisitionError("KHAN_TITLE_MISSING")
    if not published:
        raise ArticleAcquisitionError("KHAN_PUBLISHED_TIME_MISSING")
    try:
        parsed_time = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArticleAcquisitionError("KHAN_PUBLISHED_TIME_INVALID") from exc
    if parsed_time.tzinfo is None:
        raise ArticleAcquisitionError("KHAN_PUBLISHED_TIME_TZ_REQUIRED")
    if not parser.paragraphs:
        raise ArticleAcquisitionError("KHAN_ARTICLE_BODY_MISSING")
    article_text = "\n\n".join(parser.paragraphs)
    match = _KHAN_PATH.fullmatch(parse.urlparse(final_url).path)
    if match is None:
        raise ArticleAcquisitionError("KHAN_FINAL_URL_ID_INVALID")
    article = {
        "article_idx": f"khan-{match.group(1)}",
        "title": title,
        "article_text": article_text,
        "date": published,
        "source_url": source_url,
        "final_url": final_url,
    }
    extraction = {
        "adapter": "KHAN_ARTICLE_V1",
        "title_source": "meta[property=og:title]",
        "date_source": "meta[property=article:published_time]",
        "body_selector": "#articleBody .content_text",
        "paragraph_count": len(parser.paragraphs),
        "paragraph_sha256": [_sha256(value.encode("utf-8")) for value in parser.paragraphs],
        "article_text_sha256": _sha256(article_text.encode("utf-8")),
    }
    return article, extraction


def acquire_article_url(
    url: str,
    output_root: str | Path,
    *,
    timeout_seconds: float = 20.0,
) -> tuple[Path, dict[str, Any]]:
    """Acquire one supported article URL and publish immutable frozen inputs."""
    parsed = parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"khan.co.kr", "www.khan.co.kr"}:
        raise ArticleAcquisitionError("ARTICLE_URL_UNSUPPORTED")
    if _KHAN_PATH.fullmatch(parsed.path) is None:
        raise ArticleAcquisitionError("ARTICLE_URL_PATH_INVALID")
    root = Path(output_root)
    if root.exists():
        raise FileExistsError(f"refusing to overwrite URL run output: {root}")
    fetch_request = request.Request(
        url,
        headers={"User-Agent": "news-verification-shadow/2.0 (+metadata-only article acquisition)"},
    )
    try:
        with request.urlopen(fetch_request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 0) or 0)
            final_url = str(response.geturl())
            content_type = str(response.headers.get("Content-Type") or "")
            raw_html = response.read()
    except Exception as exc:
        raise ArticleAcquisitionError(f"ARTICLE_FETCH_FAILED:{type(exc).__name__}") from exc
    if status != 200:
        raise ArticleAcquisitionError(f"ARTICLE_HTTP_STATUS:{status}")
    if "text/html" not in content_type.lower():
        raise ArticleAcquisitionError("ARTICLE_CONTENT_TYPE_INVALID")
    final = parse.urlparse(final_url)
    if final.hostname not in {"khan.co.kr", "www.khan.co.kr"}:
        raise ArticleAcquisitionError("ARTICLE_REDIRECT_HOST_INVALID")
    article, extraction = parse_khan_article(raw_html, source_url=url, final_url=final_url)
    acquired_at = datetime.now(timezone.utc).isoformat()
    receipt = {
        "contract": ACQUISITION_CONTRACT,
        "source_url": url,
        "final_url": final_url,
        "http_status": status,
        "content_type": content_type,
        "acquired_at": acquired_at,
        "raw_html_sha256": _sha256(raw_html),
        "raw_html_bytes": len(raw_html),
        "extraction": extraction,
        "article_idx": article["article_idx"],
    }
    frozen_path = root / "frozen_article.jsonl"
    _write_new(root / "acquisition" / "source.html", raw_html)
    _write_new(
        frozen_path,
        (json.dumps(article, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )
    receipt["frozen_article_sha256"] = _sha256(frozen_path.read_bytes())
    _write_new(
        root / "acquisition_receipt.json",
        (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return frozen_path, receipt


__all__ = [
    "ACQUISITION_CONTRACT", "ArticleAcquisitionError", "acquire_article_url", "parse_khan_article",
]


