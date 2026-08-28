"""L2 — article-level segmentation contract.

팀 인계: 현재 파이프라인에서 HCX를 호출하는 유일한 층이다. 기사 단위의 지표,
출처 영역, 기간 문맥을 반환하며 최종 검색 필드는 생성하지 않는다.

The layer answers three questions per sentence and nothing else:

1. which indicator governs it, marked by a span copied from the sentence,
2. which source region it belongs to, or that none does,
3. which period context applies.

The model never emits retrieval fields, IDs or character offsets.  Spans are
returned as verbatim text and resolved deterministically, so a hallucinated
span fails loudly instead of entering the pipeline.  Everything downstream of
the layout — inheritance, period absolutisation, unit typing — is deterministic
code in later layers.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Callable

try:
    from .hcx_client import call_hcx_json as _call_hcx_json
    from .l1_value_candidates import build_span_candidates, sentence_offset_map
    from .l2_span_resolver import SpanResolutionError, resolve_span
except ImportError:  # pragma: no cover - direct script execution
    from hcx_client import call_hcx_json as _call_hcx_json
    from l1_value_candidates import build_span_candidates, sentence_offset_map
    from l2_span_resolver import SpanResolutionError, resolve_span


SentenceSpanIterator = Callable[[str], Iterator[tuple[int, int, int, str]]]


def value_candidates_by_sentence(
    article_text: str,
    *,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> dict[int, list[str]]:
    """Return the frozen L1 value candidates keyed by sentence.

    Showing the reviewer-visible values makes the indicator question concrete:
    the model names what each number measures instead of summarising the
    sentence with one label.
    """
    grouped: dict[int, list[str]] = {}
    candidates = (
        build_span_candidates(article_text, sentence_span_iterator=sentence_span_iterator)
        if sentence_span_iterator is not None else build_span_candidates(article_text)
    )
    for candidate in candidates:
        if candidate.get("kind") != "value_unit":
            continue
        grouped.setdefault(candidate["sentence_id"], []).append(
            str(candidate.get("text") or "")
        )
    return grouped


SOURCE_SUBTYPES = ("공식집계", "민간조사", "정책목표", "잠정추산", "법정기준")
DOMINANCE_NONE = "지배 없음"
_SPAN_ERROR_CODES = {
    "EMPTY",
    "NOT_FOUND",
    "AMBIGUOUS",
    "OCCURRENCE_INDEX_INVALID",
    "UNKNOWN",
}


def _normalize_span_error_code(exc: SpanResolutionError, span_text: str) -> str:
    """Map resolver diagnostics to the finite L2 receipt vocabulary."""
    message = str(exc).lower()
    if not str(span_text).strip() or "empty" in message:
        return "EMPTY"
    if "ambiguous" in message:
        return "AMBIGUOUS"
    if "occurrence_index" in message:
        return "OCCURRENCE_INDEX_INVALID"
    if "not found" in message:
        return "NOT_FOUND"
    return "UNKNOWN"


def _unresolved_span_detail(
    sentence_id: int,
    field: str,
    span_text: str,
    exc: SpanResolutionError,
) -> dict[str, Any]:
    code = _normalize_span_error_code(exc, span_text)
    if code not in _SPAN_ERROR_CODES:  # pragma: no cover - defensive contract guard
        code = "UNKNOWN"
    return {
        "sentence_id": int(sentence_id),
        "field": field,
        "source_span_text": str(span_text)[:512],
        "span_error_code": code,
    }


class HcxSpanResolutionError(SpanResolutionError):
    """Bounded failure from the HCX model-span normalization layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_HCX_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'",
    "’": "'",
    "ʼ": "'",
    "“": '"',
    "”": '"',
})
_HCX_CONNECTIVE_PREFIXES = (
    "는데",
    "지만",
    "으며",
    "면서",
    "도록",
    "다가",
    "고",
    "서",
    "아서",
    "어서",
    "여서",
    "라서",
    "므로",
    "기에",
)
_HCX_TERMINAL_SUFFIXES = {
    "다",
    "요",
    "죠",
    "니다",
    "습니다",
    "이다",
    "였다",
    "했다",
    "한다",
    "된다",
    "있다",
    "없다",
}


