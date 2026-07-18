"""
실전1 ② 뉴스 주장 탐지·추출 + 나열형 후처리 — 3인 통합, 단일 파일본(2026-07-14).

news_preprocessed.csv(정제된 뉴스 2,706건)에서 값·단위 쌍이 있는 문장을 뽑고(추출),
나열형 문장(idx=2655류)은 값 단위로 행을 분리(나열형 후처리)까지 한 번에 실행한다.
원래 claim_extractor.py(추출)와 claim_listform_resolver.py(나열형 후처리) 2개 파일로
나뉘어 있었으나, git 업로드 편의를 위해 이 파일 하나로 합쳤다 — 후처리 파일이 추출 파일의
함수를 import해서 쓰는 구조였는데, 두 파일을 하나로 합치면서 그 import를 없애고 함수를
그대로 이 파일 안에 담았다(자기 자신을 import하는 모순을 피하기 위함).

[결과 파일 1개로 정리] 추출 중간 산출물(claim_candidates_relaxed.csv)은 더 이상 디스크에
저장하지 않는다 — 추출 결과는 메모리(all_rows)에만 두고 곧바로 나열형 후처리로 넘겨,
input: news_preprocessed.csv -> output: claim_listform.csv 파일 하나만 남긴다
(기존 파일명 claim_candidates_listform.csv에서 claim_listform.csv로 변경).

claim_extraction_schema.md에서 정한 "핵심 스키마 필드"(2-1절: value/unit/change_type/
time_ref/source_org_raw 등)를 스키마로 삼아, 기사에서 값·단위 쌍이 있는 문장을 찾고
정규식으로 분해한다. claim_class/source_scope/verifiability_prefilter(2-2절, KOSIS 대조 전 필터링용)는
지금까지 사람이 37건을 수동으로 읽고 판단한 항목이라 규칙만으로 안정적으로 자동화하기
어렵다고 보고 이번 1차 전체 추출에서는 대상에서 제외했다 — 구조적 요소(무엇을, 얼마나,
언제, 어디서 인용했는지)만 기계적으로 뽑아내고, 사람이 봐야 하는 판단(claim_class 등)은
그대로 남겨둔다.

[통합 내역]
- 팀원A에서 채택: 행 분리 엔진 3종 — ① "X에서 Y로" 전환 반복(_expand_transitions)
  ② "A는 v1, B는 v2" 개체 나열(_expand_entity_single) ③ "7~9월 월별" 시점 범위 분배
  (_expand_time_range). claim_candidates_full.csv 7,543행 전수 검증으로 가드가 확정된
  버전이라 그대로 가져옴.
- 팀원B에서 채택: 분기 라벨 노이즈 필터("작년 3분기(7~9월)"의 "3분기"가 값으로 잡히는
  오탐 제거) + "분기(1~3월)" 괄호 보조 표현 제외 가드 + 사람 검토 라우팅 상태값
  (list_alignment_status — 자동 처리 못 한 행을 COUNT_MISMATCH/LOW_CONFIDENCE로 표시해
  사람 검토 대상을 필터 한 번으로 골라낼 수 있게 함).
- 출력 방식은 행 분리(팀원A 방식) 채택 — 뒷단(실전2 KOSIS 조회, tolerance_judge)이
  "값 1개+시점 1개" 단위로 동작하므로 행 분리가 바로 물릴 수 있는 형태다.

한계(설계상 알고 있는 것):
- population(모집단)은 규칙화가 특히 어려워 이번 1차에서는 추출하지 않는다(수동 검증만).
- change_type/source_org_raw는 문장 단위 휴리스틱이라 오탐/누락 있을 수 있음 — 사람 검증 필요.

상태값(모든 행에 부여):
  ALIGNED         자동 분리 성공 — alignment_method에 방식(transition/entity/time_range) 기록
  COUNT_MISMATCH  월 범위는 있는데 값 개수 불일치 — 사람 검토
  LOW_CONFIDENCE  값·월 개수는 맞지만 가드(분배어·콤마 인접·동일 단위) 미충족 — 사람 검토
  NOT_LIST_FORM   나열 구조 없음(원본 그대로)
  SINGLE_VALUE    노이즈 정제 후 값 1개 이하(나열형 대상 아님)

사용 예 (레포 루트에서):
    python src/claim_extractor.py
"""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from news_preprocessor import split_sentences_fast  # noqa: E402
from claim_normalizer import (  # noqa: E402
    normalize_time_ref,
    parse_korean_number,
    time_span_type,
)

RAW_INPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "news_preprocessed.csv"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_listform.csv"

# ---------------------------------------------------------------------------
# ① 추출 (문장에서 값·단위 쌍 찾기)
# ---------------------------------------------------------------------------
COMPARISON_RE = re.compile(
    r"(증가|감소|상승|하락|늘어|줄어|올랐|내렸|대비|전년|전월|전분기|동월|동기|"
    r"작년|지난해|최고|최다|최대|최소|최저|배|포인트|%p)"
)

