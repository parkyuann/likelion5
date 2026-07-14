"""
나열형 문장 후처리 — 값-시점/값-개체 대응, 2인 통합본(2026-07-14).

claim_extractor.py가 뽑은 후보 행(문장 1개=1행, 값 여러 개는 ";"로 병합)을 입력받아,
나열 구조가 명시적으로 확인되는 행만 값 단위로 쪼개고(1값=1행), 나머지는 원본 행을
보존한 채 검토 상태만 표시한다.

[통합 내역 — claim_value_time_matcher.py(팀원A) + claim_listform_resolver.py(팀원B)]
- 팀원A에서 채택: 행 분리 엔진 3종 — ① "X에서 Y로" 전환 반복(_expand_transitions)
  ② "A는 v1, B는 v2" 개체 나열(_expand_entity_single) ③ "7~9월 월별" 시점 범위 분배
  (_expand_time_range). claim_candidates_full.csv 7,543행 전수 검증으로 가드가 확정된
  버전이라 그대로 가져옴.
- 팀원B에서 채택: 분기 라벨 노이즈 필터("작년 3분기(7~9월)"의 "3분기"가 값으로 잡히는
  오탐 제거) + "분기(1~3월)" 괄호 보조 표현 제외 가드 + 사람 검토 라우팅 상태값
  (list_alignment_status — 자동 처리 못 한 행을 COUNT_MISMATCH/LOW_CONFIDENCE로 표시해
  사람 검토 대상을 필터 한 번으로 골라낼 수 있게 함).
- 중복이라 하나만 남긴 것:
  * 연도 노이즈 필터 — 양쪽에 같은 로직(4자리 19xx/20xx + 단위 "년" 제외)이 있었음 → 1개로.
  * 시점 범위 정렬 — 같은 목적의 규칙이 양쪽에 있었음. 팀원A 버전이 요구 조건(명시적
    분배어 + 콤마로 인접한 동일 단위 리스트)이 더 촘촘하고, 팀원B 버전의 가드 2종
    (단위 동일성, 값 간 간격 5자 이내)을 사실상 포함하므로 팀원A 버전 채택.
  * 출력 방식 — 열 추가(팀원B: time_ref_list) vs 행 분리(팀원A) 중 **행 분리** 채택.
    뒷단(실전2 KOSIS 조회, tolerance_judge)은 "값 1개+시점 1개" 단위로 동작하므로
    행 분리가 바로 물릴 수 있는 형태다. time_ref_list 열은 행 분리로 대체되어 제거.

상태값(모든 행에 부여):
  ALIGNED         자동 분리 성공 — alignment_method에 방식(transition/entity/time_range) 기록
  COUNT_MISMATCH  월 범위는 있는데 값 개수 불일치 — 사람 검토
  LOW_CONFIDENCE  값·월 개수는 맞지만 가드(분배어·콤마 인접·동일 단위) 미충족 — 사람 검토
  NOT_LIST_FORM   나열 구조 없음(원본 그대로)
  SINGLE_VALUE    노이즈 정제 후 값 1개 이하(나열형 대상 아님)

[2026-07-14 claim_extractor.py 연결] news_preprocessed.csv -> claim_extractor.extract_from_article
(추출) -> resolve_row(나열형 후처리)를 이 파일 한 번 실행으로 잇는다. claim_extractor.py 함수를
그대로 가져다 쓰며(원본 파일 단독 실행도 계속 가능), 중간 산출물 claim_candidates_relaxed.csv도
같이 저장한다. listform.csv 컬럼 구성 = 추출 컬럼 14개 + list_alignment_status/alignment_method.

[2026-07-14 컬럼 정리] claim_extractor.py에서 passes_old_filter/passes_relaxed_filter/
candidate_origin 3개 컬럼을 제거했다(완화 필터 검증용 임시 컬럼, 검증 완료·결론은
relaxed_filter_analysis.md에 기록됨 — KOSIS 비교 로직에는 쓰이지 않아 정리). 이 파일은 해당
컬럼을 별도로 다루지 않으므로 추가 수정은 없고, 입력 컬럼 수만 17개 -> 14개로 줄어든다.

사용 예 (레포 루트에서):
    python src/claim_listform_resolver.py
"""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claim_extractor import extract_from_article  # noqa: E402

