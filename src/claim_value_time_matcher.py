"""
claim_extractor.py(원본, 이 파일에서 절대 수정하지 않음)가 뽑은 후보 행 중
"나열형 문장"(한 문장에 값이 여러 개 나열된 경우)을 값-시점/값-개체 단위로 재매칭하는
후처리 전용 모듈.

claim_extractor.py는 문장 하나 = 행 하나로, 값이 여러 개면 전부 리스트(";"-join)에
욱여넣는다("17만1000명;16만6000명;31만2000명" 처럼). 이 모듈은 그 출력(CSV)을 입력으로
받아 claim_text/value_list/unit_list만 보고, 구조가 명시적으로 확인되는 패턴에 한해서만
값마다 별도 행으로 쪼갠다. claim_extractor.py 코드는 건드리지 않으므로, 그 파일을
그대로 실행해서 만든 CSV라면 뭐든 입력으로 쓸 수 있다.

claim_candidates_full.csv 7,543행 전수 검증 후 확정한 규칙(자세한 근거는 각 함수 docstring):
  1. _split_year_noise    - "2023년"처럼 달력 연도가 값으로 잘못 잡힌 것 제외(전체 행의 18.9%)
  2. _expand_transitions  - "A는 X에서 Y로" 반복(38건 검증, 대부분 정확)
  3. _expand_entity_single- "A는 v1, B는 v2, C는 v3" 반복(6건 검증, 대부분 정확)
  4. _expand_time_range   - "값1,값2,값3 ... 7~9월 월별"(단순 개수 일치만으로는 7건 중 6건이
                            오탐이라, 명시적 분배어+콤마 인접 리스트까지 요구하도록 강화함)

네 패턴 다 안 걸리면 원본 행을 그대로 1행 유지한다(값을 지우거나 행을 삭제하지 않음) —
claim_extractor.py의 출력 스키마와 100% 호환된다.

사용 예:
    from claim_value_time_matcher import postprocess_csv
    postprocess_csv("data/claim_candidates_relaxed.csv", "data/claim_candidates_expanded.csv")

또는 커맨드라인에서:
    python claim_value_time_matcher.py <입력.csv> [출력.csv]
"""
import csv
import re
import sys
from pathlib import Path

INPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_relaxed.csv"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_expanded.csv"

YEAR_VALUE_RE = re.compile(r"^(19|20)\d{2}$")
RANGE_TIME_RE = re.compile(r"(\d{1,2})\s*[~\-]\s*(\d{1,2})\s*(월|분기)")
DISTRIBUTIVE_RE = re.compile(r"월별|분기별|매월|각각")

_REL_TIME_WORDS = r"지난달|이번\s?달|이달|올해|금년|작년|지난해|재작년|전년|전월|전분기|동월|동기"
_TRANS_UNIT = r"%p|%|원|달러|명|건|가구|톤|ha|kg|g|포인트|위"

TRANSITION_RE = re.compile(
    r"(?:(?P<year1>(?:19|20)\d{2})년\s*"
    rf"|(?P<rel1>{_REL_TIME_WORDS})\s*"
    r"|(?P<entity1>[가-힣]{1,6})(?:\s[가-힣]{1,6})?\s*(?:은|는)\s*)?"
    rf"(?P<from_val>\d[\d,\.]*(?:조|억|만|천)?)\s*(?P<from_unit>{_TRANS_UNIT})?\s*"
    r"에서\s*"
    r"(?:(?P<year2>(?:19|20)\d{2})년\s*"
    rf"|(?P<rel2>{_REL_TIME_WORDS})\s*"
    r"|(?P<entity2>[가-힣]{1,6})(?:\s[가-힣]{1,6})?\s*(?:은|는)\s*)?"
    rf"(?P<to_val>\d[\d,\.]*(?:조|억|만|천)?)\s*(?P<to_unit>{_TRANS_UNIT})?\s*"
    r"(?:으로|로)"
)

ENTITY_SINGLE_RE = re.compile(
    rf"([가-힣]{{1,8}}?)\s*(?:은|는)\s*(\d[\d,\.]*(?:조|억|만|천)?)\s*({_TRANS_UNIT})(?=,|$|\s|를)"
)


def _split_year_noise(values: list[str], units: list[str]) -> tuple[list[str], list[str]]:
    """"2023년"처럼 달력 연도가 값-단위 쌍으로 잘못 잡힌 것을 걸러낸다.

    claim_extractor.py의 VALUE_UNIT_RE 단위 목록에 "년"이 있어서 "5년"(기간) 같은 진짜 값과
    "2023년"(연도 표기)이 구분 안 된 채로 값 리스트에 섞여 들어온다 — 실측 결과 전체 행의
    18.9%가 이 노이즈였고, 나열 매칭에서 "값 개수"를 셀 때 이걸 먼저 안 빼면 개수 비교
    자체가 틀어진다. 4자리 19xx/20xx + "년"만 제외하고, "5년"처럼 자릿수가 다른 진짜
    기간 값은 그대로 둔다.
    """
    clean_values, clean_units = [], []
    for v, u in zip(values, units):
        if u == "년" and YEAR_VALUE_RE.match(v.replace(",", "")):
            continue
        clean_values.append(v)
        clean_units.append(u)
    return clean_values, clean_units