def _validate_hcx_model_span_inputs(sentence_text: str, span_text: str) -> None:
    if not isinstance(sentence_text, str) or not sentence_text:
        raise HcxSpanResolutionError("UNKNOWN", "sentence text is empty")
    if not isinstance(span_text, str) or not span_text.strip():
        raise HcxSpanResolutionError("EMPTY", "source_span_text is empty")


def _occurrences(sentence_text: str, span_text: str) -> list[int]:
    starts: list[int] = []
    cursor = sentence_text.find(span_text)
    while cursor >= 0:
        starts.append(cursor)
        cursor = sentence_text.find(span_text, cursor + 1)
    return starts


def _normalized_span_text(text: str, *, ignore_whitespace: bool) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text.translate(_HCX_QUOTE_TRANSLATION)):
        if ignore_whitespace and char.isspace():
            continue
        normalized.append(char)
        offsets.append(index)
    return "".join(normalized), offsets


def _find_normalized_matches(
    sentence_text: str,
    span_text: str,
    *,
    ignore_whitespace: bool,
) -> tuple[list[int], list[int]]:
    sentence_normalized, offsets = _normalized_span_text(
        sentence_text,
        ignore_whitespace=ignore_whitespace,
    )
    span_normalized, _ = _normalized_span_text(
        span_text,
        ignore_whitespace=ignore_whitespace,
    )
    return _occurrences(sentence_normalized, span_normalized), offsets


def find_hcx_model_span_matches(sentence_text: str, span_text: str) -> list[int]:
    """Return unique-candidate offsets after quote/space normalization."""
    _validate_hcx_model_span_inputs(sentence_text, span_text)
    matches, offsets = _find_normalized_matches(
        sentence_text,
        span_text,
        ignore_whitespace=True,
    )
    return [offsets[start] for start in matches]


