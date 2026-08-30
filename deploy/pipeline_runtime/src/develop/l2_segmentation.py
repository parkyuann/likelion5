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
import hashlib
import re
from copy import deepcopy
from typing import Any, Iterator, Callable, Mapping, Sequence

try:
    from .hcx_client import call_hcx_json as _call_hcx_json
    from .l1_value_candidates import build_span_candidates, sentence_offset_map
    from .l2_span_resolver import SpanResolutionError, resolve_span
except ImportError:  # pragma: no cover - direct script execution
    from hcx_client import call_hcx_json as _call_hcx_json
    from l1_value_candidates import build_span_candidates, sentence_offset_map
    from l2_span_resolver import SpanResolutionError, resolve_span

try:
    from .r4c1_binding_proposer_v1 import (
        EXACT_INDICATOR_REGISTRY_VERSION,
        propose_exact_statistical_indicator_matches,
    )
except ImportError:  # pragma: no cover - packaged runtime mirror
    try:
        from ..news_verification.runtime.r4c1_binding_proposer_v1 import (
            EXACT_INDICATOR_REGISTRY_VERSION,
            propose_exact_statistical_indicator_matches,
        )
    except ImportError:  # pragma: no cover - direct script execution
        from r4c1_binding_proposer_v1 import (  # type: ignore
            EXACT_INDICATOR_REGISTRY_VERSION,
            propose_exact_statistical_indicator_matches,
        )


SentenceSpanIterator = Callable[[str], Iterator[tuple[int, int, int, str]]]

RAW_L2_CONTRACT_VERSION = "raw-hcx-l2-v1"
CANONICAL_L2_CONTRACT_VERSION = "canonical-l2-v1"
RESOLVER_VERSION = "exact-source-resolver-v1"
MISSING_SENTENCE_REPAIR_CONTRACT_VERSION = "l2-missing-sentence-exact-repair-v1"
INDICATOR_EVIDENCE_CONTRACT_VERSION = "l2-indicator-evidence-v1"
CANONICAL_L2_STATUSES = frozenset({
    "L2_READY", "REPAIRED_SOURCE_EXACT", "REPAIRED_SOURCE_NOT_PROVIDED", "HOLD_NOT_FOUND",
    "HOLD_AMBIGUOUS", "L2_CLARIFICATION_REQUIRED", "L2_UNAVAILABLE",
})
DOWNSTREAM_L2_ELIGIBLE = frozenset({
    "L2_READY", "REPAIRED_SOURCE_EXACT", "REPAIRED_SOURCE_NOT_PROVIDED",
    "L2_CLARIFICATION_REQUIRED",
})


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


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
MALFORMED_SOURCE_POINTER_REPAIR_CONTRACT_VERSION = "l2-malformed-source-pointer-v1"
SOURCE_CUE_REGISTRY_VERSION = 1

# These are deliberately bounded source-cue patterns, not an open-ended
# organization recognizer.  The suffix constraint prevents ordinary words
# such as "전국" from becoming a source.  The patterns only establish that a
# source cue is present; they never infer a source, table, or cell.
SOURCE_CUE_REGISTRY = (
    {
        "rule_id": "source-cue-relation-v1",
        "pattern": (
            r"[가-힣A-Za-z0-9·()]{2,40}(?:청|처|부|원|위원회|공단|공사|연구원|협회|은행)"
            r"(?:에\s*따르면|에\s*의하면)"
        ),
    },
    {
        "rule_id": "source-cue-reporting-v1",
        "pattern": (
            r"[가-힣A-Za-z0-9·()]{2,40}(?:청|처|부|원|위원회|공단|공사|연구원|협회|은행)"
            r"(?:은|는|이|가)\s*(?:\d{1,2}일\s*)?"
            r"(?:(?:['‘][^'’\n]{0,80}['’])\s*)?(?:에서\s*)?"
            r"(?:발표한|공표한|조사한|집계한|발표했다|공표했다|조사했다|집계했다|밝혔다)"
        ),
    },
)
SOURCE_CUE_REGISTRY_SHA256 = _canonical_sha({
    "version": SOURCE_CUE_REGISTRY_VERSION,
    "entries": SOURCE_CUE_REGISTRY,
})
_SPAN_ERROR_CODES = {
    "EMPTY",
    "NOT_FOUND",
    "AMBIGUOUS",
    "OCCURRENCE_INDEX_INVALID",
    "UNKNOWN",
}


def _exact_source_cue_matches(text: str) -> list[dict[str, Any]]:
    """Return only versioned, exact source-cue registry matches."""
    matches: list[dict[str, Any]] = []
    for entry in SOURCE_CUE_REGISTRY:
        for match in re.finditer(entry["pattern"], text):
            matches.append({
                "rule_id": entry["rule_id"],
                "source_span_text": match.group(0),
                "char_start": match.start(),
                "char_end": match.end(),
            })
    return sorted(matches, key=lambda item: (item["char_start"], item["char_end"], item["rule_id"]))


