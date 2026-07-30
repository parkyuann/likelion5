"""
근사 표현 허용 오차 판정기 — 축 A(표현 근사) + 축 B(통계 등급) 결합.

축 A — 표현 근사: 뉴스 문장의 근사 표현("3명 중 1명", "약 30만명", "두 배 가까이" 등)을
문맥에 맞춰 정규화된 수치(또는 부등식 경계)로 바꾼 뒤, 같은 표현 유형(ExpressionType)
안에서 공식값과 수치로 비교한다. 이 변환·비교 자체는 어려운 문제가 아니다 — 유형마다
비교 연산자가 다를 뿐이다(EXACT/APPROX/FRACTION은 등식형 오차 비교, FRACTION_BOUND/
THRESHOLD/MULTIPLE_BOUND는 한쪽 방향만 보는 부등식 비교). 실제로 열려 있는 질문은
"표현 유형별 허용오차 숫자를 얼마로 정할 근거가 있느냐"이며, 검색 결과
KOSIS/국가데이터처는 이 축에 대한 공식 기준을 발행하지 않는다(선거 여론조사 보도준칙처럼
특정 영역에 한정된 표본오차 고지 의무는 있으나, 이 프로젝트가 다루는 집계통계 일반
보도에는 해당하지 않음). 그래서 아래 임계값은 실측 사례(idx=2646 등) 기반 프로젝트 설계값이며, 
공식 기준이 아님을 명시해둔다.

축 B — 통계 등급: 그 공식 수치가 조사설계상 얼마나 "정확한" 성격인지(전수/확률표본조사/
유의표본추출)에 따라 축 A의 등식형 판정(EXACT/APPROX/FRACTION/UNIT_CONVERSION)을
보정한다. 부등식형(FRACTION_BOUND/THRESHOLD/MULTIPLE_BOUND)에는 적용하지 않는다 —
표본오차는 "점 추정치가 원래 얼마나 흔들릴 수 있는가"의 문제라 등식 비교에서 의미가
있고, "경계값을 넘었는가"를 묻는 부등식 비교에는 그대로 적용하면 의미가 불분명해지기
때문. 실제 공식 근거는 kosis_statistical_grade.py에 표별로 출처(k-stat.go.kr URL·원문
인용)와 함께 정리해뒀다. 표가 매핑되지 않았거나(table_key=None) 룩업에 없으면 축A만으로
판정하거나 NEEDS_REVIEW로 떨어진다(judge() 참고).

text는 이미 추출된 claim 표현 조각(예: "856만8000명", "세 명 중 한 명은")을 받는다고
가정한다 — 문장 전체에서 관련 없는 다른 숫자(날짜 등)까지 걸러내는 건 claim_extractor의
책임이고, 이 모듈은 그 결과를 받아 판정만 한다.
"""

from dataclasses import dataclass
from enum import Enum
import re

from kosis_statistical_grade import StatGrade, get_grade

# ---------------------------------------------------------------------------
# 판정값 — kosis_schema.py의 verification_results.verdict CHECK 제약(8종) 중
# "공식 수치를 찾은 뒤 숫자를 비교하는" 단계에 해당하는 4종을 그대로 재사용한다.
# ---------------------------------------------------------------------------


class Verdict(Enum):
    MATCH = "MATCH"
    MOSTLY_MATCH = "MOSTLY_MATCH"
    MISMATCH = "MISMATCH"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ExpressionType(Enum):
    EXACT = "exact"                      # "35.5%"
    APPROX = "approx"                    # "약 30만명", "실업률 3%대"
    FRACTION = "fraction"                 # "3명 중 1명"
    FRACTION_BOUND = "fraction_bound"     # "10명 중 3명 이상"
    MULTIPLE = "multiple"                 # "두 배 가까이"
    MULTIPLE_BOUND = "multiple_bound"     # "세 배 이상"
    THRESHOLD = "threshold"               # "절반 이상", "과반"
    UNIT_CONVERSION = "unit_conversion"   # claim_unit != official_unit인 등식형 비교


_BOUND_TYPES = {ExpressionType.FRACTION_BOUND, ExpressionType.THRESHOLD,
                ExpressionType.MULTIPLE_BOUND}


@dataclass
class JudgeResult:
    verdict: Verdict
    expression_type: ExpressionType | None
    claimed_value: float | None
    official_value: float | None
    metric: float | None       # 판정에 쓰인 오차/모자람 수치(설명은 explanation 참고)
    grade: StatGrade | None
    explanation: str


# ---------------------------------------------------------------------------
# 한글 숫자 파싱
# ---------------------------------------------------------------------------

