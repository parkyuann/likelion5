"""keyword_extractor.py — 형태소 기반 키워드 추출 (실험2: BM25 개선)

멘토 피드백: BM25에 문장을 통째로 넣지 말고 핵심 키워드만 추출해 넣을 것.
kiwipiepy(순수 파이썬, Java 불필요)로 명사/고유명사만 뽑아 BM25 토큰 품질을 높인다.
질의(claim)와 표 텍스트(문서) 양쪽에 동일하게 적용한다.

CLI:
    python src/keyword_extractor.py --text "지난해 혼인 건수가 1% 감소했다"
"""
from __future__ import annotations

import argparse

_KIWI = None

# 통계 도메인에서 변별력이 낮은 흔한 표현(불용어). 필요 시 확장.
_BASE_STOPWORDS = {
    "지난해", "작년", "올해", "전년", "대비", "기준", "관련", "경우", "정도",
    "가운데", "가량", "당해", "이상", "이하", "각각", "전체", "최근", "지난",
}
# 뉴스 보도체 상투어 — 문장에 늘 붙지만 통계표를 특정하지 못한다.
# (주의: '조사·현황·분석'은 KOSIS 표 이름에 자주 쓰이므로 일부러 넣지 않음)
REPORT_STOPWORDS = {
    "발표", "자료", "보도", "기자", "관계자", "이날", "이번", "한편",
    "인근", "포함", "설명", "예정", "방침", "내용", "수준",
}
# 발표 주체(기관) — 주제가 아니라 출처라서, 표 검색 질의에 넣으면 여러 표에 공통으로 걸려 노이즈가 된다.
# (기관 제한이 필요하면 키워드가 아니라 org_id 메타 필터로 거는 게 맞다)
ORG_STOPWORDS = {
    "통계청", "국가데이터처", "한국은행", "관세청", "국세청", "기획재정부",
    "고용노동부", "보건복지부", "국토교통부", "행정안전부", "산업통상자원부",
    "금융감독원", "한국석유공사", "한국부동산원", "정부", "당국",
}
STOPWORDS = _BASE_STOPWORDS | REPORT_STOPWORDS | ORG_STOPWORDS
# 일반명사·고유명사·외국어(영문 지표명 등) — 기본(명사 위주)
DEFAULT_TAGS = ("NNG", "NNP", "SL")
# 확장: 명사 + 동사(VV) + 형용사(VA) + 부사(MAG) + 조사(J*) — 실험용(넓게 다 넣기)
EXPANDED_TAGS = ("NNG", "NNP", "SL", "VV", "VA", "MAG", "VX",
                 "JKS", "JKC", "JKG", "JKO", "JKB", "JKV", "JKQ", "JX", "JC")


def _kiwi():
    global _KIWI
    if _KIWI is None:
        from kiwipiepy import Kiwi
        _KIWI = Kiwi()
    return _KIWI


def extract_tokens(text: str, tags=DEFAULT_TAGS, min_len: int = 2,
                   drop_stopwords: bool = True, join_suffix: bool = False,
                   with_pos: bool = False):
    """형태소를 뽑아 기준에 맞는 토큰만 반환한다.

    join_suffix=True면 접미사(XSN, 예: 실업+률)를 앞 명사에 붙여 '실업률'로 살린다.
    with_pos=True면 (형태, 품사태그) 튜플로 반환(어떤 토큰이 왜 남았는지 확인용).
    """
    if not text:
        return []
    merged: list[tuple[str, str]] = []
    for tok in _kiwi().tokenize(text):
        if join_suffix and tok.tag == "XSN" and merged:      # 접미사를 앞 토큰에 붙임
            pform, ptag = merged[-1]
            merged[-1] = (pform + tok.form, ptag)
        else:
            merged.append((tok.form, tok.tag))
    out, seen = [], set()
    for form, tag in merged:
        if tag not in tags:
            continue
        if len(form) < min_len or (drop_stopwords and form in STOPWORDS):
            continue
        if form in seen:
            continue
        seen.add(form)
        out.append((form, tag) if with_pos else form)
    return out


def extract_keywords(text: str, tags=DEFAULT_TAGS, min_len: int = 2,
                     drop_stopwords: bool = True) -> list[str]:
    """텍스트에서 키워드(기본: 명사류)만 순서·중복 제거해 반환한다(기존 동작 유지)."""
    return extract_tokens(text, tags, min_len, drop_stopwords)


def keyword_text(text: str, top_n: int | None = None) -> str:
    """키워드를 공백으로 이어 BM25/검색 질의 문자열로 만든다."""
    kws = extract_keywords(text)
    return " ".join(kws[:top_n] if top_n else kws)


_CATALOG_VOCAB: set[str] | None = None


def catalog_vocab(catalog_path=None) -> set[str]:
    """카탈로그(표명·항목명·차원명)에 실제로 등장하는 어휘 집합. 최초 1회 구축."""
    global _CATALOG_VOCAB
    if _CATALOG_VOCAB is None:
        import json
        from pathlib import Path
        p = Path(catalog_path) if catalog_path else (
            Path(__file__).resolve().parents[2] / "data" / "kosis_catalog_enriched_sample600.jsonl")
        vocab: set[str] = set()
        if p.exists():
            with p.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    texts = [r.get("tbl_name") or ""]
                    texts += [i.get("itm_nm") or "" for i in (r.get("items") or [])]
                    texts += [d.get("obj_nm") or "" for d in (r.get("dimensions") or [])]
                    for t in texts:
                        vocab.update(extract_tokens(t, drop_stopwords=False))
        _CATALOG_VOCAB = vocab
    return _CATALOG_VOCAB


def retrieval_keywords(text: str, use_catalog_vocab: bool = True) -> list[str]:
    """검색 질의용 키워드 — 불용어 제거 + (옵션) 카탈로그에 실제 있는 말만 남긴다.

    손으로 만든 불용어 목록으로는 사건별 고유명사('무안', '푸딩이')를 다 막을 수 없다.
    카탈로그 어휘 사전으로 걸러내면 "통계표 어휘에 아예 없는 말"이 데이터 기반으로 제거된다.
    결과가 빈 리스트면 = 통계표 어휘와 겹치는 말이 하나도 없다는 뜻(통계 주장이 아닐 신호).
    """
    kws = extract_keywords(text)
    if not use_catalog_vocab:
        return kws
    vocab = catalog_vocab()
    return [k for k in kws if k in vocab] if vocab else kws
