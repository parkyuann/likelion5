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


# --- 한국어 수사(조/억/만/천) 전개 -----------------------------------------
_KNUM_UNITS = (("조", 10 ** 12), ("억", 10 ** 8), ("만", 10 ** 4), ("천", 10 ** 3))


def parse_korean_number(s: str):
    """'1조8000억' -> 1.8e12, '92억' -> 9.2e9, '308만9300' -> 3089300, '100.4' -> 100.4.
    '62조9천444억5700만'처럼 큰 자리 계수 안에 작은 자리(천)가 중첩된 경우도 처리한다.

    조→억→만→천 순으로 큰 자리부터 순차 분해하되(뉴스 수치는 항상 내림차순 표기), 각 자리의
    계수(head)에 다시 작은 단위가 섞여 있으면 재귀로 전개한다('9천444' -> 9,444). 음수 부호
    ('-'/'−')와 콤마·공백은 허용. 파싱 불가하면 None."""
    if s is None:
        return None
    rest = str(s).replace(",", "").replace(" ", "").replace("−", "-").strip()
    if not rest:
        return None
    sign = 1.0
    if rest[0] == "-":
        sign, rest = -1.0, rest[1:]
    total = 0.0
    matched = False
    for tok, mult in _KNUM_UNITS:
        if tok in rest:
            head, rest = rest.split(tok, 1)
            head_val = parse_korean_number(head) if head else 1.0  # 중첩 계수(9천444 등) 재귀 전개
            if head_val is None:
                return None
            total += head_val * mult
            matched = True
    if rest:
        try:
            total += float(rest)
            matched = True
        except ValueError:
            if not matched:
                return None
    return sign * total if matched else None


# --- 기준 시점 세분(time_span_type) ----------------------------------------
_PARTIAL_DAY_RE = re.compile(r"\d{1,2}\s?~\s?\d{1,2}\s?일")


def time_span_type(time_ref: str):
    """time_ref 원문에서 시점 세분을 도출한다: 분기 / 월 / 연 / 부분기간 / 특정일 / 불명확."""
    if not time_ref:
        return "불명확"
    t = str(time_ref)
    if "분기" in t:
        return "분기"
    if _PARTIAL_DAY_RE.search(t):
        return "부분기간"
    if "월" in t or any(w in t for w in ("지난달", "이번달", "이번 달", "이달", "전월", "동월")):
        return "월"
    if "년" in t or any(w in t for w in ("작년", "지난해", "재작년", "올해", "금년", "전년", "전분기", "동기")):
        return "연"
    if "일" in t:
        return "특정일"
    return "불명확"


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


def normalize_value(value_str: str, unit: str):
    """추출된 값 문자열(예: '92억')을 실수로 전개한다. 값-단위 접두(만/억/조)를 풀어
    수치를 반환하되, 단위 자체는 추출 단계에서 이미 기준 단위(원/명/위안/% 등)이므로
    그대로 둔다. 파싱 실패 시 None."""
    return parse_korean_number(value_str)


_QUARTER_RE = re.compile(r"(\d)\s?분기")
_BARE_MONTH_RE = re.compile(r"(?<!\d)(\d{1,2})월")


def normalize_time_ref(time_ref: str, published_at):
    """time_ref 원문을 발행일 기준 절대 시점으로 만든다.

    - '작년'/'올해' 등 상대어는 resolve_relative_time으로 연도 치환
    - 연도 없는 'N분기'/'N월'은 발행일 기준 연도를 앞에 붙인다(그 시점이 발행월보다
      뒤면 작년으로 판단 — _replace_month와 같은 규칙)
    반환: 절대화된 문자열(없으면 None)."""
    if not time_ref:
        return None
    published = pd.Timestamp(published_at)
    t = resolve_relative_time(str(time_ref), published_at)

    # 이미 연도가 붙었으면 그대로
    if re.search(r"\d{4}", t):
        return t

    mq = _QUARTER_RE.search(t)
    if mq:
        q = int(mq.group(1))
        pub_q = (published.month - 1) // 3 + 1
        year = published.year if q <= pub_q else published.year - 1
        return f"{year}년 {q}분기"

    mm = _BARE_MONTH_RE.search(t)
    if mm:
        month = int(mm.group(1))
        year = published.year if month <= published.month else published.year - 1
        return f"{year}.{month:02d}"
    return t

