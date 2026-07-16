"""Create a Codex silver-prelabel copy of retrieval_eval_claims_v0.csv.

The source file is kept untouched.  This pass labels claim class, source
scope, and the KOSIS verifiability prefilter with conservative rules.  Table
identity fields are deliberately left blank unless an exact local
organization match is available; unresolved table mapping is marked
NEEDS_REVIEW rather than guessed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "data" / "retrieval_eval_claims_v0.csv"
OUTPUT = ROOT / "data" / "retrieval_eval_claims_v0_codex.csv"
ORG_PATH = ROOT / "data" / "kosis_org_names.json"


NOISE_RE = re.compile(
    r"current\s*time|text\s*color|selected\s+subtitles|opens\s+subtitles|"
    r"beginning\s+of\s+dialog|loaded\s*:\s*\d|duration\s*[:0-9]",
    re.I,
)
PLAN_RE = re.compile(
    r"계획|추진|도입하기로|늘리기로|신설|폐지|시행|개정|발급|공개했다|"
    r"목표|예정|방안|합의|협약|채용할|연다고|설치하기로|부과하기로"
)
FORECAST_RE = re.compile(
    r"전망|예상|가능성|것으로 보|우려|예측|경고|전망이|분석이 나|"
    r"낮췄|높일 수|될 수|돌파할 가능"
)
INTERPRET_RE = re.compile(
    r"평가다|평가가|해석된다|의미로|진단했다|진단|때문이다|영향이다|"
    r"덕분에|결과였다|원인|배경|보여준다|나타낸다"
)
SURVEY_RE = re.compile(r"조사 결과|설문|여론조사|조사에서|조사에 따르면|실태조사")
INDIVIDUAL_RE = re.compile(
    r"지냈다|역임|당선|합류했다|맡았다|근무한 경험|계약에 이어|잃었다|잃고|"
    r"구조됐다|숨지고|숨졌다|다쳤다|사망하면|결혼과 출산|출산을|반려견|"
    r"면담을|글을 올렸다|옥신각신|합의했다|연출했다|주장했다|교제했다고|"
    r"서약하며|입사한|선발한|충원하기도|새로운 신규 라인에 배치"
)
LEGAL_RE = re.compile(r"법률|법령|고시|세금|과징금|공제|제도|조항|규정|시행령|시행규칙|법안")
AGGREGATE_RE = re.compile(
    r"\d[\d,.]*\s*(?:%|퍼센트|명|개|건|대|원|달러|억|조|만|톤|kg|㎏|년|개월|"
    r"분기|포인트|bp|GWh|CGT)|증가|감소|상승|하락|판매량|생산량|수출|수입|"
    r"매출|물가|지수|성장률|비율|비중|수익률|출생아|취업자|사망자|인구|"
    r"거래량|거래대금|소비|가격|금리|시가총액|외환보유액"
)

KOSIS_ORG_RE = re.compile(r"통계청|국가데이터처|한국은행|농림축산식품부|보건복지부|고용노동부|관세청|한국거래소|전력거래소|한국석유공사|금융투자협회|공정거래위원회|공정위")
DOMESTIC_PUBLIC_RE = re.compile(
    r"공정위|금융감독원|전력거래소|한국거래소|금융투자협회|해양수산부|정부|"
    r"기획재정부|국토교통부|산업통상자원부|중소벤처기업부|한국석유공사|지자체"
)
FOREIGN_RE = re.compile(
    r"미국|영국|중국|일본|캐나다|독일|국제에너지기구|IEA|미시간대|미 노동부|"
    r"연준|FOMC|블룸버그|JP모건|캐피털 이코노믹스"
)

GENERIC_SOURCE_NAMES = {"보고서", "결과", "기관", "자료", "통계", "조사", "외신", "업계", "금융권"}

# 기사에서 자주 쓰이는 약칭을 현재 KOSIS 조직명으로 연결한다.
ORG_ALIASES = {
    "통계청": ("101", "국가데이터처"),
    "공정위": ("152", "공정거래위원회"),
    "한은": ("301", "한국은행"),
    "금융투자협회": ("325", "한국금융투자협회"),
    "전력거래소": ("388", "한국전력거래소"),
    "오피넷": ("318", "한국석유공사"),
}

PERIOD_PATTERNS = (
    ("quarter", re.compile(r"분기")),
    ("half_year", re.compile(r"상반기|하반기|반기")),
    ("month", re.compile(r"월|개월")),
    ("week", re.compile(r"주간|셋째주|넷째주|첫째주|둘째주|주\\b")),
    ("year", re.compile(r"\\d{4}\\s*년|지난해|작년|올해|연간|연말|연초")),
    ("day", re.compile(r"\\d{1,2}\\s*일|오늘|어제|그제")),
)


def clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def classify_claim(text: str) -> str:
    if not text or NOISE_RE.search(text):
        return "노이즈"
    # 정책·전망 표현이 포함된 수치는 실적 통계로 분류하지 않는다.
    if PLAN_RE.search(text):
        return "목표계획"
    if FORECAST_RE.search(text):
        return "전망예측"
    if LEGAL_RE.search(text):
        return "법령제도"
    if INTERPRET_RE.search(text):
        return "해석수사"
    if INDIVIDUAL_RE.search(text):
        return "개별사례"
    if SURVEY_RE.search(text) and AGGREGATE_RE.search(text):
        return "집계통계"
    if SURVEY_RE.search(text) and not AGGREGATE_RE.search(text):
        return "통계조사안내"
    if AGGREGATE_RE.search(text):
        return "집계통계"
    return "개별사례"


def classify_scope(org_raw: str, text: str, org_names: dict[str, str]) -> str:
    """Schema values: KOSIS등재 / 공식기관_비KOSIS / 민간기관 / 해외기관 / 불명."""
    org_probe = org_raw if org_raw not in GENERIC_SOURCE_NAMES else ""
    catalog_names = sorted(
        (clean(name) for name in org_names.values() if clean(name)), key=len, reverse=True
    )
    if org_probe and (exact_org_match(org_probe, org_names)[0] or any(alias in org_probe for alias in ORG_ALIASES)):
        return "KOSIS등재"
    if any(name in org_probe for name in catalog_names):
        return "KOSIS등재"
    if KOSIS_ORG_RE.search(org_probe):
        return "KOSIS등재"
    if FOREIGN_RE.search(org_probe):
        return "해외기관"
    if DOMESTIC_PUBLIC_RE.search(org_probe):
        return "공식기관_비KOSIS"
    if org_probe:
        return "민간기관"
    if KOSIS_ORG_RE.search(text):
        return "KOSIS등재"
    if DOMESTIC_PUBLIC_RE.search(text):
        return "공식기관_비KOSIS"
    if FOREIGN_RE.search(text):
        return "해외기관"
    return "불명"


def exact_org_match(org_raw: str, org_names: dict[str, str]) -> tuple[str, str]:
    if not org_raw:
        return "", ""
    normalized = re.sub(r"\s+", "", org_raw)
    for org_id, org_name in org_names.items():
        name = clean(org_name)
        if name and (normalized == re.sub(r"\s+", "", name) or normalized in re.sub(r"\s+", "", name)):
            return str(org_id), name
    return "", ""


def find_org(org_raw: str, text: str, org_names: dict[str, str]) -> tuple[str, str]:
    """Return a catalog organization, including common article aliases."""
    probe = org_raw if org_raw not in GENERIC_SOURCE_NAMES else ""
    matched = exact_org_match(probe, org_names)
    if matched[0]:
        return matched
    for alias, canonical in sorted(ORG_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in probe or alias in text:
            return canonical
    catalog_pairs = sorted(
        ((clean(name), str(org_id)) for org_id, name in org_names.items() if clean(name)),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for name, org_id in catalog_pairs:
        if name in text:
            return org_id, name
    if probe:
        return "", probe
    return "", ""


def infer_period(time_ref: str) -> tuple[str, str]:
    """Keep the extracted period text; do not invent an absolute period."""
    value = clean(time_ref)
    if not value:
        return "", ""
    for period_type, pattern in PERIOD_PATTERNS:
        if pattern.search(value):
            return period_type, value
    return "unknown", value


def main() -> None:
    df = pd.read_csv(INPUT, keep_default_na=False)
    org_names = json.loads(ORG_PATH.read_text(encoding="utf-8"))

    labels: list[dict[str, str]] = []
    for _, row in df.iterrows():
        text = clean(row.get("claim_text"))
        org_raw = clean(row.get("source_org_raw"))
        claim_class = classify_claim(text)
        scope = classify_scope(org_raw, text, org_names)
        is_noise = claim_class == "노이즈"
        verifiable = claim_class == "집계통계" and scope == "KOSIS등재"
        org_id, org_name = find_org(org_raw, text, org_names)
        period_type, period = infer_period(row.get("time_ref"))
        if is_noise or not verifiable:
            match_status = "NO_KOSIS_MATCH"
        else:
            match_status = "NEEDS_REVIEW"
        note = (
            "Codex 실버 라벨; KOSIS 조회 대상이 아니거나 범위 밖인 주장"
            if match_status == "NO_KOSIS_MATCH"
            else "Codex 실버 라벨; KOSIS 표·분류코드·항목은 API 메타 검토 필요"
        )
        labels.append(
            {
                "gold_claim_class": claim_class,
                "gold_source_scope": scope,
                "gold_verifiability_prefilter": "검증시도" if verifiable else "판단불가(범위밖)",
                "gold_org_id": org_id,
                "gold_org_name": org_name,
                "gold_source_role": "data_producer" if (org_raw or org_id) else "",
                "gold_tbl_id": "",
                "gold_tbl_name": "",
                "gold_stat_id": "",
                "gold_dimension_json": "",
                "gold_item_id": "",
                "gold_period_type": period_type,
                "gold_period": period,
                "gold_unit": clean(row.get("unit_list")),
                "gold_match_status": match_status,
                "gold_notes": note,
                "review_status": "codex_prelabel",
                "reviewer": "codex",
            }
        )

    label_df = pd.DataFrame(labels, index=df.index)
    for column in label_df.columns:
        df[column] = label_df[column]
    df.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print(f"wrote {OUTPUT} ({len(df):,} rows)")
    print(df["gold_claim_class"].value_counts().to_dict())
    print(df["gold_match_status"].value_counts().to_dict())


if __name__ == "__main__":
    main()
