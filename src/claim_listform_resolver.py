"""
나열형 문장(idx=2655: "17만1000명, 16만6000명, 31만2000명… 7~9월 월별 취업자 수")의
값-시점 대응 후처리. `claim_extractor.py`(정규식 코어)는 건드리지 않고, 그 출력
(`data/claim_candidates_full.csv`)을 입력받아 컬럼을 덧붙이는 별도 모듈로 분리했다 —
코어 추출 로직과 값-시점 정렬 로직을 섞으면 각각 따로 테스트하기 어려워지기 때문.

방법(claim_extraction_schema.md 4장에 명시된 한계를 메우는 첫 시도):
1. 문장에서 값/단위 쌍을 다시 뽑되(`claim_extractor.VALUE_UNIT_RE` 재사용), 4자리 연도
   + "년" 조합("2025년" 등 날짜 표기가 값으로 같이 잡히는 문제, claim_extraction_full_results.md
   4장에 명시된 한계)은 걸러낸다.
2. 문장에서 "N~M월" 범위 표현을 개별 월 목록으로 전개한다.
3. 정렬 휴리스틱: 나열된 값의 등장 순서 = 전개된 월의 오름차순 순서(idx=2655 실측 확인:
   3번째 값 31만2000명이 9월과 일치 — 기사 본문 뒷부분에서 직접 확인됨).
4. 값 개수와 월 개수가 안 맞으면 강제로 맞추지 않고 COUNT_MISMATCH로 남겨 사람 검토로 넘긴다.
5. **(실측으로 추가)** 값 개수가 우연히 월 개수와 같아도, 값들이 실제로 쉼표 등으로 촘촘히
   나열돼 있지 않으면(예: "2023년 1~3월 X톤에서 지난해 Y톤으로 늘더니 올해엔 Z톤"처럼 서로
   다른 시점을 각자 따로 언급하는 문장) 정렬을 신뢰할 수 없다 — 값 사이 문자 간격이 넓으면
   LOW_CONFIDENCE로 남긴다. idx=2655류 진짜 나열형은 값들이 ", "로 바로 붙어 있다는 점에서
   착안(전 항목 간격 5자 이내).
6. **(실측으로 추가, 완화 필터 이후)** 값들의 단위가 전부 동일해야 ALIGNED를 준다 — 월별
   나열은 같은 지표의 반복이므로 단위가 같을 수밖에 없다(idx=2655: 명/명/명). 완화 필터로
   후보가 늘어난 뒤 "1~2월 합계 판매량은 8% 증가한 11만6535대"(%/대), "4~5월 관세로 12억
   달러(약 1조 1700억원)"(달러/원)처럼 서로 성격이 다른 값들이 개수만 우연히 월 개수와
   일치해 ALIGNED로 오판되는 사례 2건이 실측 확인됨 — 단위가 섞여 있으면 LOW_CONFIDENCE로
   남긴다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/claim_listform_resolver.py
"""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claim_extractor import VALUE_UNIT_RE  # noqa: E402

INPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_relaxed.csv"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_listform.csv"

MONTH_RANGE_RE = re.compile(r"(\d{1,2})\s?~\s?(\d{1,2})\s?월")
_YEAR_TOKEN_RE = re.compile(r"^(19|20)\d{2}$")


def _is_year_noise(value: str, unit: str) -> bool:
    """'2025년'처럼 날짜 표기가 값으로 같이 잡힌 경우(claim_extraction_full_results.md 4장
    한계)를 걸러낸다. 4자리 연도(1900~2099) + 단위 '년' 조합만 노이즈로 간주 — 그 외
    '10년 만에' 같은 정상적인 기간 표현(1~3자리)은 살려둔다."""
    return unit == "년" and bool(_YEAR_TOKEN_RE.match(value.strip()))


def _is_quarter_label_noise(value: str, unit: str) -> bool:
    """'작년 3분기(7~9월)'처럼 '3분기'가 실제 지표 값이 아니라 시점 라벨(1~4분기)로
    쓰인 경우를 걸러낸다. 실측(idx=2655 계열 기사)에서 값-시점 정렬이 '4분기'를 하나의
    값으로 오인해 뒤따르는 월 목록과 억지로 대응시키는 오탐이 확인됨 — 1~4 사이 정수 +
    단위 '분기' 조합만 노이즈로 간주한다."""
    return unit == "분기" and value.strip() in {"1", "2", "3", "4"}


def extract_clean_values(sentence: str) -> list[tuple[str, str]]:
    """연도·분기 라벨 노이즈를 제거한 (value, unit) 쌍 리스트."""
    return [(v.strip(), u.strip()) for v, u, _s, _e in _extract_clean_value_spans(sentence)]