_SMALL_NATIVE = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
                 "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
_SMALL_SINO = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
               "육": 6, "칠": 7, "팔": 8, "구": 9, "십": 10}
_SMALL_WORD_RE_PART = "|".join(
    sorted(list(_SMALL_NATIVE) + list(_SMALL_SINO), key=len, reverse=True)
)


def _small_word_to_number(token: str) -> float | None:
    token = token.strip()
    try:
        return float(token)
    except ValueError:
        pass
    return _SMALL_NATIVE.get(token, _SMALL_SINO.get(token))


_LARGE_UNIT = {"조": 10 ** 12, "억": 10 ** 8, "만": 10 ** 4, "천": 10 ** 3}
_LARGE_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(조|억|만|천)?")


def parse_large_number(text: str) -> float | None:
    """"856만8000", "약 30만", "35.5" 같은 표현에서 원단위로 환산한 수치를 뽑는다.

    "3조5000억"처럼 큰 단위부터 이어붙는 관행을 반영해, 텍스트에 등장하는 (수, 단위)
    조각을 전부 찾아 합산한다.
    """
    total, matched = 0.0, False
    for m in _LARGE_NUMBER_RE.finditer(text):
        num_str, unit = m.groups()
        total += float(num_str) * _LARGE_UNIT.get(unit, 1)
        matched = True
    return total if matched else None


# ---------------------------------------------------------------------------
# 단위 배율(단위환산용) — "856만8000명" vs KOSIS "856.8천명" 같은 순수 산술 변환에 사용.
# parse_large_number가 이미 만/천을 원단위로 풀어내므로, 여기서는 official_value가
# claim과 다른 배율 단위(예: 천명)로 주어졌을 때 그것만 원단위로 맞춰준다.
# ---------------------------------------------------------------------------

_UNIT_MULTIPLIER = {"명": 1, "천명": 1_000, "만명": 10_000,
                    "건": 1, "천건": 1_000, "만건": 10_000,
                    "원": 1, "만원": 10_000, "억원": 100_000_000}


