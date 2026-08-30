"""뉴스 claim의 문장 밖 지시 대상을 보수적으로 보강한다.

이 모듈은 대명사·생략 주어를 임베딩 모델에 맡겨 추측하지 않는다. 같은 기사 제목과
앞 문장에 있는 구체 명사 후보가 정확히 하나일 때만 검색 query에 보강하고, 여러 후보면
모호 상태와 근거만 남겨 이후 셀 정렬을 막는다.
"""

from __future__ import annotations

import re
import json
from typing import Any, Iterable


CONTEXT_WINDOW_SENTENCES = 3
CONTEXT_STATUSES = {
    "NOT_APPLICABLE", "EXPLICIT", "RESOLVED", "REFERENT_CANDIDATE", "REFERENT_AMBIGUOUS", "CONTEXT_MISSING",
}

# 첫 구현은 보험처럼 표 검색 결과를 크게 바꾸는 도메인 head noun부터 다룬다. 다른
# 도메인 head noun은 fixture와 gold 근거가 쌓인 뒤 같은 계약으로 확장한다.
SPECIFIC_INSURANCE_RE = re.compile(r"(?<![가-힣])([가-힣A-Za-z]{1,12}보험)(?:료|업|시장|상품)?")
GENERIC_INSURANCE_RE = re.compile(r"(?<![가-힣])보험(?:료|업|시장|상품)?")
DEMONSTRATIVE_RE = re.compile(r"(?:이|그|해당|동일한|앞서\s*언급한)\s*(?:보험|수치|업계|상품)")


def _specific_terms(text: str, *, sentence_index: int, source: str) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    for match in SPECIFIC_INSURANCE_RE.finditer(text or ""):
        terms.append({
            "term": match.group(1), "sentence_index": sentence_index, "source": source,
            "source_span": {"start": match.start(1), "end": match.end(1)}, "evidence_text": text,
        })
    return terms


def _unique_terms(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(str(candidate["term"]), candidate)
    return list(unique.values())


def resolve_claim_context(
    claim_text: str,
    *,
    sentence_index: int | None,
    article_sentences: list[str],
    article_title: str | None = None,
    window_sentences: int = CONTEXT_WINDOW_SENTENCES,
) -> dict[str, Any]:
    """지시 대상과 근거를 반환한다. 원문 claim과 기존 추출 필드는 수정하지 않는다."""
    explicit = _unique_terms(_specific_terms(claim_text, sentence_index=sentence_index if sentence_index is not None else -1, source="claim"))
    if explicit:
        return {
            "status": "EXPLICIT", "resolved_terms": [entry["term"] for entry in explicit],
            "evidence": explicit, "retrieval_policy": "context_expanded",
        }

    explicit_reference = bool(DEMONSTRATIVE_RE.search(claim_text or ""))
    requires_context = bool(GENERIC_INSURANCE_RE.search(claim_text or "") or explicit_reference)
    if not requires_context:
        return {"status": "NOT_APPLICABLE", "resolved_terms": [], "evidence": [], "retrieval_policy": "claim_only"}

    candidates: list[dict[str, Any]] = []
    if sentence_index is not None:
        start = max(0, sentence_index - max(window_sentences, 0))
        for index in range(start, min(sentence_index, len(article_sentences))):
            candidates.extend(_specific_terms(article_sentences[index], sentence_index=index, source="article_sentence"))
    if not candidates and article_title:
        candidates.extend(_specific_terms(article_title, sentence_index=-1, source="article_title"))
    candidates = _unique_terms(candidates)

    if len(candidates) == 1:
        # "보험료"처럼 bare head noun은 직전 문장의 특정 보험이 아니라 업계 일반을
        # 뜻할 수 있다. 직접 지시어가 있는 경우만 확정하고, 나머지는 후속 HCX/사람
        # 판단용 후보로 보존하되 검색 query·셀 정렬에는 쓰지 않는다.
        if not explicit_reference:
            return {
                "status": "REFERENT_CANDIDATE", "resolved_terms": [], "candidate_terms": [candidates[0]["term"]],
                "evidence": candidates, "retrieval_policy": "claim_only_alignment_blocked",
            }
        return {
            "status": "RESOLVED", "resolved_terms": [candidates[0]["term"]], "evidence": candidates,
            "retrieval_policy": "context_expanded",
        }
    if len(candidates) > 1:
        return {
            "status": "REFERENT_AMBIGUOUS", "resolved_terms": [], "candidate_terms": [entry["term"] for entry in candidates],
            "evidence": candidates, "retrieval_policy": "claim_only_alignment_blocked",
        }
    return {
        "status": "CONTEXT_MISSING", "resolved_terms": [], "evidence": [],
        "retrieval_policy": "claim_only_alignment_blocked",
    }


def build_contextual_query(claim_text: str, resolution: dict[str, Any]) -> str:
    """확정된 문맥만 검색 query에 추가한다. 모호 후보는 query를 오염시키지 않는다."""
    terms = resolution.get("resolved_terms") if isinstance(resolution.get("resolved_terms"), list) else []
    if resolution.get("status") in {"EXPLICIT", "RESOLVED"} and terms:
        return f"{claim_text}\n문맥 확정 대상: {' '.join(str(term) for term in terms)}"
    return claim_text


def augment_article_claim_rows(rows: list[dict[str, Any]], *, article_title: str, article_sentences: list[str]) -> list[dict[str, Any]]:
    """추출 행에 CSV 왕복 가능한 문맥 audit JSON과 검색 query를 추가한다."""
    for row in rows:
        resolution = resolve_claim_context(
            str(row.get("claim_text") or ""), sentence_index=row.get("sentence_index"),
            article_sentences=article_sentences, article_title=article_title,
        )
        row["context_resolution_json"] = json.dumps(resolution, ensure_ascii=False, sort_keys=True)
        row["retrieval_query_text"] = build_contextual_query(str(row.get("claim_text") or ""), resolution)
    return rows


