"""
뉴스 주장의 상대 시점/단위를 KOSIS 조회에 쓸 수 있는 절대값으로 정규화한다.

`mentoring_questions.md`/`claim_extraction_schema.md`에 "보류 중"으로 남겨뒀던
두 가지 — ① time_ref(지난달/작년 등)를 KOSIS API 파라미터로 바꾸는 규칙,
② 단위 환산(만 명→명, 억원→원, %와 %p 구분) — 을 채운다.

기준점은 반드시 "기사 발행일"이어야 한다. 현재 날짜를 기준으로 잡으면 과거
기사의 "올해"/"작년"이 틀린 연도로 바뀐다.

사용 예:
    from claim_normalizer import resolve_relative_time, convert_value
    resolve_relative_time("지난해 실업률은 3%였다", "2024-03-02")  # -> "2023년 실업률은 3%였다"
    convert_value(5.2, "만명", "명")  # -> 52000.0
"""
import re

import pandas as pd

_MONTH_RE = re.compile(r"지난\s?(\d{1,2})월")


def resolve_relative_time(text: str, published_at) -> str:
    """상대 시점 표현을 기사 발행일 기준 절대 시점으로 치환한다.

    치환 순서가 중요하다 — "지난달"을 "지난해"보다 먼저 처리하면 "지난해"
    안의 "지난"이 먼저 걸려 깨지므로, 긴 표현(지난달/재작년)을 짧은 표현
    (작년/올해)보다 먼저 치환한다.
    """
    published = pd.Timestamp(published_at)
    text = str(text)

    # "지난 3월"처럼 구체적 월이 박힌 표현 -> 발행일 기준 그 월(올해/작년 자동 판단)
    def _replace_month(m: re.Match) -> str:
        month = int(m.group(1))
        year = published.year if month <= published.month else published.year - 1
        return f"{year}.{month:02d}"

    text = _MONTH_RE.sub(_replace_month, text)

    ordered_replacements = [
        ("지난달", (published - pd.DateOffset(months=1)).strftime("%Y.%m")),
        ("이번 달", f"{published.year}.{published.month:02d}"),
        ("이번달", f"{published.year}.{published.month:02d}"),
        ("이달", f"{published.year}.{published.month:02d}"),
        ("재작년", str(published.year - 2)),
        ("지난해", str(published.year - 1)),
        ("작년", str(published.year - 1)),
        ("올해", str(published.year)),
        ("금년", str(published.year)),
    ]
    for source, target in ordered_replacements:
        text = text.replace(source, target)
    return text


# 단위 -> 기준 단위(명/원) 환산 배수. %/%p는 수준(level) 개념이 다른 값이라
# 절대 서로/기준단위로 환산하지 않는다(convert_value에서 별도 처리).
UNIT_MULTIPLIER = {
    "명": 1, "천명": 1_000, "만명": 10_000,
    "원": 1, "만원": 10_000, "억원": 100_000_000, "조원": 1_000_000_000_000,
}
_RATE_UNITS = {"%", "%p"}


def convert_value(value: float, source_unit: str, target_unit: str) -> float:
    """단위를 환산한다. %/%p는 절대 자동 변환하지 않는다(둘은 다른 개념:
    %는 비율 자체, %p는 비율의 변화폭 — 자동 동일시하면 안 됨)."""
    if source_unit in _RATE_UNITS or target_unit in _RATE_UNITS:
        if source_unit != target_unit:
            raise ValueError(f"%와 %p는 자동 변환하지 않습니다: {source_unit} -> {target_unit}")
        return value
    if source_unit not in UNIT_MULTIPLIER or target_unit not in UNIT_MULTIPLIER:
        raise ValueError(f"환산 규칙이 없는 단위입니다: {source_unit} -> {target_unit}")
    return value * UNIT_MULTIPLIER[source_unit] / UNIT_MULTIPLIER[target_unit]