def convert_unit(value: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return value
    if from_unit not in _UNIT_MULTIPLIER or to_unit not in _UNIT_MULTIPLIER:
        raise ValueError(f"변환 불가능한 단위 조합: {from_unit} -> {to_unit}")
    return value * _UNIT_MULTIPLIER[from_unit] / _UNIT_MULTIPLIER[to_unit]


# ---------------------------------------------------------------------------
# 표현 유형 분류 + 파싱
# ---------------------------------------------------------------------------

_FRACTION_RE = re.compile(
    rf"(\d+|{_SMALL_WORD_RE_PART})\s*명?\s*중\s*(\d+|{_SMALL_WORD_RE_PART})\s*명?"
    rf"\s*(이상|이하|초과|미만)?"
)
_MULTIPLE_RE = re.compile(
    rf"(\d+(?:\.\d+)?|{_SMALL_WORD_RE_PART})\s*배\s*(가까이|이상|넘게|초과)?"
)
_THRESHOLD_RE = re.compile(r"(절반|과반)\s*(이상|이하)?")
_APPROX_RE = re.compile(r"(약|가량|내외|대)")


def classify_expression(text: str) -> ExpressionType:
    """우선순위: 분수형(+경계) > 배수형(+경계) > 임계값 > 약·가량 > 정확한 수치."""
    frac = _FRACTION_RE.search(text)
    if frac:
        return ExpressionType.FRACTION_BOUND if frac.group(3) else ExpressionType.FRACTION
    mult = _MULTIPLE_RE.search(text)
    if mult:
        return ExpressionType.MULTIPLE_BOUND if mult.group(2) in ("이상", "넘게", "초과") \
            else ExpressionType.MULTIPLE
    if _THRESHOLD_RE.search(text):
        return ExpressionType.THRESHOLD
    if _APPROX_RE.search(text):
        return ExpressionType.APPROX
    return ExpressionType.EXACT


@dataclass
class ParsedClaim:
    """표현을 정규화한 결과. point는 등식형 비교용, bound/direction은 부등식형 비교용."""
    point: float | None = None
    bound: float | None = None
    direction: str | None = None   # "at_least" | "at_most"


def parse_fraction(text: str) -> ParsedClaim | None:
    m = _FRACTION_RE.search(text)
    if not m:
        return None
    denom, numer, qualifier = _small_word_to_number(m.group(1)), _small_word_to_number(m.group(2)), m.group(3)
    if denom is None or numer is None or denom == 0:
        return None
    pct = numer / denom * 100
    if qualifier in ("이상", "초과"):
        return ParsedClaim(bound=pct, direction="at_least")
    if qualifier in ("이하", "미만"):
        return ParsedClaim(bound=pct, direction="at_most")
    return ParsedClaim(point=pct)


def parse_multiple(text: str) -> ParsedClaim | None:
    m = _MULTIPLE_RE.search(text)
    if not m:
        return None
    value, qualifier = _small_word_to_number(m.group(1)), m.group(2)
    if value is None:
        return None
    if qualifier in ("이상", "넘게", "초과"):
        return ParsedClaim(bound=value, direction="at_least")
    return ParsedClaim(point=value)  # "가까이" 또는 수식어 없음 — 등식형(근접)으로 처리


def parse_threshold(text: str) -> ParsedClaim | None:
    m = _THRESHOLD_RE.search(text)
    if not m:
        return None
    qualifier = m.group(2) or "이상"
    return ParsedClaim(bound=50.0, direction="at_least" if qualifier == "이상" else "at_most")


def parse_point_number(text: str) -> ParsedClaim | None:
    value = parse_large_number(text)
    return ParsedClaim(point=value) if value is not None else None


_PARSERS = {
    ExpressionType.FRACTION: parse_fraction,
    ExpressionType.FRACTION_BOUND: parse_fraction,
    ExpressionType.MULTIPLE: parse_multiple,
    ExpressionType.MULTIPLE_BOUND: parse_multiple,
    ExpressionType.THRESHOLD: parse_threshold,
    ExpressionType.APPROX: parse_point_number,
    ExpressionType.EXACT: parse_point_number,
    ExpressionType.UNIT_CONVERSION: parse_point_number,
}


# ---------------------------------------------------------------------------
# 축 A 허용오차표 (프로젝트 설계값 — 공식 KOSIS 기준 아님, 위 모듈 docstring 참고)
# ---------------------------------------------------------------------------

# 등식형: (match_threshold, mostly_match_threshold 또는 None, 상대오차 여부)
_EQUALITY_TOLERANCE = {
    ExpressionType.EXACT: (0.05, None, False),           # 절대오차(pp) — 반올림 오차만
    ExpressionType.APPROX: (3.0, 8.0, True),             # 상대오차(%)
    ExpressionType.FRACTION: (1.0, 3.0, False),          # 절대오차(pp)
    ExpressionType.MULTIPLE: (5.0, 20.0, True),          # 상대오차(%, 배수 기준)
    ExpressionType.UNIT_CONVERSION: (1.0, None, True),   # 상대오차(%) — 순수 산술 변환
}

# 부등식형: 경계값보다 실제가 얼마나 모자라도 봐줄까
_BOUND_TOLERANCE_PP = {"match": 1.0, "mostly_match": 2.0}      # FRACTION_BOUND, THRESHOLD
_BOUND_TOLERANCE_REL_PCT = {"match": 3.0, "mostly_match": 8.0}  # MULTIPLE_BOUND(배수 기준)

# 축 B 등급별 축A 보정(등식형에만 적용) — SAMPLE_PROBABILITY는 공식 목표 RSE
# (kosis_statistical_grade)를 그대로 허용폭에 추가. SAMPLE_PURPOSIVE는 공식 수치가 없어
# 프로젝트가 보수적으로 잡은 기본값(공식 기준 아님, 명시적으로 라벨링)을 쓴다.
# CENSUS_ADMIN/MIXED는 보정하지 않는다.
_PURPOSIVE_DEFAULT_EXTRA_RSE_PCT = 1.0


def _grade_extra_rse(grade_info) -> tuple[float | None, str]:
    if grade_info is None:
        return None, ""
    if grade_info.grade == StatGrade.SAMPLE_PROBABILITY and grade_info.target_rse_pct:
        return grade_info.target_rse_pct, (
            f"축B: {grade_info.survey_name}의 공식 목표 상대표준오차(RSE) "
            f"{grade_info.target_rse_pct}%를 허용폭에 추가 반영."
        )
    if grade_info.grade == StatGrade.SAMPLE_PURPOSIVE:
        return _PURPOSIVE_DEFAULT_EXTRA_RSE_PCT, (
            f"축B: {grade_info.survey_name}은 유의표본추출이라 공식 RSE 미공표 — "
            f"프로젝트 기본값(상대오차 {_PURPOSIVE_DEFAULT_EXTRA_RSE_PCT}%)을 보수적으로 적용(공식 기준 아님)."
        )
    return None, f"축B: {grade_info.survey_name} — 전수/혼합 등급이라 축A 임계값을 보정하지 않음."


def judge(
    text: str,
    official_value: float | None,
    *,
    claim_unit: str | None = None,
    official_unit: str | None = None,
    table_key: str | None = None,
) -> JudgeResult:
    """claim 원문 조각(text)과 공식값(official_value)을 대조해 판정한다.

    table_key를 넘기면 축B(kosis_statistical_grade)를 함께 반영한다. table_key를
    넘겼는데 룩업에 없으면(get_grade가 None) 명시적으로 NEEDS_REVIEW로 처리한다 —
    "축B를 쓰고 싶었는데 근거가 없다"는 상태를 "안 쓴 것"과 구분하기 위함이다.
    table_key를 아예 넘기지 않으면(None) 축A만으로 판정한다.
    """
    expr_type = classify_expression(text)
    parsed = _PARSERS[expr_type](text)

    if official_value is None or parsed is None:
        return JudgeResult(Verdict.NEEDS_REVIEW, expr_type, None, official_value,
                            None, None,
                            "공식값이 없거나 claim 표현을 파싱하지 못해 판정 불가.")

    grade_info, extra_rse_pct, grade_note = None, None, ""
    if table_key is not None:
        grade_info = get_grade(table_key)
        if grade_info is None:
            return JudgeResult(
                Verdict.NEEDS_REVIEW, expr_type, parsed.point, official_value, None, None,
                f"table_key={table_key}에 대한 축B 통계 등급 근거가 아직 없음 "
                "(kosis_statistical_grade.py 룩업 미등록) — 임의로 등급을 추정하지 않고 NEEDS_REVIEW 처리.",
            )
        if expr_type not in _BOUND_TYPES:
            extra_rse_pct, grade_note = _grade_extra_rse(grade_info)

    off_value = official_value
    if claim_unit and official_unit and claim_unit != official_unit:
        try:
            off_value = convert_unit(official_value, official_unit, claim_unit)
        except ValueError as exc:
            return JudgeResult(Verdict.NEEDS_REVIEW, expr_type, parsed.point,
                                official_value, None, grade_info and grade_info.grade,
                                f"단위 변환 실패: {exc}")
        expr_type = ExpressionType.UNIT_CONVERSION

    # --- 부등식형(FRACTION_BOUND / THRESHOLD / MULTIPLE_BOUND) ---
    if parsed.bound is not None and parsed.direction is not None:
        bound, direction = parsed.bound, parsed.direction
        shortfall = (bound - off_value) if direction == "at_least" else (off_value - bound)
        shortfall = max(shortfall, 0.0)

        if expr_type == ExpressionType.MULTIPLE_BOUND:
            metric = shortfall / bound * 100 if bound else 0.0
            th = _BOUND_TOLERANCE_REL_PCT
        else:
            metric = shortfall
            th = _BOUND_TOLERANCE_PP

        if metric <= th["match"]:
            verdict = Verdict.MATCH
        elif metric <= th["mostly_match"]:
            verdict = Verdict.MOSTLY_MATCH
        else:
            verdict = Verdict.MISMATCH

        explanation = (
            f"{expr_type.value}: 경계값={bound}, 방향={direction}, 실제값={off_value}, "
            f"모자람={metric:.2f}{'%' if expr_type == ExpressionType.MULTIPLE_BOUND else 'pp'}."
        )
        return JudgeResult(verdict, expr_type, bound, official_value, metric,
                            grade_info and grade_info.grade, explanation)

    # --- 등식형(EXACT / APPROX / FRACTION / MULTIPLE / UNIT_CONVERSION) ---
    claimed = parsed.point
    match_th, mostly_th, is_relative = _EQUALITY_TOLERANCE[expr_type]
    abs_error = abs(claimed - off_value)
    rel_error = (abs_error / off_value * 100) if off_value else None

    effective_match_th = match_th
    if extra_rse_pct is not None:
        effective_match_th += extra_rse_pct if is_relative else off_value * extra_rse_pct / 100

    metric = rel_error if is_relative else abs_error
    if metric <= effective_match_th:
        verdict = Verdict.MATCH
    elif mostly_th is not None and metric <= mostly_th:
        verdict = Verdict.MOSTLY_MATCH
    else:
        verdict = Verdict.MISMATCH

    explanation = (
        f"{expr_type.value}: claim={claimed}, official={off_value}, "
        f"abs_error={abs_error:.4f}, "
        f"rel_error={'%.2f%%' % rel_error if rel_error is not None else 'N/A'}, "
        f"match_threshold(보정후)={effective_match_th:.4f}. {grade_note}"
    )
    return JudgeResult(verdict, expr_type, claimed, official_value, metric,
                        grade_info and grade_info.grade, explanation)
