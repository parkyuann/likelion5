"""
뉴스 본문 전처리 — 통합본.

세 갈래로 따로 진행됐던 전처리 로직을 하나로 합쳤다:
  - (기존) 이 파일의 v1 — "입력 날짜" 바이라인 패턴만으로 헤더/푸터를 자르는 범용 버전
  - 팀원 A(preprocessing.ipynb) — 조선일보 페이지 구조를 실측 분석해 만든 버전.
    헤더 블록이 페이지에 두 번 반복된다는 것(바이라인은 첫 번째 "입력" 앞, 본문은
    마지막 타임스탬프 뒤에서 잘라야 함)과, 작성자(기자)/섹션(URL에서 복원)을
    추가로 뽑아낼 수 있다는 걸 찾아냄. 작성자 추출률 97.7% 실측.
  - 팀원 B(eda.ipynb) — "기사제목이 본문 텍스트 안에 그대로 들어있다"는 걸 헤더 절삭의
    앵커로 씀. 날짜 패턴보다 강건해서(제목은 항상 원본 그대로 있음) 프리뷰/유료화
    스니펫처럼 날짜 패턴이 아예 없는 경우에도 잘 동작. 유니코드 정규화(NFKC)와
    제어문자/장식기호 제거, 관련기사·광고·댓글 마커 절삭도 별도로 갖추고 있었음.

병합 원칙:
  - 헤더 절삭은 "기사제목 앵커(B)"를 우선 적용하고, 제목이 본문에 그대로 안 나오는
    경우(프리뷰 스니펫 등)에만 "입력 날짜 앵커(A/v1)"로 폴백한다.
  - 팀원 A가 찾은 "헤더가 두 번 반복된다" 문제 대응(바이라인=첫 타임스탬프 앞,
    본문 시작=마지막 타임스탬프 뒤)을 그대로 채택 — 이게 없으면 작성자 추출률이
    32.8%까지 떨어지는 게 실측으로 확인된 부분이라 반드시 유지.
  - 작성자/섹션 컬럼은 팀원 A 방식 그대로 신규 추가.
  - 꼬리(푸터) 절삭 마커는 세 버전이 서로 다른 마커를 썼는데(#해시태그·100자평·
    Taboola·많이 본 뉴스·AI 추천 / 무단전재·저작권자ⓒ·Copyright·기사 전체보기 /
    관련기사·광고·댓글 마커) 겹치는 게 거의 없었다 — 세 버전 마커를 전부 합집합으로
    채택해 한 버전만 썼으면 놓쳤을 잔재까지 잡히게 했다.
  - 최종 산출 컬럼명은 `본문_정제`로 유지한다 — `claim_extractor.py`가 이 컬럼명을
    그대로 읽고 있어서, 이름을 바꾸면 하위 파이프라인이 전부 깨진다.
  - 이 함수는 행을 삭제하지 않는다(팀원 B와 동일한 원칙) — 원본 행 수를 보존해야
    다른 컬럼(레이블 등)과의 매핑이 안 깨지고, 결측/저품질 필터링은
    `filter_low_quality()`로 분리해 옵션으로 남겨뒀다(팀원 A의 MIN_BODY_LEN/중복
    제거 로직을 그대로 가져온 것 — 필요할 때만 명시적으로 호출).

알려진 한계:
  - "관련기사 더보기" 티저나 기자 소개 문구가 본문 뒤에 소량 남을 수 있다. "더보기"는
    전체의 36%에서 등장하는데 진짜 절삭 마커인지 본문 일부인지 구분이 어려워
    자동 마커로 넣지 않았다(v1과 동일 판단 유지) — 잘못 넣으면 정상 문장을 잘라먹을
    위험이 더 크다.
  - 위젯잔재_의심 플래그로 안 잡히는 잔여 잡음이 있을 수 있다. 수치 기반 주장 추출 시
    표본을 뽑아 육안 확인을 권장.

실행: (레포 루트에서, venv) venv/Scripts/python.exe src/news_preprocessor.py
입력: data/AI_기반_뉴스_사실검증_시스템_프로젝트_데이터.csv
출력: data/news_preprocessed.csv
"""
import html
import re
import sys
import unicodedata

