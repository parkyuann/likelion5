from __future__ import annotations

import pytest

from backend import url_article_service
from backend.article_extractors import ArticleExtractionError, extract_article_html
from backend.errors import BackendError


_BODY = (
    "국가데이터처가 발표한 통계에 따르면 올해 지표는 전년보다 증가했다. "
    "이 문장은 URL 기사 추출기의 본문 품질 게이트를 검증하기 위한 충분한 길이의 예시 문장이다. "
    "추출 결과는 메뉴나 광고가 아니라 하나의 완결된 기사 본문이라는 점을 함께 확인한다."
)


@pytest.mark.parametrize(
    ("url", "body_html", "expected_adapter"),
    [
        ("https://www.khan.co.kr/article/202608271115011", f'<div id="articleBody"><p class="content_text">{_BODY}</p></div>', "KHAN_ARTICLE_V1"),
        ("https://www.chosun.com/national/example/", f'<div id="news-body"><p>{_BODY}</p></div>', "CHOSUN_ARTICLE_V1"),
        ("https://www.joongang.co.kr/article/example", f'<div id="article_body"><p>{_BODY}</p></div>', "JOONGANG_ARTICLE_V1"),
        ("https://www.donga.com/news/example/article/all/20260830/", f'<div class="article_txt"><p>{_BODY}</p></div>', "DONGA_ARTICLE_V1"),
        ("https://n.news.naver.com/mnews/article/001/0000000000", f'<div id="dic_area"><p>{_BODY}</p></div>', "NAVER_NEWS_ARTICLE_V1"),
    ],
)
def test_publisher_adapters_extract_standard_article_document(url: str, body_html: str, expected_adapter: str) -> None:
    html = f'''<!doctype html><html><head>
      <meta property="og:title" content="검증용 기사 제목" />
      <meta property="article:published_time" content="2026-08-30T09:00:00+09:00" />
    </head><body>{body_html}</body></html>'''.encode()

    article, receipt = extract_article_html(html, source_url=url, final_url=url)

    assert article["title"] == "검증용 기사 제목"
    assert article["date"] == "2026-08-30"
    assert article["article_text"] == _BODY
    assert receipt["adapter"] == expected_adapter
    assert receipt["body_source"] == "publisher_selector"
    assert receipt["paragraph_count"] == 1


def test_generic_json_ld_fallback_is_provenance_labeled() -> None:
    url = "https://example-news.test/articles/2026/08/30"
    html = f'''<html><head><script type="application/ld+json">{{
      "@context":"https://schema.org", "@type":"NewsArticle",
      "headline":"범용 기사", "datePublished":"2026.08.30",
      "articleBody":"{_BODY}"
    }}</script></head><body><main>메뉴</main></body></html>'''.encode()

    article, receipt = extract_article_html(html, source_url=url, final_url=url)

    assert article["title"] == "범용 기사"
    assert article["date"] == "2026-08-30"
    assert article["article_text"] == _BODY
    assert receipt["adapter"] == "GENERIC_ARTICLE_V1"
    assert receipt["body_source"] == "json_ld_articleBody"


def test_chosun_fusion_metadata_is_used_when_browser_dom_has_no_article() -> None:
    url = "https://www.chosun.com/national/example/"
    html = f'''<html><head>
      <meta property="og:title" content="조선일보 검증 기사" />
      <meta property="article:published_time" content="2026-08-30T09:00:00+09:00" />
    </head><body>
      <script id="fusion-metadata">window.Fusion=window.Fusion||{{}};
      Fusion.globalContent={{"content_elements":[
        {{"type":"image","caption":"이미지 설명"}},
        {{"type":"text","content":"<p>{_BODY}</p>"}}
      ]}};</script>
    </body></html>'''.encode()

    article, receipt = extract_article_html(html, source_url=url, final_url=url)

    assert article["article_text"] == _BODY
    assert receipt["adapter"] == "CHOSUN_ARTICLE_V1"
    assert receipt["body_source"] == "publisher_fusion_metadata"
    assert receipt["paragraph_count"] == 1


def test_donga_current_view_body_and_naver_timestamp_element_are_supported() -> None:
    donga_url = "https://www.donga.com/news/example/article/all/20260830/"
    donga_html = f'''<html><head><meta property="og:title" content="동아 기사" />
      <meta property="article:published_time" content="2026-08-30T09:00:00+09:00" /></head>
      <body><div class="view_body"><p>{_BODY}</p></div></body></html>'''.encode()
    naver_url = "https://n.news.naver.com/mnews/article/001/0000000000"
    naver_html = f'''<html><head><meta property="og:title" content="네이버 기사" /></head>
      <body><span class="media_end_head_info_datestamp_time _ARTICLE_DATE_TIME"
      data-date-time="2026-08-30 18:35:12">2026.08.30.</span>
      <div id="dic_area"><p>{_BODY}</p></div></body></html>'''.encode()

    donga_article, donga_receipt = extract_article_html(donga_html, source_url=donga_url, final_url=donga_url)
    naver_article, naver_receipt = extract_article_html(naver_html, source_url=naver_url, final_url=naver_url)

    assert donga_receipt["body_selector"] == ".view_body"
    assert donga_article["article_text"] == _BODY
    assert naver_receipt["body_selector"] == "#dic_area"
    assert naver_article["date"] == "2026-08-30"


def test_generic_extractor_rejects_menu_sized_text() -> None:
    html = "<html><head><meta property='og:title' content='x'></head><body><article>짧은 메뉴</article></body></html>".encode()

    with pytest.raises(ArticleExtractionError, match="ARTICLE_BODY_QUALITY_INSUFFICIENT"):
        extract_article_html(html, source_url="https://example-news.test/a", final_url="https://example-news.test/a")


def test_prepare_url_article_preserves_standard_contract_and_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://n.news.naver.com/mnews/article/001/0000000000"
    html = f'''<html><head><meta property="og:title" content="네이버 기사" />
      <meta property="article:published_time" content="2026-08-30T00:00:00+09:00" /></head>
      <body><div id="dic_area"><p>{_BODY}</p></div></body></html>'''.encode()
    monkeypatch.setattr(url_article_service, "_fetch_html", lambda _: (html, url, "text/html; charset=utf-8"))

    result = url_article_service.prepare_url_article(url)

    assert result["type"] == "article_document"
    assert result["status"] == "ready_for_verification"
    assert result["article_document"]["extractor"] == "NAVER_NEWS_ARTICLE_V1"
    assert result["source"]["contract"] == "release-bound-url-article-adapter-v2"
    assert result["article_document"]["published_date"] == "2026-08-30"


@pytest.mark.parametrize("url", ["http://www.khan.co.kr/article/1", "https://127.0.0.1/article/1", "https://user@www.khan.co.kr/article/1"])
def test_url_validation_rejects_non_public_or_credentialed_url(url: str) -> None:
    with pytest.raises(BackendError) as exc:
        url_article_service._validated_url(url)

    assert exc.value.code == "URL_NOT_SUPPORTED"