def _morphological_prefix_matches(
    sentence_text: str,
    span_text: str,
) -> list[tuple[int, int, list[int]]]:
    source_normalized, offsets = _normalized_span_text(
        sentence_text,
        ignore_whitespace=True,
    )
    model_normalized, _ = _normalized_span_text(
        span_text.rstrip(" .,!?:;。！？…"),
        ignore_whitespace=True,
    )
    minimum_prefix = max(8, (len(model_normalized) * 3) // 5)
    candidates: set[tuple[int, int]] = set()
    for cut in range(len(model_normalized) - 1, minimum_prefix - 1, -1):
        model_suffix = model_normalized[cut:]
        if model_suffix not in _HCX_TERMINAL_SUFFIXES:
            continue
        prefix = model_normalized[:cut]
        for start in _occurrences(source_normalized, prefix):
            source_suffix = source_normalized[start + cut:]
            if any(source_suffix.startswith(value) for value in _HCX_CONNECTIVE_PREFIXES):
                candidates.add((start, cut))
    return [(start, cut, offsets) for start, cut in sorted(candidates)]


def resolve_hcx_model_span(sentence_text: str, span_text: str) -> dict[str, Any]:
    """Resolve an HCX span exactly, by quote/space equivalence, or safely by morphology."""
    _validate_hcx_model_span_inputs(sentence_text, span_text)
    try:
        return resolve_span(sentence_text, span_text)
    except SpanResolutionError:
        pass

    quote_sentence = sentence_text.translate(_HCX_QUOTE_TRANSLATION)
    quote_span = span_text.translate(_HCX_QUOTE_TRANSLATION)
    quote_matches = _occurrences(quote_sentence, quote_span)
    if len(quote_matches) == 1:
        start = quote_matches[0]
        source_span_text = sentence_text[start:start + len(span_text)]
        if len(source_span_text) == len(span_text):
            return {
                "source_span_text": source_span_text,
                "source_char_start": start,
                "source_char_end": start + len(source_span_text),
                "model_source_span_text": span_text,
                "span_match_mode": "QUOTE_EQUIVALENT",
                "offset_provenance": "DERIVED_FROM_MODEL_SPAN_QUOTE_EQUIVALENT",
            }
    if len(quote_matches) > 1:
        raise HcxSpanResolutionError("AMBIGUOUS", "source_span_text is ambiguous")

    normalized_matches, offsets = _find_normalized_matches(
        sentence_text,
        span_text,
        ignore_whitespace=True,
    )
    if len(normalized_matches) == 1:
        start = normalized_matches[0]
        end = offsets[start + len(_normalized_span_text(span_text, ignore_whitespace=True)[0]) - 1] + 1
        source_span_text = sentence_text[offsets[start]:end]
        return {
            "source_span_text": source_span_text,
            "source_char_start": offsets[start],
            "source_char_end": end,
            "model_source_span_text": span_text,
            "span_match_mode": "WHITESPACE_EQUIVALENT",
            "offset_provenance": "DERIVED_FROM_MODEL_SPAN_WHITESPACE_EQUIVALENT",
        }
    if len(normalized_matches) > 1:
        raise HcxSpanResolutionError("AMBIGUOUS", "source_span_text is ambiguous")

    morphological_matches = _morphological_prefix_matches(sentence_text, span_text)
    if len(morphological_matches) == 1:
        start, length, source_offsets = morphological_matches[0]
        source_start = source_offsets[start]
        source_end = source_offsets[start + length - 1] + 1
        return {
            "source_span_text": sentence_text[source_start:source_end],
            "source_char_start": source_start,
            "source_char_end": source_end,
            "model_source_span_text": span_text,
            "span_match_mode": "MORPHOLOGICAL_CONTAINMENT",
            "offset_provenance": "DERIVED_FROM_MODEL_SPAN_MORPHOLOGICAL_CONTAINMENT",
        }
    if len(morphological_matches) > 1:
        raise HcxSpanResolutionError("AMBIGUOUS", "source_span_text is ambiguous")
    raise HcxSpanResolutionError(
        "NOT_FOUND",
        f"source_span_text not found in sentence: {span_text!r}",
    )


def _apply_hcx_span_resolution(target: dict[str, Any], span: dict[str, Any]) -> None:
    """Attach model-span offsets without changing exact-result fields."""
    target["source_char_start"] = span["source_char_start"]
    target["source_char_end"] = span["source_char_end"]
    if span.get("span_match_mode"):
        target["source_span_text"] = span["source_span_text"]
        target["model_source_span_text"] = span["model_source_span_text"]
        target["span_match_mode"] = span["span_match_mode"]
        target["offset_provenance"] = span["offset_provenance"]

L2_SOURCE_SYSTEM_PROMPT = """당신은 한국어 경제·사회 기사에서 각 문장이 누구
자료를 말하고 있는지만 표시한다. 지표나 수치의 의미는 판단하지 않는다.

문장마다 답할 것은 하나다 — 이 문장을 지배하는 출처가 **어느 문장에서
열렸는가**.

기사를 처음부터 순서대로 읽으면서 "현재 열려 있는 출처"를 계속 들고 간다.

- 문장이 출처를 밝히면(`통계청에 따르면`, `한국은행이 발표한`,
  `경총 조사에 따르면`, `현행법상`) 그 문장이 출처를 **연다**.
  opens_region=true, source_subtype과 근거 표현을 함께 반환하고
  governing_sentence_id는 자기 자신으로 한다.
- 출처를 새로 밝히지 않았는데 앞에서 열린 출처의 수치·사실을 계속 보고하고
  있으면 governing_sentence_id를 **그 출처를 연 문장 번호**로 한다.
  한국어 기사는 출처를 한 번만 쓰고 이후 반복하지 않으므로 이 경우가 가장
  흔하다.
- governing_sentence_id를 null로 두는 것은 그 문장이 **어느 출처의 수치도
  말하고 있지 않을 때뿐**이다. 기자의 해석, 전문가 인용, 배경 설명,
  개별 기업 사례, 용어 풀이가 여기 해당한다.

**null은 기본값이 아니다.** 통계 기사에서는 출처를 여는 문장 하나 뒤로 여러
문장이 그 출처를 상속한다. 수치가 있는 문장인데 null이라면 다시 확인하라.

source_subtype은 다음 5종이다.
  공식집계 — 정부·공공기관이 정기적으로 집계·공표하는 통계
  민간조사 — 협회·연구소·기업의 조사·설문
  정책목표 — 정부 사업·계획·목표치
  잠정추산 — 확정 전 추정치
  법정기준 — 법령이 정한 기준값

예시:
  [3] 16일 통계청은 '6월 고용동향'에서 취업자가 2909만명이라고 밝혔다.
      → opens_region=true, subtype=공식집계, governing_sentence_id=3
  [4] 보건업 취업자가 21만명 넘게 증가한 영향이 컸다.
      → opens_region=false, governing_sentence_id=3
  [5] 김 교수는 "저임금 일자리 위주로 늘었다"고 했다.
      → governing_sentence_id=null"""

L2_INDICATOR_SYSTEM_PROMPT = """당신은 한국어 경제·사회 기사에서 각 수치가
무엇을 재는지만 표시한다. 출처가 누구인지는 판단하지 않는다.

문장 뒤에 `값:` 으로 그 문장에서 검출된 수치를 함께 준다.
**각 수치마다 그것이 재는 지표를 하나씩 만든다.** 수치가 3개면 지표도
원칙적으로 3개다. 같은 지표를 재는 수치가 둘 이상일 때만 합친다.

- 각 지표에는 그 판단의 근거가 되는 표현을 원문에서 **그대로 복사**해
  source_span_text에 넣는다. 원문에 없는 문자열을 만들지 않는다.
- 지표명은 검색에 쓸 이름이므로 **구체적으로** 쓴다.
    `근로자`(X) → `단기 근로자 비율`(O)
    `수출`(X) → `대기업 수출액 증가율`(O)
- 앞 문장이 지표를 제시하고 이 문장이 그 지표의 값을 이어서 보고하면,
  앞 문장의 지표명을 그대로 이어 쓴다.
- 수치가 없거나 지표를 특정할 수 없으면 빈 배열을 반환한다.

예시:
  [2] 대기업 수출액은 1223억달러(약 178조원)로 5.1% 증가했다.
      값: 1223억달러 | 178조원 | 5.1%
      → `대기업 수출액(달러)`, `대기업 수출액(원화)`, `대기업 수출액 증가율`"""

L2_SYSTEM_PROMPT = """당신은 한국어 경제·사회 기사의 의미 레이아웃을 표시한다.

기사 전체를 읽고 문장마다 다음 셋을 판단한다.

1. indicator_scopes — 이 문장의 **수치 하나하나가** 무엇을 재는가.

   문장 뒤에 `값:` 으로 그 문장에서 검출된 수치를 함께 준다.
   **각 수치마다 그것이 재는 지표를 하나씩 만든다.** 수치가 3개면 지표도
   원칙적으로 3개다. 같은 지표를 재는 수치가 둘 이상일 때만 합친다.

   - 각 지표에는 그 판단의 근거가 되는 표현을 원문에서 **그대로 복사**해
     source_span_text에 넣는다. 원문에 없는 문자열을 만들지 않는다.
   - 지표명은 검색에 쓸 이름이므로 **구체적으로** 쓴다.
     `근로자`(X) → `단기 근로자 비율`(O)
     `수출`(X) → `대기업 수출액 증가율`(O)
   - 수치가 없거나 지표를 특정할 수 없으면 빈 배열을 반환한다.

   예시:
     [2] 대기업 수출액은 1223억달러(약 178조원)로 5.1% 증가했다.
         값: 1223억달러 | 178조원 | 5.1%
         → 지표 3개: `대기업 수출액(달러)`, `대기업 수출액(원화)`,
                     `대기업 수출액 증가율`

2. source_region — 이 문장을 지배하는 출처가 **어느 문장에서 열렸는가**.

   기사를 처음부터 순서대로 읽으면서 "현재 열려 있는 출처"를 계속 들고 간다.

   - 문장이 출처를 밝히면(`통계청에 따르면`, `한국은행이 발표한`,
     `경총 조사에 따르면`, `현행법상`) 그 문장이 출처를 **연다**.
     opens_region=true, source_subtype과 근거 표현을 함께 반환하고
     governing_sentence_id는 자기 자신으로 한다.
   - 출처를 새로 밝히지 않았는데 앞에서 열린 출처의 수치·사실을 계속
     보고하고 있으면 governing_sentence_id를 **그 출처를 연 문장 번호**로
     한다. 한국어 기사는 출처를 한 번만 쓰고 이후 문장에서 반복하지 않으므로
     이 경우가 가장 흔하다.
   - governing_sentence_id를 null로 두는 것은 그 문장이 **어느 출처의
     수치도 말하고 있지 않을 때뿐**이다. 기자의 해석, 전문가 인용,
     배경 설명, 개별 기업 사례, 용어 풀이가 여기 해당한다.

   **null은 기본값이 아니다.** 통계 기사에서는 출처를 여는 문장 하나 뒤로
   여러 문장이 그 출처를 상속한다. 수치가 있는 문장인데 null이라면 그
   판단을 다시 확인하라.

   예시:
     [3] 16일 통계청은 '6월 고용동향'에서 취업자가 2909만명이라고 밝혔다.
         → opens_region=true, subtype=공식집계, governing_sentence_id=3
     [4] 보건업 취업자가 21만명 넘게 증가한 영향이 컸다.
         → opens_region=false, governing_sentence_id=3   (출처 반복 없음)
     [5] 제조업 분야는 전년 동월 대비 8만3000명 줄었다.
         → opens_region=false, governing_sentence_id=3
     [6] 김 교수는 "저임금 일자리 위주로 늘었다"고 했다.
         → governing_sentence_id=null                    (전문가 인용)

3. period_context — 이 문장의 수치가 언제 기준인가. 원문 표현 그대로.

판단하지 말아야 할 것: 그 수치가 KOSIS에서 조회되는지는 이 단계의 질문이
아니다. 검색 가능성이 아니라 기사의 의미 구조만 표시한다."""


SENTENCE_CHUNK_SIZE = 15


def chunk_sentence_ids(
    article_text: str,
    size: int = SENTENCE_CHUNK_SIZE,
    *,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> list[list[int]]:
    """Split judgement targets so a long article cannot truncate the response."""
    sentences = sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator) if sentence_span_iterator is not None else sentence_offset_map(article_text)
    ids = [row["sentence_id"] for row in sentences]
    if not ids:
        return [[]]
    return [ids[index:index + size] for index in range(0, len(ids), size)]


def build_l2_prompt(
    title: str,
    article_text: str,
    target_ids: list[int] | None = None,
    *,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> str:
    sentences = sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator) if sentence_span_iterator is not None else sentence_offset_map(article_text)
    targets = (
        set(target_ids)
        if target_ids is not None
        else {row["sentence_id"] for row in sentences}
    )
    values = (value_candidates_by_sentence(article_text, sentence_span_iterator=sentence_span_iterator)
              if sentence_span_iterator is not None else value_candidates_by_sentence(article_text))
    lines = []
    for row in sentences:
        sentence_id = row["sentence_id"]
        marker = "▶ " if sentence_id in targets else "   "
        lines.append(f"{marker}[{sentence_id}] {row['text']}")
        if sentence_id in targets and values.get(sentence_id):
            lines.append(
                "      값: " + " | ".join(values[sentence_id])
            )
    rendered = "\n".join(lines)
    return (
        f"기사 제목: {title}\n\n"
        "기사 전문 (▶ 표시된 문장만 판단한다. 나머지는 문맥용이며 "
        "governing_sentence_id로 가리킬 수 있다):\n"
        f"{rendered}\n\n"
        f"▶ 문장 {sorted(targets)} 각각에 대해 정확히 한 번씩 판단을 반환하라."
    )


def build_l2_schema(
    article_text: str,
    target_ids: list[int] | None = None,
    *,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> dict[str, Any]:
    sentences = sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator) if sentence_span_iterator is not None else sentence_offset_map(article_text)
    all_ids = [row["sentence_id"] for row in sentences]
    sentence_ids = list(target_ids) if target_ids is not None else all_ids
    return {
        "type": "object",
        "properties": {
            "sentences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sentence_id": {"type": "integer", "enum": sentence_ids},
                        "indicator_scopes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "indicator_label": {"type": "string"},
                                    "source_span_text": {"type": "string"},
                                },
                                "required": [
                                    "indicator_label",
                                    "source_span_text",
                                ],
                            },
                        },
                        "source_region": {
                            "type": "object",
                            "properties": {
                                "opens_region": {"type": "boolean"},
                                "governing_sentence_id": {
                                    # May point at any earlier sentence, not
                                    # only the ones judged in this chunk.
                                    "type": ["integer", "null"],
                                    "enum": [*all_ids, None],
                                },
                                "source_subtype": {
                                    "type": "string",
                                    "enum": [*SOURCE_SUBTYPES, ""],
                                },
                                "source_span_text": {"type": "string"},
                            },
                            "required": [
                                "opens_region",
                                "governing_sentence_id",
                            ],
                        },
                        "period_context": {
                            "type": "object",
                            "properties": {
                                "period_raw": {"type": "string"},
                                "source_span_text": {"type": "string"},
                            },
                        },
                    },
                    "required": [
                        "sentence_id",
                        "indicator_scopes",
                        "source_region",
                    ],
                },
            }
        },
        "required": ["sentences"],
    }