import pandas as pd

RAW_PATH = "data/AI_기반_뉴스_사실검증_시스템_프로젝트_데이터.csv"
OUT_PATH = "data/news_preprocessed.csv"

# --- 헤더 앵커 ---

TS_FIRST = re.compile(r"입력\s*\d{4}\.\d{2}\.\d{2}\.?\s*\d{1,2}:\d{2}")
TS_FULL = re.compile(
    r"입력\s*\d{4}\.\d{2}\.\d{2}\.?\s*\d{1,2}:\d{2}"
    r"(?:\s*업데이트\s*\d{4}\.\d{2}\.\d{2}\.?\s*\d{1,2}:\d{2})?"
    r"(?:\s*\d{1,4}(?=\s))?"  # 댓글 수 — 뒤에 공백이 와야만 소비("5년간"류 오탐 방지)
)
BYLINE_RE = re.compile(r"((?:[가-힣]{2,4}\s*(?:기자|특파원)\s*(?:\([^)]{1,12}\))?\s*)+)$")

_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "—": "-", "–": "-",
})


def normalize_boundary_text(text: str) -> str:
    """제목 경계 비교용 — 따옴표·대시·말줄임표 표기 차이를 통일."""
    text = text.translate(_QUOTE_TRANSLATION)
    text = re.sub(r"\.{2,}", "…", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def find_title_end(text: str, title) -> int:
    if not isinstance(title, str) or not title.strip():
        return -1
    t = normalize_boundary_text(unicodedata.normalize("NFKC", title))
    pos = text.find(t)
    return pos + len(t) if pos >= 0 else -1


# --- 꼬리(푸터) 마커: 세 버전 마커의 합집합 ---
# '#' 바로 뒤에 공백 없이 글자가 오는 경우만 해시태그로 인정한다(팀원 A 실측: '#' 단독
# 앵커를 쓰면 "# 2024년 12월 현대차는..." 같은 리드 기호를 해시태그로 오인해 본문이
# 통째로 날아가는 사례가 있었음).
TAIL_MARKERS = re.compile(
    r"#[가-힣A-Za-z0-9]"
    r"|100자평|도움말\s*삭제기준|By\s*Taboola|많이\s*본\s*뉴스|AI\s*추천"
    r"|무단\s*전재|저작권자\s*ⓒ|Copyright\s*조선일보|English\s*기사보기|기사\s*전체보기"
    r"|구독수\s*\d|당신이\s*좋아할\s*만한\s*콘텐츠|오늘의\s*멤버십|지금\s*뜨는\s*콘텐츠"
    r"|관련\s?기사|추천\s?기사|더보기"
)

DECORATIVE_SYMBOL_RE = re.compile(r"[◇◆◈■□▲△▼▽▶▷◀◁●○◎※★☆♠♣♥♦]+")
WHITESPACE_RE = re.compile(r"\s+")
HTML_TAG_RE = re.compile(r"<div|<script")
SECTION_RE = re.compile(r"chosun\.com/([^/]+)/")


def _remove_control_characters(text: str) -> str:
    return "".join(
        ch for ch in text
        if ch in ("\n", "\t") or unicodedata.category(ch) not in {"Cc", "Cf"}
    )


def strip_head(text: str, title) -> tuple[str, str | None, bool, bool]:
    """반환: (헤더 제거 후 본문, 작성자, 헤더_앵커_찾음, 프리뷰_여부)"""
    text = unicodedata.normalize("NFKC", text)
    text = html.unescape(text)
    text = _remove_control_characters(text)
    text = DECORATIVE_SYMBOL_RE.sub(" ", text)
    text = normalize_boundary_text(text)

    title_end = find_title_end(text, title)
    rest = text[title_end:] if title_end >= 0 else text

    ts_matches = list(TS_FULL.finditer(rest))
    if not ts_matches:
        # 제목 앵커도 날짜 앵커도 못 찾음 -> 유료화 프리뷰 스니펫으로 간주
        return rest.strip(), None, False, True

    byline_zone = rest[: ts_matches[0].start()]  # 바이라인은 '첫' 타임스탬프 앞
    m = BYLINE_RE.search(byline_zone.strip())
    if m:
        author = re.sub(r"\s+", " ", m.group(1)).strip()
    elif byline_zone.strip().endswith("조선일보"):
        author = "조선일보"
    else:
        author = None  # 외부 필진(교수/소장 등) 또는 무기명

    body = rest[ts_matches[-1].end():]  # 본문은 '마지막' 타임스탬프 뒤
    return body.strip(), author, True, False


def strip_tail(body: str, author: str | None) -> tuple[str, bool]:
    marks = TAIL_MARKERS.pattern
    if author and "기자" in author:
        # 본문 뒤에 재등장하는 '기자명 + 프로필' 블록을 종료 앵커로 추가
        marks += rf"|{re.escape(author.split()[0])}\s*기자"
    m = re.search(marks, body)
    if not m:
        return body.strip(), False
    return body[: m.start()].strip(), True


def extract_section(url) -> str | None:
    m = SECTION_RE.search(str(url))
    return m.group(1) if m else None


def widget_suspected(text: str) -> bool:
    signatures = ["돌아가기", "오늘의 핫뉴스", "많이 본 뉴스", "당신이 좋아할 만한 콘텐츠", "By Taboola", "100자평"]
    return sum(text.count(sig) for sig in signatures) >= 2


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
    titles = df["기사제목"]

    heads = [
        strip_head(b, t) if isinstance(b, str) and b.strip() else ("", None, False, False)
        for b, t in zip(raw_body, titles)
    ]
    after_head = [h[0] for h in heads]
    authors = [h[1] for h in heads]
    head_matched = [h[2] for h in heads]
    is_preview = [h[3] for h in heads]

    tails = [strip_tail(b, a) for b, a in zip(after_head, authors)]
    after_tail = [t[0] for t in tails]
    tail_matched = [t[1] for t in tails]

    cleaned = [normalize_whitespace(b) for b in after_tail]

    df["본문_정제"] = cleaned
    df["작성자"] = authors
    df["섹션"] = df["URL"].map(extract_section)
    df["헤더_제거됨"] = head_matched
    df["푸터_제거됨"] = tail_matched
    df["포맷"] = [
        "D_프리뷰" if p else ("B_전체페이지" if tm else "C_푸터없음")
        for p, tm in zip(is_preview, tail_matched)
    ]
    df["위젯잔재_의심"] = [widget_suspected(c) for c in cleaned]
    df["본문_상태"] = [classify_broken(r, c) for r, c in zip(raw_body, cleaned)]
    df["본문_길이"] = [len(c) for c in cleaned]
    df["문장수"] = [len(split_sentences_fast(c)) if c else 0 for c in cleaned]

    return df


def filter_low_quality(df: pd.DataFrame, min_body_len: int = 200) -> pd.DataFrame:
    """선택적 필터링(팀원 A 방식) — 기본 preprocess()는 행을 지우지 않으므로,
    학습/평가용으로 저품질 행을 걷어내고 싶을 때만 별도로 호출한다."""
    before = len(df)
    out = df[df["본문_정제"].ne("")]
    out = out[out["포맷"].ne("D_프리뷰")]
    out = out[out["본문_길이"] >= min_body_len]
    out = out[~out["URL"].duplicated(keep="first")]
    out = out[~out.duplicated(subset=["기사제목", "본문_정제"], keep="first")]
    print(f"필터링: {before}행 -> {len(out)}행 (잔존율 {len(out) / before * 100:.1f}%)")
    return out.reset_index(drop=True)


def summarize(df: pd.DataFrame) -> str:
    n = len(df)
    lines = [
        f"전체 {n}건",
        f"헤더(제목/날짜 앵커) 제거됨: {df['헤더_제거됨'].sum()}건 ({df['헤더_제거됨'].mean() * 100:.1f}%)",
        f"작성자 추출됨: {df['작성자'].notna().sum()}건 ({df['작성자'].notna().mean() * 100:.1f}%) | 고유 {df['작성자'].nunique()}명",
        f"푸터(저작권/광고/댓글) 제거됨: {df['푸터_제거됨'].sum()}건 ({df['푸터_제거됨'].mean() * 100:.1f}%)",
        f"포맷 분포: {df['포맷'].value_counts().to_dict()}",
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