def _valid_governing_source_cue_matches(
    sentence_id: Any,
    region: dict[str, Any],
    sentences: dict[int, str],
) -> list[dict[str, Any]]:
    """Inspect the sentence and only its explicitly valid governing context."""
    current_text = sentences.get(sentence_id, "")
    matches = _exact_source_cue_matches(current_text)
    governing = region.get("governing_sentence_id")
    if isinstance(governing, int) and governing != sentence_id and governing < int(sentence_id):
        context_text = sentences.get(governing)
        if context_text is not None:
            for match in _exact_source_cue_matches(context_text):
                matches.append({**match, "context_sentence_id": governing})
    return matches


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


def _missing_sentence_repair(
    sentence_id: int,
    sentence_text: str,
    value_candidates: list[dict[str, Any]],
    sentence_char_start: int = 0,
) -> tuple[dict[str, Any] | None, str, str, dict[str, Any]]:
    """Attempt the bounded exact-only repair for one absent HCX sentence.

    The only authorities here are the article sentence, the existing L1
    value candidates, and the versioned exact terminology registry.  A
    missing match or any ambiguity remains a hold; nothing is inferred from
    retrieval, metadata, similarity, or another HCX call.
    """
    indicators = propose_exact_statistical_indicator_matches(sentence_text)
    values = [candidate for candidate in value_candidates if candidate.get("kind") == "value_unit"]
    # An omitted non-claim sentence is not an L2 repair target.  In
    # particular, source-only lead sentences may be intentionally absent
    # while their exact source span is promoted from a later sentence.
    if not indicators and not values:
        return None, "SKIP", "", {}
    receipt: dict[str, Any] = {
        "repair_contract_version": MISSING_SENTENCE_REPAIR_CONTRACT_VERSION,
        "repair_reason_code": "MISSING_SENTENCE_EXACT_INDICATOR",
        "sentence_id": int(sentence_id),
        "value_span_id": None,
        "value_span_text": None,
        "value_char_start": None,
        "value_char_end": None,
        "indicator_source_span_text": None,
        "indicator_char_start": None,
        "indicator_char_end": None,
        "indicator_source_char_start": None,
        "indicator_source_char_end": None,
        "value_source_char_start": None,
        "value_source_char_end": None,
        "terminology_registry_version": EXACT_INDICATOR_REGISTRY_VERSION,
        "terminology_rule_id": None,
        "candidate_count": len(indicators),
        "value_candidate_count": len(values),
        "raw_prediction_sha256": None,
        "canonical_l2_sha256": None,
    }
    if not indicators:
        return None, "HOLD_NOT_FOUND", "MISSING_SENTENCE_EXACT_INDICATOR_NOT_FOUND", receipt
    if len(indicators) != 1 or len(values) != 1:
        return None, "HOLD_AMBIGUOUS", "MISSING_SENTENCE_EXACT_INDICATOR_AMBIGUOUS", receipt

    indicator = indicators[0]
    value = values[0]
    value_start = int(value.get("char_start") or 0)
    value_end = int(value.get("char_end") or 0)
    value_local_start = value_start - int(sentence_char_start)
    indicator_start = int(indicator.start)
    indicator_end = int(indicator.end)
    receipt.update({
        "value_span_id": value.get("span_id"),
        "value_span_text": value.get("text"),
        "value_char_start": value_start,
        "value_char_end": value_end,
        "indicator_source_span_text": indicator.text,
        "indicator_char_start": int(sentence_char_start) + indicator_start,
        "indicator_char_end": int(sentence_char_start) + indicator_end,
        "indicator_source_char_start": int(sentence_char_start) + indicator_start,
        "indicator_source_char_end": int(sentence_char_start) + indicator_end,
        "value_source_char_start": value_start,
        "value_source_char_end": value_end,
        "terminology_rule_id": indicator.rule_id,
    })
    if indicator_end > value_local_start:
        return None, "HOLD_AMBIGUOUS", "MISSING_SENTENCE_EXACT_INDICATOR_AMBIGUOUS", receipt

    period_candidates = [candidate for candidate in value_candidates if candidate.get("kind") == "time"]
    period_context: dict[str, Any] = {}
    if len(period_candidates) == 1:
        period = period_candidates[0]
        period_text = str(period.get("text") or "")
        period_context = {
            "period_raw": period_text,
            "source_span_text": period_text,
            "source_char_start": int(period.get("char_start") or 0),
            "source_char_end": int(period.get("char_end") or 0),
            "span_status": "RESOLVED",
            "offset_provenance": "L1_EXACT_TIME_CANDIDATE",
        }
    row = {
        "sentence_id": int(sentence_id),
        "text": sentence_text,
        "indicator_scopes": [{
            "indicator_label": indicator.text,
            "source_span_text": indicator.text,
            "source_char_start": indicator_start,
            "source_char_end": indicator_end,
            "occurrence_index": 0,
            "match_count": 1,
            "offset_provenance": "L2_MISSING_SENTENCE_EXACT_REGISTRY",
            "span_status": "RESOLVED",
        }],
        "source_region": {
            "opens_region": False,
            "governing_sentence_id": None,
            "dominance": DOMINANCE_NONE,
            "span_status": "NOT_PROVIDED",
        },
        "period_context": period_context,
    }
    return row, "REPAIRED_SOURCE_EXACT", "MISSING_SENTENCE_EXACT_INDICATOR", receipt