# 단위 목록: news_preprocessed.csv 전체에서 "숫자 뒤 1~2글자 한글" 빈도를 직접 스캔해
# 기존 목록에 없는 흔한 단위(대/개/회/곳/시간/분 등)를 추가했다. "대"는 "1012대"(기기 세는
# 단위) 같은 진짜 값도 잡지만 "30대"(연령대) 같은 문장에서도 걸릴 수 있음 — 이런 다의성은
# 이 정규식 단계에서 해소하지 않고, 뒷단(claim_class 필터링)에서 걸러지도록 남겨둔다.
# 다의어 교정(실측 스캔 근거):
#  - 멀티글자 단위(위안·배럴)를 단일글자(위·배)보다 alternation 앞에 둬서 잘림 방지
#    ('92억위안'이 값 92억+단위 '위'로, '6배럴'이 '배'로 잘리던 것 교정).
#  - '세대'(가구/세대)는 수치 주장 단위가 아니라 '세(?!대)'로 연령 '세'만 남기고 제외.
#  - '분기'는 시점 개념이라 값-단위에서 제거(시점은 TIME_RE가 담당) — '1분기'가 값 '1'로
#    잡히던 오탐 제거. '분(?!의)'로 분수('3분의 1'), '초(?!반)'로 범위 표현('90 초반') 제외.
# '%포인트'(증감폭)는 '%'(증감률)보다 앞에 둬서 '0.4%포인트'가 '0.4%'+버려진 '포인트'로
# 잘리지 않게 한다(실측 603건). '분위'(소득 5분위 등 구간 라벨)는 분(minute) 오탐이라 제외.
_UNIT_ALT = (
    r"%\s?포인트|%p|%|원|위안|달러|배럴|천\s?명|만\s?명|명|건|가구|톤|ha|kg|g|포인트"
    r"|세(?!대)|개월|년|위|대|개|채|척|마리|그루|병|잔|회|차례|편|곳|층|배"
    r"|시간|분(?!의|기|위)|초(?!반)|도|점"
)

# 표현 정규화: 잡힌 단위 표기를 기준 단위로 통일한다('%포인트'/'% 포인트' -> '%p').
_UNIT_CANON = {"%포인트": "%p", "%p": "%p"}


def canon_unit(u: str) -> str:
    return _UNIT_CANON.get(u.replace(" ", ""), u)

# 값 앞의 음수 부호(-/−)를 값에 포함한다 — '자동차부품(-7.6%)'처럼 감소를 나타내는 부호가
# 유실되면 증감 방향이 사라지므로(값이 전부 양수화됨) 부호까지 캡처한다.
VALUE_UNIT_RE = re.compile(
    rf"(?P<value>[-−]?\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?(?:\s?\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?)*)"
    rf"\s*(?P<unit>{_UNIT_ALT})"
)

# "1950~1960년대", "3~4%대"처럼 범위 표현의 앞쪽 숫자엔 단위가 안 붙고 뒤쪽에만 붙는
# 한국어 생략 구조 전용 규칙 — 이게 없으면 VALUE_UNIT_RE는 뒤쪽 값만 잡고 앞쪽은 놓친다
# (단위가 바로 뒤에 없는 위치에서는 애초에 매치가 성립하지 않기 때문).
RANGE_RE = re.compile(
    rf"(?P<start>\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?)\s*[~\-]\s*"
    rf"(?P<end>\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?)\s*(?P<unit>{_UNIT_ALT})"
)


def extract_value_unit_pairs(sentence: str) -> list[tuple[str, str]]:
    """VALUE_UNIT_RE 단독으로는 "A~B단위" 범위의 앞쪽 값(A)을 놓치므로, RANGE_RE로 먼저
    범위 표현을 찾아 양쪽 값을 다 뽑고, 그 구간과 겹치는 VALUE_UNIT_RE 매치(뒤쪽 값 B가
    중복으로 잡힘)는 건너뛴다."""
    pairs: list[tuple[str, str]] = []
    consumed_spans: list[tuple[int, int]] = []
    for m in RANGE_RE.finditer(sentence):
        u = canon_unit(m.group("unit"))
        pairs.append((m.group("start"), u))
        pairs.append((m.group("end"), u))
        consumed_spans.append(m.span())

    for m in VALUE_UNIT_RE.finditer(sentence):
        if any(start <= m.start() and m.end() <= end for start, end in consumed_spans):
            continue
        v, u = m.group("value"), canon_unit(m.group("unit"))
        # 달력 연도('2023년')는 시점이지 값이 아님 — 값-단위 추출 단계에서 바로 제외해
        # value_count/change_type가 처음부터 노이즈 없이 계산되게 한다.
        if _is_year_noise(v, u):
            continue
        pairs.append((v, u))
    return pairs


TIME_RE = re.compile(
    r"(지난달|이번\s?달|이달|올해|금년|작년|지난해|전년\s?동월|전년|전월|전분기|동월|동기|"
    r"지난\s?\d+월|\d{4}년(?:\s?\d+월)?|\d+분기)"
)