def _extract_clean_value_spans(sentence: str) -> list[tuple[str, str, int, int]]:
    """(value, unit, start, end) — 오탐 방지 판정(간격 체크)에 위치 정보가 필요해 별도로 둠."""
    out = []
    for m in VALUE_UNIT_RE.finditer(sentence):
        v, u = m.group("value").strip(), m.group("unit").strip()
        if _is_year_noise(v, u) or _is_quarter_label_noise(v, u):
            continue
        out.append((v, u, m.start(), m.end()))
    return out


MAX_LIST_GAP = 5  # 나열형 값 사이 최대 허용 간격(문자 수) — idx=2655 실측: ", " 등 2~3자


def _is_tight_list(spans: list[tuple[str, str, int, int]]) -> bool:
    """값들이 실제로 쉼표 등으로 촘촘히 나열돼 있는지 확인 — 서로 다른 시점을 각자 따로
    언급하는 문장(값 사이에 긴 서술이 낀 경우)을 걸러내기 위한 가드."""
    gaps = [spans[i + 1][2] - spans[i][3] for i in range(len(spans) - 1)]
    return all(gap <= MAX_LIST_GAP for gap in gaps)


def expand_month_range(sentence: str) -> list[str]:
    """'7~9월' -> ['7월', '8월', '9월']. 매치가 여러 개면 전부 이어붙인다(드문 경우).

    단, '1분기(1~3월)'처럼 분기를 월로 환산해 괄호로 풀어쓴 표현은 제외한다 — 이건 값 여러
    개가 각 달에 대응한다는 뜻이 아니라 분기 하나를 가리키는 보조 설명이라, 실측에서 이걸
    나열형으로 오인해 서로 무관한 값들(증가율%·달러·원화 환산 등)을 억지로 1/2/3월에
    대응시키는 오탐이 확인됨."""
    labels = []
    for match in MONTH_RANGE_RE.finditer(sentence):
        preceding = sentence[max(0, match.start() - 4):match.start()]
        if "분기(" in preceding or preceding.endswith("분기("):
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if start <= end and end - start <= 11:  # 역방향/비정상 범위(연도 오탐 등) 방어
            labels.extend(f"{month}월" for month in range(start, end + 1))
    return labels


def resolve_list_form(sentence: str) -> dict:
    """반환: {'time_ref_list': str|None, 'list_alignment_status': str}

    상태값: ALIGNED(정렬 성공) / COUNT_MISMATCH(값-월 개수 불일치) /
    LOW_CONFIDENCE(개수는 맞지만 값들이 촘촘한 나열 형태가 아님) / NOT_LIST_FORM(애초에 대상 아님)."""
    spans = _extract_clean_value_spans(sentence)
    if len(spans) < 2:
        return {"time_ref_list": None, "list_alignment_status": "NOT_LIST_FORM"}

    month_labels = expand_month_range(sentence)
    if not month_labels:
        return {"time_ref_list": None, "list_alignment_status": "NOT_LIST_FORM"}

    if len(spans) != len(month_labels):
        return {"time_ref_list": None, "list_alignment_status": "COUNT_MISMATCH"}

    if not _is_tight_list(spans):
        return {"time_ref_list": None, "list_alignment_status": "LOW_CONFIDENCE"}

    units = {u for _v, u, _s, _e in spans}
    if len(units) != 1:  # 월별 나열은 같은 지표의 반복 — 단위가 섞이면 신뢰 불가(docstring 6번)
        return {"time_ref_list": None, "list_alignment_status": "LOW_CONFIDENCE"}

    return {"time_ref_list": ";".join(month_labels), "list_alignment_status": "ALIGNED"}


def main():
    df = pd.read_csv(INPUT_PATH, encoding="utf-8-sig")
    target = df["value_count"] >= 2

    results = df.loc[target, "claim_text"].apply(resolve_list_form)
    df["time_ref_list"] = None
    df["list_alignment_status"] = "NOT_LIST_FORM"
    df.loc[target, "time_ref_list"] = results.apply(lambda r: r["time_ref_list"])
    df.loc[target, "list_alignment_status"] = results.apply(lambda r: r["list_alignment_status"])
    df.loc[~target, "list_alignment_status"] = "SINGLE_VALUE"

    df.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

    print(f"value_count>=2 후보 {target.sum()}건 중:")
    print(df.loc[target, "list_alignment_status"].value_counts())
    print(f"저장 위치: {OUTPUT_PATH}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