def _normalize_malformed_source_pointer(
    sentence_id: int,
    sentence_text: str,
    region: dict[str, Any],
    indicator_scopes: list[dict[str, Any]],
    owned_values: list[dict[str, Any]],
    sentences: dict[int, str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Downgrade one bounded HCX structural pointer to no source evidence.

    This is intentionally narrower than normal span recovery.  A bracketed
    model pointer is discarded only when the article itself proves one exact
    indicator and one L1 value, and neither the sentence nor its explicitly
    governing context contains a registered source cue.  No retrieval,
    metadata, similarity, reverse inference, or additional HCX call is used.
    """
    original_pointer = str(region.get("source_span_text") or "")
    pointer = original_pointer.strip()
    if not region.get("opens_region") or not re.fullmatch(r"\[\d+\]", pointer):
        return None

    governing = region.get("governing_sentence_id")
    governing_is_self = (
        isinstance(governing, int)
        and not isinstance(governing, bool)
        and governing == sentence_id
    )
    exact_source_match_count = len(_occurrences(sentence_text, pointer))
    source_cue_matches = _valid_governing_source_cue_matches(sentence_id, region, sentences)
    exact_indicator_scopes = [
        scope for scope in indicator_scopes if scope.get("span_status") == "RESOLVED"
    ]
    exact_indicator_count = len(exact_indicator_scopes)
    owned_l1_value_count = len(owned_values)
    ownership_conflict = (
        len(indicator_scopes) != 1
        or exact_indicator_count != 1
        or any(scope.get("span_status") != "RESOLVED" for scope in indicator_scopes)
        or owned_l1_value_count != 1
        or len({str(value.get("span_id") or "") for value in owned_values}) != owned_l1_value_count
    )
    if (
        exact_source_match_count != 0
        or bool(str(region.get("source_subtype") or "").strip())
        or not governing_is_self
        or bool(source_cue_matches)
        or exact_indicator_count != 1
        or owned_l1_value_count != 1
        or ownership_conflict
    ):
        return None

    normalized = dict(region)
    for key in (
        "source_char_start", "source_char_end", "source_sentence_id",
        "model_source_span_text", "span_match_mode", "offset_provenance",
        "span_error",
    ):
        normalized.pop(key, None)
    normalized.update({
        "opens_region": False,
        "governing_sentence_id": None,
        "source_subtype": "",
        "source_span_text": "",
        "span_status": "NOT_PROVIDED",
        "dominance": DOMINANCE_NONE,
    })
    receipt = {
        "repair_contract_version": MALFORMED_SOURCE_POINTER_REPAIR_CONTRACT_VERSION,
        "repair_action": "NORMALIZE_MODEL_POINTER_TO_NOT_PROVIDED",
        "reason_code": "MALFORMED_SOURCE_POINTER_WITHOUT_EXACT_EVIDENCE",
        "sentence_id": int(sentence_id),
        "original_source_span_text": original_pointer,
        "pointer_artifact_class": "BRACKETED_INTEGER",
        "exact_source_span_match_count": exact_source_match_count,
        "exact_source_cue_match_count": len(source_cue_matches),
        "exact_indicator_count": exact_indicator_count,
        "owned_l1_value_count": owned_l1_value_count,
        "ownership_conflict": ownership_conflict,
        "source_cue_registry_version": SOURCE_CUE_REGISTRY_VERSION,
        "source_cue_registry_sha256": SOURCE_CUE_REGISTRY_SHA256,
    }
    return normalized, receipt


class HcxSpanResolutionError(SpanResolutionError):
    """Bounded failure from the HCX model-span normalization layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def _indicator_evidence_decision(
    indicator_label: Any,
    resolved_source_span: Any,
    sentence_text: Any,
) -> tuple[str, dict[str, Any]]:
    """Accept a model label only when it has exact article evidence.

    This is deliberately a character-for-character check.  The registry
    proposal is also exact and versioned; it is not a normalization,
    similarity, metadata, retrieval, or retry fallback.
    """
    label = str(indicator_label or "")
    source = str(resolved_source_span or "")
    sentence = str(sentence_text or "")
    source_count = len(_occurrences(source, label)) if label else 0
    sentence_count = len(_occurrences(sentence, label)) if label else 0
    registry_matches = [
        proposal for proposal in propose_exact_statistical_indicator_matches(sentence)
        if proposal.text == label
    ] if label else []
    exact_count = source_count or sentence_count
    if source_count == 1 or sentence_count == 1 or len(registry_matches) == 1:
        decision = "RESOLVED"
        reason = "EXACT_LABEL_IN_SOURCE_OR_SENTENCE"
    elif exact_count > 1 or len(registry_matches) > 1:
        decision = "AMBIGUOUS"
        reason = "INDICATOR_LABEL_EXACT_MATCH_AMBIGUOUS"
    else:
        decision = "MISSING"
        reason = "MODEL_INDICATOR_LABEL_NOT_GROUNDED"
    return decision, {
        "contract_version": INDICATOR_EVIDENCE_CONTRACT_VERSION,
        "decision": decision,
        "reason_code": reason,
        "indicator_label": label,
        "exact_label_in_source_span_count": source_count,
        "exact_label_in_sentence_count": sentence_count,
        "exact_registry_match_count": len(registry_matches),
        "terminology_registry_version": EXACT_INDICATOR_REGISTRY_VERSION,
        "owned_l1_value_count": None,
        "period_context_preserved": None,
    }


def _recover_single_exact_indicator(
    sentence_text: str,
    sentence_values: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Recover one omitted indicator from a uniquely exact source surface.

    HCX may return an empty ``indicator_scopes`` array even when the sentence
    contains one ordinary statistical indicator. This recovery is deliberately
    narrower than semantic inference: it requires exactly one L1 value, exactly
    one entry in the versioned terminology registry, and the indicator must
    precede that value in the same sentence. It never selects an item, table,
    dimension, period, or cell.
    """
    if not isinstance(sentence_text, str) or len(sentence_values) != 1:
        return None
    proposals = propose_exact_statistical_indicator_matches(sentence_text)
    if len(proposals) != 1:
        return None
    proposal = proposals[0]
    value_start = sentence_values[0].get("char_start")
    if not isinstance(value_start, int) or proposal.end > value_start:
        return None
    return {
        "indicator_label": proposal.text,
        "source_span_text": proposal.text,
        "source_char_start": proposal.start,
        "source_char_end": proposal.end,
        "span_status": "RESOLVED",
        "indicator_evidence_status": "RESOLVED",
        "indicator_evidence_reason": "EXACT_REGISTRY_SOURCE_RECOVERY",
        "offset_provenance": "L2_EXACT_INDICATOR_REGISTRY_RECOVERY",
        "terminology_registry_version": EXACT_INDICATOR_REGISTRY_VERSION,
        "recovery_rule_id": "l2-single-exact-indicator-recovery-v1",
    }


def _model_indicator_label_is_compatible(
    sentence_text: str,
    model_indicator_label: str,
) -> bool:
    """Allow only an exact registry label with article-grounded context."""
    proposals = propose_exact_statistical_indicator_matches(sentence_text)
    label = str(model_indicator_label or "").strip()
    if len(proposals) != 1 or not label or proposals[0].text not in label:
        return False
    residual = label.replace(proposals[0].text, "")
    allowed = [
        proposals[0].text,
        *(
            str(candidate.get("text") or "")
            for candidate in build_span_candidates(sentence_text)
            if candidate.get("kind") in {"dimension", "time"}
        ),
    ]
    for surface in sorted({surface for surface in allowed if surface}, key=len, reverse=True):
        residual = residual.replace(surface, "")
    residual = re.sub(r"[\s\W_]+", "", residual)
    residual = re.sub(r"(?:의|은|는|이|가|을|를|에|에서|별|기준)+$", "", residual)
    return not residual


def find_hcx_model_span_matches(sentence_text: str, span_text: str) -> list[int]:
    """Return offsets for exact source text only."""
    _validate_hcx_model_span_inputs(sentence_text, span_text)
    return _occurrences(sentence_text, span_text)


def resolve_hcx_model_span(sentence_text: str, span_text: str) -> dict[str, Any]:
    """Resolve an HCX source span using an exact character substring only."""
    resolved = resolve_span(sentence_text, span_text)
    resolved["model_source_span_text"] = span_text
    resolved["span_match_mode"] = "EXACT"
    resolved["offset_provenance"] = "DERIVED_FROM_MODEL_SPAN_EXACT"
    return resolved


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
    """Create a raw envelope and an exact-source canonical L2 envelope."""
    raw_prediction = deepcopy(prediction)
    raw_prediction_sha256 = _canonical_sha(raw_prediction)
    sentence_rows = (
        sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator)
        if sentence_span_iterator is not None else sentence_offset_map(article_text)
    )
    sentences = {row["sentence_id"]: row["text"] for row in sentence_rows}
    sentence_starts = {row["sentence_id"]: int(row.get("char_start") or 0) for row in sentence_rows}
    candidates = (
        build_span_candidates(article_text, sentence_span_iterator=sentence_span_iterator)
        if sentence_span_iterator is not None else build_span_candidates(article_text)
    )
    candidates_by_sentence: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        sentence_key = candidate.get("sentence_id")
        if isinstance(sentence_key, int):
            candidates_by_sentence.setdefault(sentence_key, []).append(candidate)
    resolved: list[dict[str, Any]] = []
    cross_source_promotions: list[tuple[int, dict[str, Any]]] = []
    unresolved = 0
    unresolved_span_details: list[dict[str, Any]] = []
    repaired_source_count = 0
    exact_repair_receipts: list[dict[str, Any]] = []
    exact_repair_statuses: list[str] = []
    malformed_source_pointer_receipts: list[dict[str, Any]] = []
    source_not_provided_receipts: list[dict[str, Any]] = []
    indicator_evidence_receipts: list[dict[str, Any]] = []
    indicator_clarification = False
    for item in prediction.get("sentences") or []:
        sentence_id = item.get("sentence_id")
        text = sentences.get(sentence_id, "")
        scopes = []
        sentence_indicator_clarification = False
        model_indicator_labels: list[str] = []
        for scope in item.get("indicator_scopes") or []:
            model_indicator_labels.append(str(scope.get("indicator_label") or ""))
            entry = {
                "indicator_label": scope.get("indicator_label") or "",
                "source_span_text": scope.get("source_span_text") or "",
            }
            try:
                span = resolve_hcx_model_span(text, entry["source_span_text"])
                _apply_hcx_span_resolution(entry, span)
                entry["span_status"] = "RESOLVED"
                evidence_decision, evidence_receipt = _indicator_evidence_decision(
                    entry["indicator_label"], entry.get("source_span_text"), text,
                )
                evidence_receipt.update({
                    "sentence_id": int(sentence_id),
                    "value_span_ids": [
                        value.get("span_id")
                        for value in candidates_by_sentence.get(int(sentence_id), [])
                        if value.get("kind") == "value_unit"
                    ],
                    "period_context_preserved": bool(
                        item.get("period_context")
                        or any(value.get("kind") == "time" for value in candidates_by_sentence.get(int(sentence_id), []))
                    ),
                })
                if evidence_decision != "RESOLVED":
                    # Keep the raw model label in raw_envelope only.  The
                    # canonical row must not turn an invented label into a
                    # retrieval field or permit indicator inheritance.
                    entry["indicator_label"] = ""
                    entry["indicator_evidence_status"] = evidence_decision
                    entry["indicator_evidence_reason"] = evidence_receipt["reason_code"]
                    sentence_indicator_clarification = True
                    indicator_evidence_receipts.append(evidence_receipt)
                else:
                    entry["indicator_evidence_status"] = "RESOLVED"
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
        sentence_values = [
            value for value in candidates_by_sentence.get(int(sentence_id), [])
            if value.get("kind") == "value_unit"
        ]
        if sentence_values and not scopes:
            # Do not let exact indicator recovery mask an invalid HCX source
            # pointer.  That pointer is an independent hard-failure contract
            # and must remain visible to the canonical L2 status.
            raw_region = dict(item.get("source_region") or {})
            raw_region_span = str(raw_region.get("source_span_text") or "").strip()
            source_pointer_is_valid = True
            if raw_region_span:
                try:
                    resolve_hcx_model_span(text, raw_region_span)
                except SpanResolutionError:
                    source_pointer_is_valid = False
            recovered_indicator = (
                _recover_single_exact_indicator(text, sentence_values)
                if source_pointer_is_valid else None
            )
            if recovered_indicator is not None:
                scopes = [recovered_indicator]
                proposal = propose_exact_statistical_indicator_matches(text)[0]
                indicator_evidence_receipts.append({
                    "contract_version": INDICATOR_EVIDENCE_CONTRACT_VERSION,
                    "decision": "RESOLVED",
                    "reason_code": "EXACT_REGISTRY_SOURCE_RECOVERY",
                    "indicator_label": proposal.text,
                    "exact_label_in_source_span_count": 1,
                    "exact_label_in_sentence_count": 1,
                    "exact_registry_match_count": 1,
                    "terminology_registry_version": EXACT_INDICATOR_REGISTRY_VERSION,
                    "owned_l1_value_count": len(sentence_values),
                    "period_context_preserved": bool(
                        item.get("period_context")
                        or any(value.get("kind") == "time" for value in candidates_by_sentence.get(int(sentence_id), []))
                    ),
                    "sentence_id": int(sentence_id),
                    "value_span_ids": [value.get("span_id") for value in sentence_values],
                    "recovery_rule_id": "l2-single-exact-indicator-recovery-v1",
                })
            else:
                sentence_indicator_clarification = True
                indicator_evidence_receipts.append({
                    "contract_version": INDICATOR_EVIDENCE_CONTRACT_VERSION,
                    "decision": "MISSING",
                    "reason_code": "MODEL_INDICATOR_LABEL_NOT_GROUNDED",
                    "indicator_label": "",
                    "exact_label_in_source_span_count": 0,
                    "exact_label_in_sentence_count": 0,
                    "exact_registry_match_count": 0,
                    "terminology_registry_version": EXACT_INDICATOR_REGISTRY_VERSION,
                    "owned_l1_value_count": len(sentence_values),
                    "period_context_preserved": bool(
                        item.get("period_context")
                        or any(value.get("kind") == "time" for value in candidates_by_sentence.get(int(sentence_id), []))
                    ),
                    "sentence_id": int(sentence_id),
                    "value_span_ids": [value.get("span_id") for value in sentence_values],
                })
        if sentence_values and scopes and sentence_indicator_clarification:
            # HCX sometimes returns one exact but overly broad source span and
            # leaves indicator_label empty.  Treat that as malformed model
            # evidence, then apply the same narrow registry recovery used for
            # an omitted scope.  Multiple scopes or unresolved spans remain a
            # clarification/hold and are never guessed.
            raw_region = dict(item.get("source_region") or {})
            raw_region_span = str(raw_region.get("source_span_text") or "").strip()
            source_pointer_is_valid = True
            if raw_region_span:
                try:
                    resolve_hcx_model_span(text, raw_region_span)
                except SpanResolutionError:
                    source_pointer_is_valid = False
            if (
                source_pointer_is_valid
                and len(scopes) == 1
                and scopes[0].get("span_status") == "RESOLVED"
                and len(model_indicator_labels) == 1
                and (
                    not model_indicator_labels[0].strip()
                    or _model_indicator_label_is_compatible(
                        text, model_indicator_labels[0],
                    )
                )
            ):
                recovered_indicator = _recover_single_exact_indicator(text, sentence_values)
                if recovered_indicator is not None:
                    indicator_evidence_receipts = [
                        receipt for receipt in indicator_evidence_receipts
                        if not (
                            receipt.get("sentence_id") == int(sentence_id)
                            and receipt.get("decision") in {"MISSING", "AMBIGUOUS"}
                        )
                    ]
                    scopes = [recovered_indicator]
                    proposal = propose_exact_statistical_indicator_matches(text)[0]
                    indicator_evidence_receipts.append({
                        "contract_version": INDICATOR_EVIDENCE_CONTRACT_VERSION,
                        "decision": "RESOLVED",
                        "reason_code": "EXACT_REGISTRY_SOURCE_RECOVERY",
                        "indicator_label": proposal.text,
                        "exact_label_in_source_span_count": 1,
                        "exact_label_in_sentence_count": 1,
                        "exact_registry_match_count": 1,
                        "terminology_registry_version": EXACT_INDICATOR_REGISTRY_VERSION,
                        "owned_l1_value_count": len(sentence_values),
                        "period_context_preserved": bool(
                            item.get("period_context")
                            or any(value.get("kind") == "time" for value in candidates_by_sentence.get(int(sentence_id), []))
                        ),
                        "sentence_id": int(sentence_id),
                        "value_span_ids": [value.get("span_id") for value in sentence_values],
                        "recovery_rule_id": "l2-single-exact-indicator-recovery-v1",
                        "recovered_from_model_scope": True,
                    })
                    sentence_indicator_clarification = False
        indicator_clarification = indicator_clarification or sentence_indicator_clarification
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
        if region.get("opens_region") and not region_span:
            original_region = dict(region)
            for key in (
                "source_char_start", "source_char_end", "source_sentence_id",
                "model_source_span_text", "span_match_mode", "offset_provenance",
                "span_error",
            ):
                region.pop(key, None)
            region.update({
                "opens_region": False,
                "governing_sentence_id": None,
                "source_subtype": "",
                "source_span_text": "",
                "span_status": "NOT_PROVIDED",
                "dominance": DOMINANCE_NONE,
            })
            source_not_provided_receipts.append({
                "repair_contract_version": MALFORMED_SOURCE_POINTER_REPAIR_CONTRACT_VERSION,
                "repair_action": "NORMALIZE_EMPTY_SOURCE_REGION_TO_NOT_PROVIDED",
                "reason_code": "SOURCE_REGION_OPEN_WITHOUT_EXACT_SPAN",
                "sentence_id": int(sentence_id),
                "original_opens_region": bool(original_region.get("opens_region")),
                "original_source_subtype": str(original_region.get("source_subtype") or ""),
                "original_source_span_text": str(original_region.get("source_span_text") or ""),
                "exact_source_span_match_count": 0,
            })
            region_span = ""
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
                    # A model-provided ownership pointer is a constraint, not
                    # a hint.  Never search another sentence merely because
                    # it contains the same exact source surface.
                    candidate_ids = (
                        (int(governing),)
                        if isinstance(governing, int) and governing != sentence_id
                        else tuple(sentences)
                    )
                    for candidate_id in candidate_ids:
                        candidate_text = sentences.get(candidate_id)
                        if candidate_text is None:
                            continue
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
                    repaired_source_count += 1
                else:
                    malformed_repair = _normalize_malformed_source_pointer(
                        int(sentence_id),
                        text,
                        region,
                        scopes,
                        [
                            candidate for candidate in candidates_by_sentence.get(int(sentence_id), [])
                            if candidate.get("kind") == "value_unit"
                        ],
                        sentences,
                    )
                    if malformed_repair is not None:
                        region, receipt = malformed_repair
                        malformed_source_pointer_receipts.append(receipt)
                        resolved.append({
                            "sentence_id": sentence_id,
                            "text": text,
                            "indicator_scopes": scopes,
                            "source_region": region,
                            "period_context": item.get("period_context") or {},
                        })
                        continue
                    diagnostic = (
                        HcxSpanResolutionError("AMBIGUOUS", "exact source span ownership is ambiguous")
                        if len(cross_sentence_matches) > 1
                        else exc
                    )
                    region["span_status"] = "UNRESOLVED"
                    region["span_error"] = str(diagnostic)
                    unresolved += 1
                    unresolved_span_details.append(
                        _unresolved_span_detail(
                            sentence_id,
                            "source_region",
                            region_span,
                            diagnostic,
                        )
                    )
        resolved.append({
            "sentence_id": sentence_id,
            "text": text,
            "indicator_scopes": scopes,
            "source_region": region,
            "period_context": item.get("period_context") or {},
            "field_states": ({
                "indicator": {
                    "contract_version": INDICATOR_EVIDENCE_CONTRACT_VERSION,
                    "state": "AMBIGUOUS" if any(
                        receipt.get("decision") == "AMBIGUOUS"
                        and receipt.get("sentence_id") == int(sentence_id)
                        for receipt in indicator_evidence_receipts
                    ) else "MISSING",
                    "reason_code": "INDICATOR_EVIDENCE_AMBIGUOUS" if any(
                        receipt.get("decision") == "AMBIGUOUS"
                        and receipt.get("sentence_id") == int(sentence_id)
                        for receipt in indicator_evidence_receipts
                    ) else "INDICATOR_EVIDENCE_MISSING",
                    "value_span_ids": [value.get("span_id") for value in sentence_values],
                    "period_preserved": bool(
                        item.get("period_context")
                        or any(value.get("kind") == "time" for value in candidates_by_sentence.get(int(sentence_id), []))
                    ),
                }
            } if any(
                receipt.get("sentence_id") == int(sentence_id)
                and receipt.get("decision") in {"MISSING", "AMBIGUOUS"}
                for receipt in indicator_evidence_receipts
            ) else {}),
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
    missing_sentence_ids = sorted(set(sentences) - covered)
    if missing_sentence_ids:
        for missing_sentence_id in list(missing_sentence_ids):
            repaired_row, repair_status, repair_reason, repair_receipt = _missing_sentence_repair(
                missing_sentence_id,
                sentences[missing_sentence_id],
                candidates_by_sentence.get(missing_sentence_id, []),
                sentence_starts.get(missing_sentence_id, 0),
            )
            if repair_status == "SKIP":
                continue
            repair_receipt["raw_prediction_sha256"] = raw_prediction_sha256
            exact_repair_receipts.append(repair_receipt)
            exact_repair_statuses.append(repair_status)
            if repaired_row is not None:
                resolved.append(repaired_row)
        covered = {row["sentence_id"] for row in resolved}
        missing_sentence_ids = sorted(set(sentences) - covered)
    error_codes = {str(item.get("span_error_code") or "UNKNOWN") for item in unresolved_span_details}
    if "AMBIGUOUS" in error_codes:
        canonical_status = "HOLD_AMBIGUOUS"
        reason_code = "SOURCE_EXACT_AMBIGUOUS"
    elif unresolved_span_details:
        canonical_status = "HOLD_NOT_FOUND"
        reason_code = "SOURCE_EXACT_NOT_FOUND"
    # A missing, non-repairable sentence must not newly suppress another
    # independently resolved sentence.  Before this repair path existed,
    # absent HCX rows did not change the article-level status by themselves.
    elif "HOLD_AMBIGUOUS" in exact_repair_statuses and not resolved:
        canonical_status = "HOLD_AMBIGUOUS"
        reason_code = "MISSING_SENTENCE_EXACT_INDICATOR_AMBIGUOUS"
    elif "HOLD_NOT_FOUND" in exact_repair_statuses and not resolved:
        canonical_status = "HOLD_NOT_FOUND"
        reason_code = "MISSING_SENTENCE_EXACT_INDICATOR_NOT_FOUND"
    elif indicator_clarification and (malformed_source_pointer_receipts or source_not_provided_receipts):
        # A malformed source pointer is a hard L2 hold even when the same row
        # also lacks an indicator.  Do not downgrade the malformed-pointer
        # evidence into a clarification-only result.
        canonical_status = "HOLD_NOT_FOUND"
        reason_code = (
            "MALFORMED_SOURCE_POINTER_WITHOUT_EXACT_EVIDENCE"
            if malformed_source_pointer_receipts
            else "SOURCE_REGION_OPEN_WITHOUT_EXACT_SPAN"
        )
    elif indicator_clarification:
        canonical_status = "L2_CLARIFICATION_REQUIRED"
        reason_code = "INDICATOR_EVIDENCE_MISSING"
    elif malformed_source_pointer_receipts or source_not_provided_receipts:
        canonical_status = "REPAIRED_SOURCE_NOT_PROVIDED"
        reason_code = (
            "MALFORMED_SOURCE_POINTER_WITHOUT_EXACT_EVIDENCE"
            if malformed_source_pointer_receipts
            else "SOURCE_REGION_OPEN_WITHOUT_EXACT_SPAN"
        )
    elif "REPAIRED_SOURCE_EXACT" in exact_repair_statuses:
        canonical_status = "REPAIRED_SOURCE_EXACT"
        reason_code = "MISSING_SENTENCE_EXACT_INDICATOR"
    elif repaired_source_count:
        canonical_status = "REPAIRED_SOURCE_EXACT"
        reason_code = "SOURCE_EXACT_CROSS_SENTENCE"
    else:
        canonical_status = "L2_READY"
        reason_code = None
    canonical_payload = {
        "contract_version": CANONICAL_L2_CONTRACT_VERSION,
        "status": canonical_status,
        "reason_code": reason_code,
        "resolver_version": RESOLVER_VERSION,
        "sentences": resolved,
        "missing_sentence_ids": missing_sentence_ids,
        "unresolved_span_details": unresolved_span_details,
    }
    canonical_l2_sha256 = _canonical_sha(canonical_payload)
    for receipt in exact_repair_receipts:
        receipt["canonical_l2_sha256"] = canonical_l2_sha256
    all_repair_receipts = [
        *exact_repair_receipts,
        *malformed_source_pointer_receipts,
        *source_not_provided_receipts,
    ]
    canonicalization_receipt_payload = {
        "contract_version": CANONICAL_L2_CONTRACT_VERSION,
        "raw_prediction_sha256": raw_prediction_sha256,
        "canonical_l2_sha256": canonical_l2_sha256,
        "resolver_version": RESOLVER_VERSION,
        "source_cue_registry_version": SOURCE_CUE_REGISTRY_VERSION,
        "source_cue_registry_sha256": SOURCE_CUE_REGISTRY_SHA256,
        "repair_receipts": [
            {
                key: value
                for key, value in receipt.items()
                if key not in {"canonical_l2_sha256", "canonicalization_receipt_sha256"}
            }
            for receipt in all_repair_receipts
        ],
        "unresolved_span_details": unresolved_span_details,
    }
    canonicalization_receipt_sha256 = _canonical_sha(canonicalization_receipt_payload)
    for receipt in all_repair_receipts:
        receipt["canonical_l2_sha256"] = canonical_l2_sha256
        receipt["canonicalization_receipt_sha256"] = canonicalization_receipt_sha256
    return {
        "sentences": resolved,
        "missing_sentence_ids": missing_sentence_ids,
        "unresolved_spans": unresolved,
        "unresolved_span_details": unresolved_span_details,
        "raw_envelope": {
            "contract_version": RAW_L2_CONTRACT_VERSION,
            "model": None,
            "raw_prediction": raw_prediction,
            "raw_prediction_sha256": raw_prediction_sha256,
            "transport_status": "OK",
        },
        "canonical_status": canonical_status,
        "canonical_reason_code": reason_code,
        "resolver_version": RESOLVER_VERSION,
        "repair_reason_code": reason_code,
        "repair_receipts": all_repair_receipts,
        "indicator_evidence_receipts": indicator_evidence_receipts,
        "canonical_l2_sha256": canonical_l2_sha256,
        "canonicalization_receipt_sha256": canonicalization_receipt_sha256,
    }


def call_hcx_l2_segmentation(
    title: str,
    article_text: str,
    *,
    api_key: str,
    model: str = "HCX-007",
    timeout: int | None = None,
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
