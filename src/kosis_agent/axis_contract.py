# -*- coding: utf-8 -*-
"""계약(contract) 추출 + 축 역할·값 매칭.

주장 문장에서 "어떤 분류축에 어떤 값을 요구하는지"(예: 지역=대구, 성별=여성,
연령=30대)를 계약으로 뽑고, 통계표의 축 이름을 역할로 분류해 그 축값이 계약을
충족하는지 판정한다. 표 선택 값순회의 ① 계약 매칭 단계가 이 로직을 쓴다.
"""
from __future__ import annotations
import re

# ── 축 이름 → 역할 분류 ────────────────────────────────────────────
ROLE_PATTERNS = [
    ("sex", re.compile(r"성별|남녀")),
    ("age", re.compile(r"연령|나이|세대")),
    ("region", re.compile(r"지역|행정구역|시도|시군구|소재지|권역")),
    ("industry", re.compile(r"산업|업종|경제활동|직업")),
    ("enterprise_size", re.compile(r"기업규모|사업체규모|종사자규모|규모별")),
    ("income", re.compile(r"소득|분위")),
    ("education", re.compile(r"교육|학력|학교")),
    ("household", re.compile(r"가구|가구주|가구원")),
    ("product", re.compile(r"품목|재화|상품|수출입")),
    ("country", re.compile(r"국가|대륙")),
]

# ── 계약 어휘 사전 ─────────────────────────────────────────────────
REGIONS = ["전국", "수도권", "비수도권", "서울", "경기", "인천", "부산", "대구", "광주",
           "대전", "울산", "세종", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]
SEXES = ["여성", "남성", "여자", "남자"]
SIZES = ["대기업", "중견기업", "중소기업", "소기업", "소상공인", "대규모", "중규모", "소규모"]
HOUSEHOLDS = ["1인가구", "1인 가구", "다문화가구", "다문화 가구", "맞벌이가구", "맞벌이 가구"]
INDUSTRIES = ["제조업", "서비스업", "건설업", "농림어업", "광업", "도소매업", "운수업",
              "숙박음식점업", "금융보험업", "정보통신업", "부동산업", "보건복지업"]


def compact(value: str) -> str:
    """표기 정규화 — 대소문자·구분자 제거, 성별·행정구역 표기 통일."""
    text = str(value or "").lower()
    text = text.replace("여성", "여자").replace("남성", "남자")
    text = re.sub(r"[\s·ㆍ,()（）_/]", "", text)
    text = text.replace("특별자치도", "도").replace("특별자치시", "시")
    text = text.replace("광역시", "시").replace("특별시", "시")
    return text


def axis_role(name: str) -> str:
    """축 이름을 역할(sex/age/region/...)로 분류. 미분류는 'other:<정규화>'."""
    normalized = compact(name)
    for role, pattern in ROLE_PATTERNS:
        if pattern.search(normalized):
            return role
    return f"other:{normalized}"


def age_satisfies(required: str, label: str) -> bool:
    """연령 계약 충족 — 'N대'는 시작 10년 범위, 그 외는 정규화 포함 비교."""
    req_n = [int(v) for v in re.findall(r"\d+", required)]
    lab_n = [int(v) for v in re.findall(r"\d+", label)]
    if "대" in required and req_n and lab_n:
        decade = req_n[0]
        return lab_n[0] == decade and (len(lab_n) == 1 or lab_n[-1] <= decade + 9)
    return compact(required) == compact(label) or compact(required) in compact(label)


def value_satisfies(role: str, required: str, label: str) -> bool:
    """축값 라벨이 계약 값을 충족하는지 — 역할별 규칙."""
    left, right = compact(required), compact(label)
    if role == "age":
        return age_satisfies(required, label)
    if role == "region":
        return left in right or right in left
    return left == right or (len(left) >= 3 and left in right) or (len(right) >= 3 and right in left)


def _find_terms(claim: str, values: list[str], role: str) -> list[dict]:
    normalized = compact(claim)
    return [{"role": role, "value": v} for v in values if compact(v) in normalized]


def extract_contract(claim: str) -> list[dict]:
    """주장에서 계약 목록 추출 — [{'role','value'}...] (중복 제거)."""
    cons = []
    cons += _find_terms(claim, REGIONS, "region")
    cons += _find_terms(claim, SEXES, "sex")
    cons += _find_terms(claim, SIZES, "enterprise_size")
    cons += _find_terms(claim, HOUSEHOLDS, "household")
    cons += _find_terms(claim, INDUSTRIES, "industry")
    for m in re.finditer(r"\d{1,2}\s*대(?:\s*(?:초반|중반|후반))?|\d{1,3}\s*세\s*(?:이상|이하|미만|초과)|\d{1,3}\s*[~-]\s*\d{1,3}\s*세", claim):
        cons.append({"role": "age", "value": m.group(0)})
    for m in re.finditer(r"(?:상위|하위)\s*\d+(?:%|퍼센트|개)|\d+\s*분위", claim):
        cons.append({"role": "income", "value": m.group(0)})
    uniq = {}
    for c in cons:
        uniq[(c["role"], compact(c["value"]))] = c
    return list(uniq.values())


def extract_contract_expanded(claim: str) -> list[dict]:
    """연쇄 연령대 표기('10·20대')를 개별 연령대로 확장한 뒤 계약 추출."""
    expanded = claim
    for m in re.finditer(r"(\d{1,2})\s*[·ㆍ]\s*(\d{1,2})\s*대", claim):
        expanded += f" {m.group(1)}대 {m.group(2)}대"
    return extract_contract(expanded)