SOURCE_ORG_RE = re.compile(
    r"([가-힣A-Za-z0-9·()]{2,20})(?:에\s?따르면|가\s?\d*일?\s?발표한|은\s?\d*일?\s?발표에서)"
)

INDEX_RE = re.compile(r"지수|=\s?100|=100")

RANK_KEYWORDS = ("최고", "최다", "최대", "최소", "최저", "1위", "2위", "3위", "꼴찌")
COMPARE_KEYWORDS = ("보다", "대비", "반면")
PCT_POINT_KEYWORDS = ("%p", "포인트")


def classify_change_type(sentence: str, units: list[str]) -> str:
    """문장 단위 휴리스틱 — 우선순위: 순위 > 증감폭(%p) > 비교 > 증감률(%) > 단순수치."""
    if any(k in sentence for k in RANK_KEYWORDS):
        return "순위"
    if "%p" in units or any(k in sentence for k in PCT_POINT_KEYWORDS):
        return "증감폭"
    if any(k in sentence for k in COMPARE_KEYWORDS):
        return "비교"
    if "%" in units or COMPARISON_RE.search(sentence):
        return "증감률"
    return "단순수치"


def extract_from_sentence(sentence: str) -> dict | None:
    value_unit_pairs = extract_value_unit_pairs(sentence)
    if not value_unit_pairs:
        return None  # 완화 기준: 숫자+비교어가 아니라 값·단위 쌍이 1개 이상
    values = [v.strip() for v, u in value_unit_pairs]
    units = [u.strip() for v, u in value_unit_pairs]

    time_matches = TIME_RE.findall(sentence)
    time_ref = time_matches[0] if time_matches else None
    time_compare = next((t for t in time_matches[1:] if t != time_ref), None)

    source_match = SOURCE_ORG_RE.search(sentence)
    source_org_raw = source_match.group(1) if source_match else None

    return {
        "claim_text": sentence,
        "value_list": ";".join(values),
        "unit_list": ";".join(units),
        "value_count": len(values),
        "change_type": classify_change_type(sentence, units),
        "time_ref": time_ref,
        "time_compare": time_compare,
        "is_index": bool(INDEX_RE.search(sentence)),
        "source_mentioned": source_org_raw is not None,
        "source_org_raw": source_org_raw,
    }


def _fmt_num(x) -> str:
    """정규화 수치를 문자열로. 정수면 소수점 없이(9200000000), 아니면 그대로(100.4)."""
    if x is None:
        return ""
    return str(int(x)) if float(x).is_integer() else str(x)


def enrich_normalization(row: dict, published_at) -> dict:
    """추출된 행에 정규화 필드를 추가한다(원본 필드는 보존).

    - value_norm_list: value_list의 각 값을 조/억/만/천 전개한 실수(기준단위 기준)
    - time_ref_abs / time_compare_abs: 발행일 기준 절대 시점
    - time_span_type: 월/분기/연/부분기간/특정일/불명확
    """
    values = [v for v in str(row.get("value_list") or "").split(";") if v]
    norm = []
    for v in values:
        n = parse_korean_number(v)
        norm.append(_fmt_num(n) if n is not None else v)
    row["value_norm_list"] = ";".join(norm)
    row["time_ref_abs"] = normalize_time_ref(row.get("time_ref"), published_at)
    row["time_compare_abs"] = normalize_time_ref(row.get("time_compare"), published_at)
    row["time_span_type"] = time_span_type(row.get("time_ref"))
    return row


def extract_from_article(idx: int, title: str, date: str, label, text: str) -> list[dict]:
    rows = []
    for sent in split_sentences_fast(text):
        extracted = extract_from_sentence(sent)
        if extracted is None:
            continue
        extracted.update({"article_idx": idx, "기사제목": title, "작성일": date, "검색_구분_레이블": label})
        enrich_normalization(extracted, date)
        rows.append(extracted)
    return rows


def extract_all_rows(raw_path: Path) -> list[dict]:
    """news_preprocessed.csv 전체 기사에 extract_from_article을 적용해 후보 행 리스트를 만든다."""
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


# ---------------------------------------------------------------------------
# ② 나열형 후처리 (값-시점/값-개체 대응)
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
    # 나열형 분리로 value_list/time_ref가 바뀐 행은 정규화를 다시 계산한다.
    for r in out:
        enrich_normalization(r, r.get("작성일"))
    return out


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def main():
    all_rows = extract_all_rows(RAW_INPUT_PATH)
    print(f"[추출] 후보 문장 {len(all_rows)}행")

    result = pd.DataFrame(postprocess_rows(all_rows))
    result.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

    print(f"[나열형 후처리] 입력 {len(all_rows)}행 -> 출력 {len(result)}행 "
          f"(증가분 {len(result) - len(all_rows)}행은 나열형 분리)")
    print(f"상태 분포:\n{result['list_alignment_status'].value_counts()}")
    aligned = result[result["list_alignment_status"] == "ALIGNED"]
    if len(aligned):
        print(f"ALIGNED 방식 분포:\n{aligned['alignment_method'].value_counts()}")
    print(f"저장 위치: {OUTPUT_PATH}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
