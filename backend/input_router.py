"""간단한 통계 질의와 기사 입력을 결정론적으로 구분한다."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit


class RouteType(str, Enum):
    SIMPLE_KOSIS_QUERY = "simple_kosis_query"
    ARTICLE_URL = "article_url"
    ARTICLE_TEXT = "article_text"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class RouteDecision:
    route: RouteType
    confidence: float
    reason_code: str
    # LLM 라우터가 재구성한 질문 등 부가 정보(예: {"questions": [...], "via": "hcx"}).
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "route": self.route.value,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }
        if self.extra:
            data["extra"] = self.extra
        return data


_STATISTICS_TERMS = (
    "인구", "출생", "사망", "혼인", "이혼", "취업", "고용", "실업",
    "물가", "소득", "가구", "사업체", "수출", "수입", "생산", "소비",
    "GDP", "국내총생산", "통계", "증가율", "감소율", "비율", "비중",
    "지수", "인원", "명", "건", "%", "퍼센트",
)
_QUERY_TERMS = (
    "얼마", "몇", "알려", "조회", "찾아", "증가", "감소", "높", "낮",
    "비교", "인가", "인가요", "했나", "했나요", "어디", "언제", "?",
)
_ARTICLE_SIGNALS = (
    "기자", "입력", "보도했다", "밝혔다", "전했다", "따르면", "뉴스",
)
_SENTENCE_END_RE = re.compile(r"[.!?。](?:\s|$)")


def is_http_url(value: str) -> bool:
    """입력 전체가 공개 HTTP(S) URL 형태인지 네트워크 없이 판별한다."""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def is_kosis_eligible_question(text: str) -> bool:
    """KOSIS에 직접 질의할 가능성이 높은 짧은 통계 질문인지 보수적으로 검사한다."""
    normalized = " ".join(text.split())
    if not normalized or len(normalized) > 300:
        return False
    if len(_SENTENCE_END_RE.findall(normalized)) > 2:
        return False
    has_statistic = any(term.lower() in normalized.lower() for term in _STATISTICS_TERMS)
    has_query_intent = any(term in normalized for term in _QUERY_TERMS)
    return has_statistic and has_query_intent


def looks_like_article(text: str) -> bool:
    normalized = text.strip()
    if len(normalized) >= 400 or normalized.count("\n") >= 2:
        return True
    sentence_count = len(_SENTENCE_END_RE.findall(normalized))
    return sentence_count >= 3 or any(signal in normalized for signal in _ARTICLE_SIGNALS)


def is_obvious_junk(text: str) -> bool:
    """LLM 없이 즉시 거절해도 안전한 '명백한 비통계 잡담'인지 판단한다.

    보수적으로만 True: 짧고, 숫자도 통계용어도 없고, 기사 구조도 아닐 때만.
    통계 신호가 조금이라도 있으면(숫자·통계용어) False → LLM이 판단하게 둔다."""
    normalized = " ".join(text.split())
    if not normalized or len(normalized) > 40:
        return False  # 길면 기사일 수 있으니 LLM에 맡긴다
    if any(ch.isdigit() for ch in normalized):
        return False  # 연도·수치가 있으면 통계 질의일 수 있다
    if any(term.lower() in normalized.lower() for term in _STATISTICS_TERMS):
        return False
    if looks_like_article(normalized):
        return False
    return True


def route_input(text: str, *, input_type: str = "auto") -> RouteDecision:
    """명시된 입력 유형을 우선하고 auto일 때만 내용 기반으로 판별한다."""
    value = text.strip()
    if input_type == "url":
        return RouteDecision(RouteType.ARTICLE_URL, 1.0, "EXPLICIT_URL")
    if input_type == "query":
        if is_kosis_eligible_question(value):
            return RouteDecision(RouteType.SIMPLE_KOSIS_QUERY, 1.0, "EXPLICIT_KOSIS_QUERY")
        return RouteDecision(RouteType.OUT_OF_SCOPE, 1.0, "EXPLICIT_QUERY_NOT_KOSIS_ELIGIBLE")
    if input_type == "article":
        return RouteDecision(RouteType.ARTICLE_TEXT, 1.0, "EXPLICIT_ARTICLE_TEXT")

    if is_http_url(value):
        return RouteDecision(RouteType.ARTICLE_URL, 1.0, "HTTP_URL")
    if is_kosis_eligible_question(value):
        return RouteDecision(RouteType.SIMPLE_KOSIS_QUERY, 0.9, "SHORT_STATISTICAL_QUESTION")
    if looks_like_article(value):
        return RouteDecision(RouteType.ARTICLE_TEXT, 0.85, "ARTICLE_STRUCTURE")
    return RouteDecision(RouteType.OUT_OF_SCOPE, 0.8, "NO_SUPPORTED_ROUTE")