RAW_INPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "news_preprocessed.csv"
RELAXED_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_relaxed.csv"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_listform.csv"

# ---------------------------------------------------------------------------
# 노이즈 필터 (연도: 양쪽 공통 로직 통합 / 분기 라벨: 팀원B)
# ---------------------------------------------------------------------------
_YEAR_TOKEN_RE = re.compile(r"^(19|20)\d{2}$")


def _is_year_noise(value: str, unit: str) -> bool:
    """'2023년'처럼 달력 연도가 값-단위 쌍으로 잘못 잡힌 경우를 걸러낸다(실측: 전체 행의
    18.9%). 4자리 19xx/20xx + 단위 '년' 조합만 노이즈로 간주 — '5년 만에' 같은 정상적인
    기간 표현(자릿수가 다름)은 살려둔다."""
    return unit == "년" and bool(_YEAR_TOKEN_RE.match(value.replace(",", "")))


def _is_quarter_label_noise(value: str, unit: str) -> bool:
    """'작년 3분기(7~9월)'처럼 '3분기'가 실제 지표 값이 아니라 시점 라벨(1~4분기)로
    쓰인 경우를 걸러낸다. 값-시점 정렬이 '4분기'를 하나의 값으로 오인해 뒤따르는 월
    목록과 억지로 대응시키는 오탐이 실측 확인됨 — 1~4 정수 + 단위 '분기'만 노이즈로 간주."""
    return unit == "분기" and value.strip() in {"1", "2", "3", "4"}


def _clean_pairs(values: list[str], units: list[str]) -> tuple[list[str], list[str]]:
    clean_v, clean_u = [], []
    for v, u in zip(values, units):
        v, u = v.strip(), u.strip()
        if _is_year_noise(v, u) or _is_quarter_label_noise(v, u):
            continue
        clean_v.append(v)
        clean_u.append(u)
    return clean_v, clean_u


# ---------------------------------------------------------------------------
# 행 분리 엔진 (팀원A — 7,543행 전수 검증으로 확정된 규칙)
# ---------------------------------------------------------------------------
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