def resolve_prediction(
    article_text: str,
    prediction: dict[str, Any],
    *,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> dict[str, Any]:
    """Attach derived offsets and record spans the model invented."""
    sentences = {
        row["sentence_id"]: row["text"]
        for row in (sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator) if sentence_span_iterator is not None else sentence_offset_map(article_text))
    }
    resolved: list[dict[str, Any]] = []
    cross_source_promotions: list[tuple[int, dict[str, Any]]] = []
    unresolved = 0
    unresolved_span_details: list[dict[str, Any]] = []
    for item in prediction.get("sentences") or []:
        sentence_id = item.get("sentence_id")
        text = sentences.get(sentence_id, "")
        scopes = []
        for scope in item.get("indicator_scopes") or []:
            entry = {
                "indicator_label": scope.get("indicator_label") or "",
                "source_span_text": scope.get("source_span_text") or "",
            }
            try:
                span = resolve_hcx_model_span(text, entry["source_span_text"])
                _apply_hcx_span_resolution(entry, span)
                entry["span_status"] = "RESOLVED"
            except SpanResolutionError as exc:
                entry["span_status"] = "UNRESOLVED"
                entry["span_error"] = str(exc)
                unresolved += 1
                unresolved_span_details.append(
                    _unresolved_span_detail(
                        sentence_id,
                        "indicator_scope",
                        entry["source_span_text"],
                        exc,
                    )
                )
            scopes.append(entry)
        region = dict(item.get("source_region") or {})
        # Derive the category from the pointer so downstream code and the
        # evaluator keep one vocabulary regardless of how the model answered.
        governing = region.get("governing_sentence_id")
        if region.get("opens_region"):
            region["dominance"] = "정의"
            region.setdefault("governing_sentence_id", sentence_id)
        elif governing is None:
            region["dominance"] = DOMINANCE_NONE
        else:
            region["dominance"] = "상속"
        region_span = str(region.get("source_span_text") or "").strip()
        if region_span:
            try:
                span = resolve_hcx_model_span(text, region_span)
                _apply_hcx_span_resolution(region, span)
                region["span_status"] = "RESOLVED"
            except SpanResolutionError as exc:
                # Article leads often state the number first and attribute it
                # in the immediately following sentence.  HCX can therefore
                # copy an exact source phrase from another sentence.  The
                # frozen path remains strictly sentence-local; the opt-in
                # article-body path accepts only one exact cross-sentence hit.
                cross_sentence_matches = []
                if sentence_span_iterator is not None:
                    for candidate_id, candidate_text in sentences.items():
                        if candidate_id == sentence_id:
                            continue
                        try:
                            candidate_span = resolve_hcx_model_span(candidate_text, region_span)
                        except SpanResolutionError:
                            continue
                        cross_sentence_matches.append((candidate_id, candidate_span))
                if len(cross_sentence_matches) == 1:
                    source_sentence_id, span = cross_sentence_matches[0]
                    region["source_sentence_id"] = source_sentence_id
                    region["source_char_start"] = span["source_char_start"]
                    region["source_char_end"] = span["source_char_end"]
                    region["span_status"] = "RESOLVED"
                    region["opens_region"] = False
                    region["governing_sentence_id"] = source_sentence_id
                    region["dominance"] = "상속"
                    cross_source_promotions.append((source_sentence_id, dict(region)))
                else:
                    region["span_status"] = "UNRESOLVED"
                    region["span_error"] = str(exc)
                    unresolved += 1
                    unresolved_span_details.append(
                        _unresolved_span_detail(
                            sentence_id,
                            "source_region",
                            region_span,
                            exc,
                        )
                    )
        resolved.append({
            "sentence_id": sentence_id,
            "text": text,
            "indicator_scopes": scopes,
            "source_region": region,
            "period_context": item.get("period_context") or {},
        })
    # Break the common lead↔attribution pointer cycle by opening the uniquely
    # located source on the sentence that actually contains its exact span.
    # Conflicting promotions remain untouched and therefore fail closed later.
    promotions_by_sentence: dict[int, list[dict[str, Any]]] = {}
    for source_sentence_id, promoted_region in cross_source_promotions:
        promotions_by_sentence.setdefault(source_sentence_id, []).append(promoted_region)
    for source_sentence_id, promotions in promotions_by_sentence.items():
        distinct_surfaces = {
            str(region.get("source_span_text") or "") for region in promotions
        }
        if len(distinct_surfaces) != 1:
            continue
        target = next(
            (row for row in resolved if row.get("sentence_id") == source_sentence_id),
            None,
        )
        if target is None:
            continue
        existing = target.get("source_region") or {}
        if existing.get("span_status") == "RESOLVED" and existing.get("source_span_text"):
            continue
        opened = dict(promotions[0])
        opened["opens_region"] = True
        opened["governing_sentence_id"] = source_sentence_id
        opened["source_sentence_id"] = source_sentence_id
        opened["dominance"] = "정의"
        target["source_region"] = opened
    covered = {row["sentence_id"] for row in resolved}
    return {
        "sentences": resolved,
        "missing_sentence_ids": sorted(set(sentences) - covered),
        "unresolved_spans": unresolved,
        "unresolved_span_details": unresolved_span_details,
    }


