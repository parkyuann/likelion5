"""
실전1 ② 뉴스 주장 탐지·추출 — 정제된 뉴스 2,706건 전체에 완화 필터 적용.

claim_extraction_schema.md에서 정한 "핵심 스키마 필드"(2-1절: value/unit/change_type/
time_ref/source_org_raw 등)를 스키마로 삼아, 기사에서 값·단위 쌍이 있는 문장을 찾고
정규식으로 분해한다. claim_class/source_scope/verifiability_prefilter(2-2절, KOSIS 대조 전 필터링용)는
지금까지 사람이 37건을 수동으로 읽고 판단한 항목이라 규칙만으로 안정적으로 자동화하기
어렵다고 보고 이번 1차 전체 추출에서는 대상에서 제외했다 — 구조적 요소(무엇을, 얼마나,
언제, 어디서 인용했는지)만 기계적으로 뽑아내고, 사람이 봐야 하는 판단(claim_class 등)은
그대로 남겨둔다.

한계(설계상 알고 있는 것):
- "나열형 문장"(idx=2655: "17만1000명, 16만6000명, 31만2000명…")은 문장 하나에서
  value/unit 쌍을 전부(findall) 뽑아 리스트로 남긴다 — 몇 번째 값이 몇 번째 시점에
  대응하는지까지는 정규식만으로 못 맞춘다(스키마 문서 4장에 명시된 한계 그대로).
- population(모집단)은 규칙화가 특히 어려워 이번 1차에서는 추출하지 않는다(수동 검증만).
- change_type/source_org_raw는 문장 단위 휴리스틱이라 오탐/누락 있을 수 있음 — 사람 검증 필요.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/claim_extractor.py
"""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from news_preprocessor import split_sentences_fast  # noqa: E402

INPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "news_preprocessed.csv"
# 기존 7,043건 결과(claim_candidates_full.csv)는 비교 기준선으로 보존한다.
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_relaxed.csv"

NUMBER_RE = re.compile(r"\d")
COMPARISON_RE = re.compile(
    r"(증가|감소|상승|하락|늘어|줄어|올랐|내렸|대비|전년|전월|전분기|동월|동기|"
    r"작년|지난해|최고|최다|최대|최소|최저|배|포인트|%p)"
)

VALUE_UNIT_RE = re.compile(
    r"(?P<value>\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?(?:\s?\d[\d,]*(?:\.\d+)?(?:조|억|만|천)?)*)"
    r"\s*(?P<unit>%p|%|원|달러|천\s?명|만\s?명|명|건|가구|톤|ha|kg|g|포인트|세|개월|년|분기|위)"
)

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
    value_unit_pairs = VALUE_UNIT_RE.findall(sentence)
    if not value_unit_pairs:
        return None  # 완화 기준: 숫자+비교어가 아니라 값·단위 쌍이 1개 이상
    values = [v.strip() for v, u in value_unit_pairs]
    units = [u.strip() for v, u in value_unit_pairs]
    passes_old_filter = bool(NUMBER_RE.search(sentence) and COMPARISON_RE.search(sentence))

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
        "passes_old_filter": passes_old_filter,
        "passes_relaxed_filter": True,
        "candidate_origin": "both" if passes_old_filter else "relaxed_only",
    }


def extract_from_article(idx: int, title: str, date: str, label, text: str) -> list[dict]:
    rows = []
    for sent in split_sentences_fast(text):
        extracted = extract_from_sentence(sent)
        if extracted is None:
            continue
        extracted.update({"article_idx": idx, "기사제목": title, "작성일": date, "검색_구분_레이블": label})
        rows.append(extracted)
    return rows


def main():
    df = pd.read_csv(INPUT_PATH)
    all_rows = []
    for idx, row in df.iterrows():
        text = row.get("본문_정제")
        if not isinstance(text, str) or not text:
            continue
        all_rows.extend(
            extract_from_article(idx, row["기사제목"], row["작성일"], row["검색 구분 레이블"], text)
        )

    result = pd.DataFrame(all_rows)
    result.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

    n_articles_with_claims = result["article_idx"].nunique() if len(result) else 0
    print(f"기사 {len(df)}건 중 {n_articles_with_claims}건에서 후보 문장 {len(result)}개 추출")
    print(f"레이블별 후보 문장 수:\n{result.groupby('검색_구분_레이블').size()}")
    print(f"change_type 분포:\n{result['change_type'].value_counts()}")
    print(f"candidate_origin 분포:\n{result['candidate_origin'].value_counts()}")
    print(f"저장 위치: {OUTPUT_PATH}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
