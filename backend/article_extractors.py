"""Publisher-aware, provenance-bearing HTML article extraction.

The HTTP URL adapter deliberately keeps acquisition and verification separate:
this module only turns already-fetched HTML into an ``ArticleDocument`` input.
It never calls a model, a search service, or KOSIS.  A publisher-specific
selector is preferred when available; a guarded structured-data/HTML fallback
is used for another public HTTPS news page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag


class ArticleExtractionError(ValueError):
    """Raised when HTML cannot be made into a safe article document."""


_DATE_RE = re.compile(r"(?P<year>20\d{2})\s*[.\-/년]\s*(?P<month>0?[1-9]|1[0-2])\s*[.\-/월]\s*(?P<day>3[01]|[12]\d|0?[1-9])")
_SPACE_RE = re.compile(r"\s+")
_FUSION_GLOBAL_CONTENT_RE = re.compile(r"Fusion\.globalContent\s*=\s*")
# Keep the source ASCII-safe because the service is developed from both Korean
# Windows and Linux hosts.  Generic fallback also accepts an English article.
_LETTER_RE = re.compile(r"[A-Za-z\uac00-\ud7a3]")
_UNWANTED_SELECTOR = ", ".join(
    (
        "script",
        "style",
        "noscript",
        "nav",
        "footer",
        "aside",
        ".advertisement",
        ".ad",
        ".ads",
        ".related",
        ".recommend",
        ".subscription",
        ".comment",
        ".reply",
        ".share",
    )
)


@dataclass(frozen=True)
class PublisherAdapter:
    name: str
    hosts: frozenset[str]
    body_selectors: tuple[str, ...]


PUBLISHER_ADAPTERS: tuple[PublisherAdapter, ...] = (
    PublisherAdapter(
        "KHAN_ARTICLE_V1",
        frozenset({"khan.co.kr", "www.khan.co.kr"}),
        ("#articleBody .content_text", "#articleBody", "article"),
    ),
    PublisherAdapter(
        "CHOSUN_ARTICLE_V1",
        frozenset({"chosun.com", "www.chosun.com"}),
        ("#news-body", ".article-body", ".article_body", ".article_txt", ".article_content", "article"),
    ),
    PublisherAdapter(
        "JOONGANG_ARTICLE_V1",
        frozenset({"joongang.co.kr", "www.joongang.co.kr"}),
        ("#article_body", ".article_body", ".article-body", ".article-content", ".article_content", "article"),
    ),
    PublisherAdapter(
        "DONGA_ARTICLE_V1",
        frozenset({"donga.com", "www.donga.com"}),
        ("#article_body", ".article_txt", ".article-body", ".article_body", ".article_view", ".view_body", "article"),
    ),
    PublisherAdapter(
        "NAVER_NEWS_ARTICLE_V1",
        frozenset({"n.news.naver.com", "news.naver.com"}),
        ("#dic_area", "#newsct_article", "#newsEndContents", "#articeBody", "article"),
    ),
)

_GENERIC_SELECTORS = (
    "article[itemprop='articleBody']",
    "[itemprop='articleBody']",
    "article",
    "main article",
    ".article-body",
    ".article_body",
    ".article-content",
    ".article_content",
    "#articleBody",
    "#article_body",
    "#newsct_article",
    "#dic_area",
    "#newsEndContents",
    "#articeBody",
)


def publisher_adapter_for_url(url: str) -> PublisherAdapter | None:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    return next((adapter for adapter in PUBLISHER_ADAPTERS if host in adapter.hosts), None)


def _clean_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


def _normalized_paragraphs(node: Tag) -> list[str]:
    for unwanted in node.select(_UNWANTED_SELECTOR):
        unwanted.decompose()
    paragraphs = [_clean_text(paragraph.get_text(" ", strip=True)) for paragraph in node.select("p")]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if paragraphs:
        return paragraphs
    text = _clean_text(node.get_text(" ", strip=True))
    return [text] if text else []


def _quality(paragraphs: Iterable[str], *, min_compact_chars: int = 100) -> tuple[bool, int, int]:
    values = [value for value in paragraphs if value]
    text = "\n\n".join(values)
    compact = _SPACE_RE.sub("", text)
    # This is a provenance/quality gate, not an article classifier.  It blocks
    # obvious menu/login pages while still allowing short but genuine notices.
    return len(compact) >= min_compact_chars and len(_LETTER_RE.findall(text)) >= 30, len(text), len(values)


def _body_from_selectors(
    soup: BeautifulSoup,
    selectors: Iterable[str],
    *,
    min_compact_chars: int = 100,
    prefer_selector_order: bool = False,
) -> tuple[list[str], str] | None:
    best: tuple[list[str], str, int] | None = None
    for selector in selectors:
        # A selector may point at an article wrapper or at every individual
        # body paragraph.  Aggregate every match for this *one* selector
        # before checking quality, so a precise repeated paragraph selector
        # does not lose to a broader wrapper merely because each paragraph is
        # short by itself.
        paragraphs: list[str] = []
        for node in soup.select(selector):
            paragraphs.extend(_normalized_paragraphs(node))
        valid, chars, _ = _quality(paragraphs, min_compact_chars=min_compact_chars)
        if not valid:
            continue
        # Publisher adapter selector order is editorial provenance: the first
        # passing scoped selector beats a broader fallback wrapper that may
        # contain a subtitle, caption, or related-page text.  Generic fallback
        # remains volume-based because it has no publisher-specific scope.
        if prefer_selector_order:
            return paragraphs, selector
        if best is None or chars > best[2]:
            best = (paragraphs, selector, chars)
    return (best[0], best[1]) if best else None


def _json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_objects(item)


def _json_ld_records(soup: BeautifulSoup) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for node in soup.select("script[type='application/ld+json']"):
        raw = node.string or node.get_text()
        if not raw or not raw.strip():
            continue
        try:
            records.extend(_json_objects(json.loads(raw)))
        except json.JSONDecodeError:
            continue
    return records


def _is_article_record(record: dict[str, Any]) -> bool:
    raw_type = record.get("@type")
    values = raw_type if isinstance(raw_type, list) else [raw_type]
    return any(str(value).casefold() in {"article", "newsarticle", "reportagenewsarticle"} for value in values)


def _first_metadata(soup: BeautifulSoup, records: Iterable[dict[str, Any]], field: str, meta_keys: Iterable[str]) -> str:
    for record in records:
        if _is_article_record(record) and record.get(field):
            return _clean_text(str(record[field]))
    for key in meta_keys:
        node = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if node and node.get("content"):
            return _clean_text(str(node["content"]))
    return ""


def _normalize_date(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ArticleExtractionError("ARTICLE_PUBLISHED_DATE_MISSING")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        match = _DATE_RE.search(raw)
        if not match:
            raise ArticleExtractionError("ARTICLE_PUBLISHED_DATE_INVALID") from None
        return "{year:04d}-{month:02d}-{day:02d}".format(
            year=int(match.group("year")), month=int(match.group("month")), day=int(match.group("day"))
        )


def _fallback_date(soup: BeautifulSoup) -> str:
    # Naver News publishes the canonical timestamp in an element rather than
    # an Open Graph/JSON-LD property on some current article pages.
    for selector in (
        ".media_end_head_info_datestamp_time",
        "._ARTICLE_DATE_TIME",
        ".article_info .date",
        ".article_date",
    ):
        node = soup.select_one(selector)
        if node:
            value = str(node.get("datetime") or node.get("data-date-time") or node.get_text(" ", strip=True))
            if value.strip():
                return value
    node = soup.find("time")
    if node:
        value = str(node.get("datetime") or node.get_text(" ", strip=True))
        if value.strip():
            return value
    return ""


def _generic_body_from_json_ld(records: Iterable[dict[str, Any]]) -> list[str] | None:
    for record in records:
        if not _is_article_record(record):
            continue
        body = _clean_text(str(record.get("articleBody") or ""))
        valid, _, _ = _quality([body])
        if valid:
            return [body]
    return None


def _chosun_body_from_fusion_metadata(soup: BeautifulSoup) -> list[str] | None:
    """Read text blocks from Chosun's server-rendered Fusion metadata.

    The page DOM is hydrated in the browser, while the fetched HTML keeps the
    article text in ``Fusion.globalContent.content_elements``.  Only explicit
    text elements are accepted; captions, author biographies, navigation and
    promotional content are intentionally excluded.
    """
    node = soup.select_one("script#fusion-metadata")
    payload = (node.string or node.get_text()) if node else ""
    match = _FUSION_GLOBAL_CONTENT_RE.search(payload or "")
    if not match:
        return None
    try:
        record, _ = json.JSONDecoder().raw_decode(payload, match.end())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    elements = record.get("content_elements")
    if not isinstance(elements, list):
        return None
    paragraphs: list[str] = []
    for element in elements:
        if not isinstance(element, dict) or str(element.get("type") or "").casefold() != "text":
            continue
        value = element.get("content")
        if not isinstance(value, str):
            continue
        # Fusion text can include presentation markup.  ArticleDocument stores
        # normalized plain text, not publisher HTML.
        paragraphs.extend(_normalized_paragraphs(BeautifulSoup(value, "html.parser")))
    return paragraphs or None


def extract_article_html(raw_html: bytes, *, source_url: str, final_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract title/date/body with explicit publisher or generic provenance."""
    try:
        # Korean news publishers normally serve UTF-8.  Decode it explicitly
        # before parsing so an absent/misleading HTML charset declaration does
        # not silently turn Korean body text into mojibake.  A genuinely
        # non-UTF-8 document still gets BeautifulSoup's encoding detection.
        try:
            html_input: str | bytes = raw_html.decode("utf-8")
        except UnicodeDecodeError:
            html_input = raw_html
        soup = BeautifulSoup(html_input, "html.parser")
    except Exception as exc:  # BeautifulSoup keeps parser failures isolated from the API boundary.
        raise ArticleExtractionError("ARTICLE_HTML_PARSE_FAILED") from exc
    records = _json_ld_records(soup)
    adapter = publisher_adapter_for_url(final_url)
    selected = _body_from_selectors(
        soup,
        adapter.body_selectors if adapter else _GENERIC_SELECTORS,
        # A known publisher selector is stronger provenance than the generic
        # fallback.  Keep its compact short-notice allowance consistent with
        # the final quality gate below.
        min_compact_chars=80 if adapter else 100,
        prefer_selector_order=adapter is not None,
    )
    body_source = "publisher_selector" if adapter else "generic_selector"
    selector = selected[1] if selected else None
    paragraphs = selected[0] if selected else None
    if paragraphs is None and adapter and adapter.name == "CHOSUN_ARTICLE_V1":
        paragraphs = _chosun_body_from_fusion_metadata(soup)
        if paragraphs:
            body_source = "publisher_fusion_metadata"
            selector = "script#fusion-metadata:Fusion.globalContent.content_elements[type=text]"
    if paragraphs is None:
        paragraphs = _generic_body_from_json_ld(records)
        body_source = "json_ld_articleBody"
    if not paragraphs:
        raise ArticleExtractionError("ARTICLE_BODY_QUALITY_INSUFFICIENT")
    # A publisher-specific selector has stronger provenance than a generic
    # page-wide fallback.  Allow a short, multi-paragraph publisher article
    # while keeping the generic fallback at the stricter 100-character gate.
    min_compact_chars = 80 if adapter and body_source.startswith("publisher_") else 100
    valid, text_chars, paragraph_count = _quality(paragraphs, min_compact_chars=min_compact_chars)
    if not valid:
        raise ArticleExtractionError("ARTICLE_BODY_QUALITY_INSUFFICIENT")

    title = _first_metadata(soup, records, "headline", ("og:title", "twitter:title"))
    if not title:
        heading = soup.find("h1")
        title = _clean_text(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        raise ArticleExtractionError("ARTICLE_TITLE_MISSING")
    raw_date = _first_metadata(
        soup,
        records,
        "datePublished",
        ("article:published_time", "og:published_time", "date", "publishdate", "pubdate"),
    ) or _fallback_date(soup)
    published_date = _normalize_date(raw_date)
    article_text = "\n\n".join(paragraphs)
    adapter_name = adapter.name if adapter else "GENERIC_ARTICLE_V1"
    article = {
        "article_idx": f"url-{hashlib.sha256(final_url.encode('utf-8')).hexdigest()[:16]}",
        "title": title,
        "article_text": article_text,
        "date": published_date,
        "source_url": source_url,
        "final_url": final_url,
    }
    extraction = {
        "adapter": adapter_name,
        "title_source": "json_ld_or_open_graph_or_h1",
        "date_source": "json_ld_or_open_graph_or_time",
        "body_source": body_source,
        "body_selector": selector,
        "paragraph_count": paragraph_count,
        "text_chars": text_chars,
        "paragraph_sha256": [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in paragraphs],
        "article_text_sha256": hashlib.sha256(article_text.encode("utf-8")).hexdigest(),
    }
    return article, extraction


__all__ = [
    "ArticleExtractionError",
    "PUBLISHER_ADAPTERS",
    "extract_article_html",
    "publisher_adapter_for_url",
]