def call_hcx_l2_segmentation(
    title: str,
    article_text: str,
    *,
    api_key: str,
    model: str = "HCX-007",
    timeout: int = 180,
    chunk_size: int = SENTENCE_CHUNK_SIZE,
    generation_config: dict[str, Any] | None = None,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    merged: list[dict[str, Any]] = []
    usage_total = {
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
    }
    latency_ms = 0.0
    chunks = (chunk_sentence_ids(article_text, chunk_size, sentence_span_iterator=sentence_span_iterator)
              if sentence_span_iterator is not None else chunk_sentence_ids(article_text, chunk_size))
    for target_ids in chunks:
        if not target_ids:
            continue
        prompt_kwargs = {
            "system_prompt": L2_SYSTEM_PROMPT,
            "user_prompt": (build_l2_prompt(title, article_text, target_ids, sentence_span_iterator=sentence_span_iterator)
                             if sentence_span_iterator is not None else build_l2_prompt(title, article_text, target_ids)),
            "schema": (build_l2_schema(article_text, target_ids, sentence_span_iterator=sentence_span_iterator)
                       if sentence_span_iterator is not None else build_l2_schema(article_text, target_ids)),
            "api_key": api_key,
            "model": model,
            "timeout": timeout,
        }
        prediction, usage, chunk_latency = _call_hcx_json(
            **prompt_kwargs,
            **(generation_config or {}),
        )
        merged.extend(prediction.get("sentences") or [])
        for key in usage_total:
            usage_total[key] += int(usage.get(key) or 0)
        latency_ms += chunk_latency
    return (
        (resolve_prediction(article_text, {"sentences": merged}, sentence_span_iterator=sentence_span_iterator)
         if sentence_span_iterator is not None else resolve_prediction(article_text, {"sentences": merged})),
        usage_total,
        latency_ms,
    )


def _split_schema(
    article_text: str,
    target_ids: list[int],
    *,
    pass_name: str,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> dict[str, Any]:
    """Schema for one half of the split contract."""
    sentences = sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator) if sentence_span_iterator is not None else sentence_offset_map(article_text)
    all_ids = [row["sentence_id"] for row in sentences]
    if pass_name == "source":
        properties: dict[str, Any] = {
            "sentence_id": {"type": "integer", "enum": target_ids},
            "source_region": {
                "type": "object",
                "properties": {
                    "opens_region": {"type": "boolean"},
                    "governing_sentence_id": {
                        "type": ["integer", "null"],
                        "enum": [*all_ids, None],
                    },
                    "source_subtype": {
                        "type": "string",
                        "enum": [*SOURCE_SUBTYPES, ""],
                    },
                    "source_span_text": {"type": "string"},
                },
                "required": ["opens_region", "governing_sentence_id"],
            },
        }
        required = ["sentence_id", "source_region"]
    else:
        properties = {
            "sentence_id": {"type": "integer", "enum": target_ids},
            "indicator_scopes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "indicator_label": {"type": "string"},
                        "source_span_text": {"type": "string"},
                    },
                    "required": ["indicator_label", "source_span_text"],
                },
            },
        }
        required = ["sentence_id", "indicator_scopes"]
    return {
        "type": "object",
        "properties": {
            "sentences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        },
        "required": ["sentences"],
    }


def call_hcx_l2_split(
    title: str,
    article_text: str,
    *,
    api_key: str,
    model: str = "HCX-007",
    timeout: int = 180,
    chunk_size: int = SENTENCE_CHUNK_SIZE,
    generation_config: dict[str, Any] | None = None,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Ask about source and indicator separately, then merge per sentence.

    Four single-call rounds showed the two questions competing: strengthening
    the source instructions left the indicator metric flat, and strengthening
    the indicator instructions cost source accuracy.  Splitting lets each half
    keep its own best contract.
    """
    merged: dict[int, dict[str, Any]] = {}
    usage_total = {
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
    }
    latency_ms = 0.0
    for pass_name, system_prompt, key in (
        ("source", L2_SOURCE_SYSTEM_PROMPT, "source_region"),
        ("indicator", L2_INDICATOR_SYSTEM_PROMPT, "indicator_scopes"),
    ):
        chunks = (chunk_sentence_ids(article_text, chunk_size, sentence_span_iterator=sentence_span_iterator)
                  if sentence_span_iterator is not None else chunk_sentence_ids(article_text, chunk_size))
        for target_ids in chunks:
            if not target_ids:
                continue
            prediction, usage, chunk_latency = _call_hcx_json(
                system_prompt=system_prompt,
                user_prompt=(build_l2_prompt(title, article_text, target_ids, sentence_span_iterator=sentence_span_iterator)
                             if sentence_span_iterator is not None else build_l2_prompt(title, article_text, target_ids)),
                schema=(_split_schema(article_text, target_ids, pass_name=pass_name, sentence_span_iterator=sentence_span_iterator)
                        if sentence_span_iterator is not None else _split_schema(article_text, target_ids, pass_name=pass_name)),
                api_key=api_key,
                model=model,
                timeout=timeout,
                **(generation_config or {}),
            )
            for usage_key in usage_total:
                usage_total[usage_key] += int(usage.get(usage_key) or 0)
            latency_ms += chunk_latency
            for item in prediction.get("sentences") or []:
                sentence_id = item.get("sentence_id")
                if sentence_id is None:
                    continue
                slot = merged.setdefault(
                    sentence_id,
                    {
                        "sentence_id": sentence_id,
                        "indicator_scopes": [],
                        "source_region": {},
                    },
                )
                if key in item:
                    slot[key] = item[key]
    return (
        (resolve_prediction(article_text, {"sentences": [merged[key] for key in sorted(merged)]}, sentence_span_iterator=sentence_span_iterator)
         if sentence_span_iterator is not None else resolve_prediction(article_text, {"sentences": [merged[key] for key in sorted(merged)]})),
        usage_total,
        latency_ms,
    )


def prediction_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
