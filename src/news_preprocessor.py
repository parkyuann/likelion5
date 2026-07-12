"""
뉴스 본문 1차 전처리.

크롤링 결과(기사 본문 전체)에는 아래 두 가지가 섞여 있다:
  - 헤더 잔재: 페이지 nav 메뉴, 제목 중복, "OOO 기자 입력 2025.01.01. 12:34 업데이트 ... 5" 바이라인
  - 푸터 잔재: 저작권 고지, "많이 본 뉴스"/"당신이 좋아할 만한 콘텐츠" 등 추천 위젯, 댓글 UI 문구

이 정제는 KOSIS 통계표 구조와 무관하게 먼저 진행해도 되는 범용 텍스트 정제 단계다.
"어떤 필드를 추출할지"(지표·시점·단위·모집단 스키마)는 통계표 관찰 후 별도로 정한다.

알려진 한계 (완전 자동화하지 않고 EDA 단계에서 육안 확인을 권장하는 부분):
  - "관련기사 더보기" 티저(다른 기사 제목+스니펫)나 기자 소개 문구가 본문 뒤에 소량 남을 수 있다.
    "더보기"는 전체의 36%에서 등장하는데 진짜 정리 마커인지 본문 일부인지 구분이 어려워
    자동 절삭 마커로 넣지 않았다 — 잘못 넣으면 정상 문장을 잘라먹을 위험이 더 크다고 판단.
  - 푸터_제거됨=False인 약 20%는 광고/추천 위젯 마커를 못 찾은 행으로, 위젯잔재_의심 플래그로
    잡히지 않는 이상 뒷부분에 잡음이 남아있을 수 있다.
  - 수치 기반 주장 추출 시엔 정제된 본문이라도 표본을 뽑아 육안 확인 후 진행할 것을 권장.

실행: (레포 루트에서, venv) venv/Scripts/python.exe src/news_preprocessor.py
입력: data/AI_기반_뉴스_사실검증_시스템_프로젝트_데이터.csv
출력: data/news_preprocessed.csv
"""
import re
import sys

import pandas as pd

RAW_PATH = "data/AI_기반_뉴스_사실검증_시스템_프로젝트_데이터.csv"
OUT_PATH = "data/news_preprocessed.csv"

# "OOO 기자 입력 2025.01.01. 12:34 업데이트 2025.01.02. 09:00 5" 형태.
# 댓글수(마지막 1~4자리 숫자)는 뒤에 공백이 와야만 소비한다 — "5년간"처럼
# 숫자 바로 뒤에 글자가 붙는 실제 문장 시작부를 잘라먹지 않기 위해서다.
HEAD_PATTERN = re.compile(
    r"입력\s*\d{4}\.\d{2}\.\d{2}\.?\s*\d{1,2}:\d{2}"
    r"(?:\s*업데이트\s*\d{4}\.\d{2}\.\d{2}\.?\s*\d{1,2}:\d{2})?"
    r"(?:\s*\d{1,4}(?=\s))?"
)

# 이 마커가 나오면 그 뒤는 저작권 고지 아니면 광고/추천기사 위젯이다.
# 본문 위치와 무관하게(=헤더가 안 잘려도) 항상 "끝을 알리는" 표현이라 믿을 수 있다.
TAIL_PATTERN_STRICT = re.compile(
    r"무단\s*전재|저작권자\s*ⓒ|Copyright\s*조선일보|English\s*기사보기|기사\s*전체보기"
)

# "100자평"/"구독수"는 조선닷컴 댓글 UI 라벨로, 보통 본문 바로 뒤(idx=1 등)에 나오지만
# 일부 계열사 템플릿(땅집고 등, idx=2667)에서는 맨 앞 공유버튼 카운터로도 등장한다.
# 그래서 헤더 앵커(입력 날짜 바이라인)를 이미 찾아 실제 본문 구간에 들어선 게 확인된 경우에만
# 신뢰한다 — 안 그러면 헤더도 못 찾은 행에서 본문 전체가 통째로 잘려나간다 (idx=2667로 확인).
TAIL_PATTERN_LOOSE = re.compile(
    r"100자평|구독수\s*\d+|도움말\s*삭제기준|" + TAIL_PATTERN_STRICT.pattern
)