def _expand_transitions(sentence: str, n_values: int, base: dict) -> list[dict] | None:
    """"A는 X에서 Y로, B는 ...으로"처럼 "에서~(으)로" 전환 구간이 반복되면 구간마다 한 행으로 분리한다.

    각 구간이 값을 2개(from/to)씩 소비하므로, 매칭된 구간 수*2가 문장 전체 값 개수와 정확히
    같을 때만 적용한다 — 실측 38건 검증, 라벨이 다소 뭉뚱그려진 사례 외엔 전부 올바르게 분리됨.
    """
    matches = [m for m in TRANSITION_RE.finditer(sentence) if m.group("from_val") and m.group("to_val")]
    if not matches or len(matches) * 2 != n_values:
        return None
    rows = []
    for m in matches:
        g = m.groupdict()
        row = dict(base)
        row.update({
            "value_list": f"{g['from_val']};{g['to_val']}",
            "unit_list": f"{g['from_unit'] or ''};{g['to_unit'] or ''}",
            "value_count": 2,
        })
        if g["year1"] or g["rel1"]:
            row["time_ref"] = f"{g['year1']}년" if g["year1"] else g["rel1"]
        if g["year2"] or g["rel2"]:
            row["time_compare"] = f"{g['year2']}년" if g["year2"] else g["rel2"]
        rows.append(row)
    return rows


def _expand_entity_single(sentence: str, n_values: int, base: dict) -> list[dict] | None:
    """"A는 v1, B는 v2, C는 v3"처럼 개체별 단일 값이 2개 이상 나열되면 개체마다 한 행으로 분리한다.

    개체 라벨이 중복되면(예: "중국의 수출가격지수는..." 처럼 정작 다른 건 국가명인데 직전 명사
    "지수"만 반복 포착되는 경우) 엉뚱한 명사를 잘못 캡처했을 가능성이 크므로 적용하지 않는다.
    실측 6건 검증, 라벨이 다소 뭉뚱그려진 1건 외엔 전부 올바르게 분리됨.
    """
    matches = ENTITY_SINGLE_RE.findall(sentence)
    entities = [e for e, v, u in matches]
    if len(matches) < 2 or len(matches) != n_values or len(set(entities)) != len(entities):
        return None
    rows = []
    for entity, value, unit in matches:
        row = dict(base)
        row.update({"value_list": value, "unit_list": unit, "value_count": 1})
        rows.append(row)
    return rows


def _expand_time_range(sentence: str, values: list[str], units: list[str], base: dict) -> list[dict] | None:
    """"올해 7~9월 월별"처럼 시점 범위 + 명시적 분배 표현이 함께 있고, 콤마로 나열된 같은 단위
    값이 범위 길이만큼 실제로 인접해 있을 때만 순서대로 매칭한다.

    조건을 세게 건 이유: "범위 길이 == 값 개수"만으로 판단하면 실측 7건 중 6건이 서로 무관한
    숫자끼리 개수만 우연히 일치한 오탐이었다(예: "3개월(6~8월) 기준 ... 320만5000원 ...
    7만7000원 증가" — 월별 수치가 전혀 아님). "각각/월별/분기별/매월" 같은 명시적 분배 신호와
    콤마로 인접한 동일 단위 리스트까지 요구하면 오탐 없이 원래 의도한 케이스만 통과한다.
    """
    m = RANGE_TIME_RE.search(sentence)
    if not m or not DISTRIBUTIVE_RE.search(sentence):
        return None
    if len(set(units)) != 1:
        return None
    start, end, period_unit = int(m.group(1)), int(m.group(2)), m.group(3)
    periods = [f"{p}{period_unit}" for p in range(start, end + 1)]
    if len(periods) != len(values):
        return None
    num = r"\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?(?:\s?\d[\d,]*(?:조|억|만|천)?)*"
    comma_list = r"\s*,\s*".join([num + re.escape(units[0])] * len(values))
    if not re.search(comma_list, sentence):
        return None
    rows = []
    for period, value, unit in zip(periods, values, units):
        row = dict(base)
        row.update({
            "value_list": value, "unit_list": unit, "value_count": 1,
            "time_ref": period, "time_compare": base.get("time_ref"),
        })
        rows.append(row)
    return rows


def expand_row(row: dict) -> list[dict]:
    """claim_extractor.py가 출력한 행 하나를 받아, 나열형 구조가 명시적으로 확인되면
    여러 행으로 쪼개고, 아니면 원본 그대로 1행 리스트로 돌려준다.

    원본 행을 삭제하거나 값을 지우지 않는다 — 애매하면 항상 원본을 그대로 보존한다.
    """
    sentence = row.get("claim_text") or ""
    value_list = row.get("value_list") or ""
    unit_list = row.get("unit_list") or ""
    if not sentence or not value_list:
        return [row]

    values = value_list.split(";")
    units = unit_list.split(";")
    clean_values, clean_units = _split_year_noise(values, units)
    if not clean_values:
        return [row]  # 값이 전부 연도 노이즈였어도 원본 삭제는 이 모듈의 책임이 아님

    base = dict(row)

    n_values = len(clean_values)
    expanded = (
        _expand_transitions(sentence, n_values, base)
        or _expand_entity_single(sentence, n_values, base)
        or _expand_time_range(sentence, clean_values, clean_units, base)
    )
    return expanded if expanded else [row]


def postprocess_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.extend(expand_row(row))
    return out


def postprocess_csv(input_path, output_path=None) -> list[dict]:
    input_path = Path(input_path)
    with open(input_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    expanded = postprocess_rows(rows)

    if output_path:
        output_path = Path(output_path)
        fieldnames, seen = [], set()
        for r in expanded:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(expanded)

    return expanded


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    in_path = Path(sys.argv[1]) if len(sys.argv) > 1 else INPUT_PATH
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT_PATH

    with open(in_path, encoding="utf-8-sig") as f:
        before = sum(1 for _ in csv.DictReader(f))
    result = postprocess_csv(in_path, out_path)
    print(f"입력 {before}행 -> 출력 {len(result)}행 (증가분 {len(result) - before}행은 나열형 문장이 여러 행으로 쪼개진 것)")
    print(f"저장 위치: {out_path}")