def _expand_transitions(sentence: str, n_values: int, base: dict) -> list[dict] | None:
    """"A는 X에서 Y로, B는 ...으로"처럼 "에서~(으)로" 전환 구간이 반복되면 구간마다 한 행으로
    분리한다. 각 구간이 값을 2개(from/to)씩 소비하므로, 매칭된 구간 수*2가 문장 전체 값
    개수와 정확히 같을 때만 적용한다 — 실측 38건 검증."""
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
    """"A는 v1, B는 v2, C는 v3"처럼 개체별 단일 값이 2개 이상 나열되면 개체마다 한 행으로
    분리한다. 개체 라벨이 중복 포착되면 엉뚱한 명사를 잘못 캡처했을 가능성이 크므로 적용하지
    않는다 — 실측 6건 검증."""
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
    숫자끼리 개수만 우연히 일치한 오탐이었다. "각각/월별/분기별/매월" 분배 신호 + 콤마로
    인접한 동일 단위 리스트까지 요구하면 원래 의도한 케이스만 통과한다(단위 동일성·값 간
    간격 가드 역할을 겸함)."""
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


# ---------------------------------------------------------------------------
# 상태 판정용 보조 (팀원B — 자동 분리에 실패한 행을 사람 검토로 라우팅)
# ---------------------------------------------------------------------------
MONTH_RANGE_RE = re.compile(r"(\d{1,2})\s?~\s?(\d{1,2})\s?월")


def expand_month_range(sentence: str) -> list[str]:
    """'7~9월' -> ['7월', '8월', '9월']. 단, '1분기(1~3월)'처럼 분기를 월로 환산해 괄호로
    풀어쓴 보조 설명은 제외한다 — 나열형으로 오인해 서로 무관한 값들을 억지로 각 달에
    대응시키는 오탐이 실측 확인됨."""
    labels = []
    for match in MONTH_RANGE_RE.finditer(sentence):
        preceding = sentence[max(0, match.start() - 4):match.start()]
        if "분기(" in preceding or preceding.endswith("분기("):
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if start <= end and end - start <= 11:  # 역방향/비정상 범위(연도 오탐 등) 방어
            labels.extend(f"{month}월" for month in range(start, end + 1))
    return labels


# ---------------------------------------------------------------------------
# 행 단위 통합 처리
# ---------------------------------------------------------------------------
def resolve_row(row: dict) -> list[dict]:
    """행 하나를 받아 (분리된 행들 | 원본 행)을 상태값과 함께 돌려준다.
    원본 행을 삭제하거나 값을 지우지 않는다 — 애매하면 항상 원본을 보존한다."""
    sentence = row.get("claim_text") or ""
    value_list = row.get("value_list")
    unit_list = row.get("unit_list")
    if not isinstance(value_list, str) or not isinstance(unit_list, str) or not sentence:
        return [{**row, "list_alignment_status": "SINGLE_VALUE", "alignment_method": None}]

    clean_v, clean_u = _clean_pairs(value_list.split(";"), unit_list.split(";"))
    if len(clean_v) < 2:
        return [{**row, "list_alignment_status": "SINGLE_VALUE", "alignment_method": None}]

    base = dict(row)
    n_values = len(clean_v)

    for method, expanded in (
        ("transition", _expand_transitions(sentence, n_values, base)),
        ("entity", _expand_entity_single(sentence, n_values, base)),
        ("time_range", _expand_time_range(sentence, clean_v, clean_u, base)),
    ):
        if expanded:
            for r in expanded:
                r["list_alignment_status"] = "ALIGNED"
                r["alignment_method"] = method
            return expanded

    # 자동 분리 실패 — 원본 보존 + 사람 검토 라우팅 상태 부여
    month_labels = expand_month_range(sentence)
    if not month_labels:
        status = "NOT_LIST_FORM"
    elif len(month_labels) != n_values:
        status = "COUNT_MISMATCH"
    else:
        status = "LOW_CONFIDENCE"
    return [{**row, "list_alignment_status": status, "alignment_method": None}]


def postprocess_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.extend(resolve_row(row))
    return out


def extract_all_rows(raw_path: Path) -> list[dict]:
    """news_preprocessed.csv 전체 기사에 claim_extractor.extract_from_article을 적용해
    후보 행 리스트를 만든다(claim_extractor.py의 main()과 동일한 순회 로직)."""
    df = pd.read_csv(raw_path)
    all_rows = []
    for idx, row in df.iterrows():
        text = row.get("본문_정제")
        if not isinstance(text, str) or not text:
            continue
        all_rows.extend(
            extract_from_article(idx, row["기사제목"], row["작성일"], row["검색 구분 레이블"], text)
        )
    return all_rows


def main():
    all_rows = extract_all_rows(RAW_INPUT_PATH)
    relaxed = pd.DataFrame(all_rows)
    relaxed.to_csv(RELAXED_OUTPUT_PATH, index=False, encoding="utf-8-sig")
    print(f"[추출] 후보 문장 {len(relaxed)}행 -> {RELAXED_OUTPUT_PATH}")

    result = pd.DataFrame(postprocess_rows(all_rows))
    result.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

    print(f"[나열형 후처리] 입력 {len(relaxed)}행 -> 출력 {len(result)}행 "
          f"(증가분 {len(result) - len(relaxed)}행은 나열형 분리)")
    print(f"상태 분포:\n{result['list_alignment_status'].value_counts()}")
    aligned = result[result["list_alignment_status"] == "ALIGNED"]
    if len(aligned):
        print(f"ALIGNED 방식 분포:\n{aligned['alignment_method'].value_counts()}")
    print(f"저장 위치: {OUTPUT_PATH}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