# 둘 다 못 걸렀을 때 위젯 잔재가 남아있는지 의심하는 용도 (자동 절삭은 하지 않음)
WIDGET_SIGNATURES = ["돌아가기", "오늘의 핫뉴스", "많이 본 뉴스", "당신이 좋아할 만한 콘텐츠", "By Taboola", "100자평"]

WHITESPACE_RE = re.compile(r"\s+")
HTML_TAG_RE = re.compile(r"<div|<script")


def strip_head(text: str) -> tuple[str, bool]:
    m = HEAD_PATTERN.search(text)
    if not m:
        return text, False
    return text[m.end():].strip(), True


def strip_tail(text: str, head_matched: bool) -> tuple[str, bool]:
    pattern = TAIL_PATTERN_LOOSE if head_matched else TAIL_PATTERN_STRICT
    m = pattern.search(text)
    if not m:
        return text, False
    return text[:m.start()].strip(), True


def widget_suspected(text: str) -> bool:
    return sum(text.count(sig) for sig in WIDGET_SIGNATURES) >= 2


def normalize_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def classify_broken(raw, cleaned: str):
    if not isinstance(raw, str) or not raw.strip():
        return "본문없음"
    if HTML_TAG_RE.search(raw):
        return "HTML/스크립트만 캡처됨"
    if len(cleaned) < 80:
        return "정제 후 80자 미만(크롤링 실패 의심)"
    return None


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])(?<!\d\.)\s+")


def split_sentences_fast(text: str) -> list[str]:
    """정규식 기반 문장 분리. 숫자 사이 마침표(6.2%, 2025.01.01)는 문장 경계로 보지 않는다.
    2706건 전체를 1초 이내에 처리 — EDA용 문장수 집계 등 대량 처리에 기본으로 쓴다."""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_BOUNDARY_RE.split(text) if s.strip()]


def split_sentences_kss(text: str) -> list[str]:
    """kss(형태소 분석 기반) 문장 분리. split_sentences_fast보다 정확하지만,
    이 환경엔 C++ 형태소 분석기가 없어 pecab 백엔드로 동작 — 기사 1건에도 수십 초 이상 걸릴 수 있다.
    대량 처리에는 쓰지 말고, 실전1 ②에서 주장 후보로 좁혀진 문장 몇 개를 정밀하게 나눌 때만 사용."""
    import kss  # 느린 의존성이라 실제 호출 시점에만 로드
    return kss.split_sentences(text) if text else []


def preprocess(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    raw_body = df["기사 본문 전체"]

    heads = raw_body.fillna("").map(strip_head)
    after_head = heads.map(lambda t: t[0])
    head_matched = heads.map(lambda t: t[1])

    tails = pd.Series(
        [strip_tail(t, hm) for t, hm in zip(after_head, head_matched)],
        index=df.index,
    )
    after_tail = tails.map(lambda t: t[0])

    cleaned = after_tail.map(normalize_whitespace)

    df["본문_정제"] = cleaned
    df["헤더_제거됨"] = heads.map(lambda t: t[1])
    df["푸터_제거됨"] = tails.map(lambda t: t[1])
    df["위젯잔재_의심"] = cleaned.map(widget_suspected)
    df["본문_상태"] = [classify_broken(r, c) for r, c in zip(raw_body, cleaned)]
    df["본문_길이"] = cleaned.map(len)
    df["문장수"] = cleaned.map(lambda t: len(split_sentences_fast(t)) if t else 0)

    return df


def summarize(df: pd.DataFrame) -> str:
    n = len(df)
    lines = [
        f"전체 {n}건",
        f"헤더(nav/바이라인) 제거됨: {df['헤더_제거됨'].sum()}건 ({df['헤더_제거됨'].mean() * 100:.1f}%)",
        f"푸터(저작권/광고) 제거됨: {df['푸터_제거됨'].sum()}건 ({df['푸터_제거됨'].mean() * 100:.1f}%)",
        f"위젯 잔재 의심(수동 확인 권장): {df['위젯잔재_의심'].sum()}건",
        f"본문 손상(크롤링 실패 의심, 검증 대상에서 제외 권장): {df['본문_상태'].notna().sum()}건",
    ]
    if df["본문_상태"].notna().any():
        lines.append("  손상 사유별: " + str(df["본문_상태"].value_counts().to_dict()))
    return "\n".join(lines)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    df = pd.read_csv(RAW_PATH, encoding="utf-8-sig")
    result = preprocess(df)
    result.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")

    print(summarize(result))
    print(f"\n저장 완료: {OUT_PATH}")
