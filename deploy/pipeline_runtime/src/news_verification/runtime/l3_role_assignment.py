"""L3 — assign a role to every value by reading the L2 layout.

팀 인계: L1의 각 수치에 그 수치를 지배하는 L2 지표·출처·기간 문맥을 연결한다.
모델 호출 없이 결정론적으로 동작한다.

r11~r16i derived the six retrieval fields independently for each claim.  The
individual fields reached 0.12~0.61 while all six together reached 0.009,
because independent derivation makes the errors independent too.

This layer does the opposite: the article's layout is already fixed by L2, so
each value inherits its indicator, source and period from the layout and only
overrides them when its own sentence states something more specific.  Errors
then correlate — a wrong region label costs the same values together instead
of each field failing separately — which is also what makes them fixable in
one place.

No model calls happen here.  Everything is a deterministic walk over L2 output.
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Callable

try:
    from .indicator_text import measure_tail, names_a_subject
    from .l1_value_candidates import build_span_candidates, sentence_offset_map
    from .period_text import extract_sentence_period_span
except ImportError:  # pragma: no cover - direct script execution
    from indicator_text import measure_tail, names_a_subject
    from l1_value_candidates import build_span_candidates, sentence_offset_map
    from period_text import extract_sentence_period_span


NO_REGION = "지배 없음"

def indicator_is_fragment(label: object) -> bool:
    """True when a label names the measure but not what is being measured.

    ``성장률``, ``1분기 성장률`` and ``원화 기준 증가율`` are all unusable as retrieval
    strings: the article stated the subject earlier and the sentence carries
    only the measure.  Detecting this is what lets inheritance run on a value
    that already has a local label — without the test, a local fragment
    silently beats a complete inherited indicator, which is what happened in
    the 2026-08-03 retrieval probe.
    """
    if not str(label or "").strip():
        return False
    return not names_a_subject(label)


SentenceSpanIterator = Callable[[str], Iterator[tuple[int, int, int, str]]]


def _sentence_values(
    article_text: str,
    *,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Group value candidates by sentence with sentence-relative offsets.

    ``build_span_candidates`` reports ``char_start``/``char_end`` against the
    whole article while L2 spans are resolved against a single sentence, so the
    two are rebased here.  Comparing them directly silently compares different
    coordinate systems.
    """
    origins = {
        row["sentence_id"]: row["char_start"]
        for row in (sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator)
                    if sentence_span_iterator is not None else sentence_offset_map(article_text))
    }
    grouped: dict[int, list[dict[str, Any]]] = {}
    candidates = (build_span_candidates(article_text, sentence_span_iterator=sentence_span_iterator)
                  if sentence_span_iterator is not None else build_span_candidates(article_text))
    for candidate in candidates:
        if candidate.get("kind") != "value_unit":
            continue
        sentence_id = candidate["sentence_id"]
        origin = origins.get(sentence_id, 0)
        row = dict(candidate)
        for source, target in (
            ("char_start", "sentence_char_start"),
            ("char_end", "sentence_char_end"),
        ):
            offset = candidate.get(source)
            row[target] = offset - origin if isinstance(offset, int) else None
        grouped.setdefault(sentence_id, []).append(row)
    for values in grouped.values():
        values.sort(key=lambda row: row.get("sentence_char_start") or 0)
    return grouped


def resolve_region_chain(
    layout: dict[int, dict[str, Any]],
    sentence_id: int,
    *,
    max_hops: int = 50,
) -> dict[str, Any] | None:
    """Follow ``governing_sentence_id`` to the sentence that opened the region.

    A cycle or a dangling pointer resolves to nothing rather than looping,
    because a fabricated pointer must not become an inherited label.
    """
    seen: set[int] = set()
    current = sentence_id
    for _ in range(max_hops):
        if current in seen or current not in layout:
            return None
        seen.add(current)
        region = layout[current].get("source_region") or {}
        if region.get("opens_region"):
            return {
                "region_sentence_id": current,
                "source_subtype": region.get("source_subtype") or "",
                "source_span_text": region.get("source_span_text") or "",
            }
        governing = region.get("governing_sentence_id")
        if governing is None or governing == current:
            return None
        current = governing
    return None


def _inherited_indicator(
    layout: dict[int, dict[str, Any]],
    sentence_id: int,
    region_sentence_id: int | None,
) -> dict[str, Any] | None:
    """Walk backwards for the nearest indicator inside the same region.

    Korean articles state the indicator once and then report values without
    repeating it, so the nearest preceding scope is the intended one.  The
    walk stops at the region boundary: inheriting across a source change would
    attach a figure to an indicator that belongs to a different publisher.
    """
    for candidate_id in range(sentence_id - 1, -1, -1):
        if candidate_id not in layout:
            continue
        if region_sentence_id is not None and candidate_id < region_sentence_id:
            return None
        scopes = layout[candidate_id].get("indicator_scopes") or []
        usable = [
            scope for scope in scopes
            if str(scope.get("indicator_label") or "").strip()
        ]
        if usable:
            return {
                "indicator_label": usable[-1]["indicator_label"],
                "indicator_source": "INHERITED",
                "indicator_sentence_id": candidate_id,
            }
    return None


def _gap(span_start: int, span_end: int, value_start: int, value_end: int) -> int:
    """Character gap between two ranges; 0 when they overlap."""
    if span_end <= value_start:
        return value_start - span_end
    if value_end <= span_start:
        return span_start - value_end
    return 0


def _nearest_scope(
    scopes: list[dict[str, Any]],
    value_start: object,
    value_end: object,
    sentence_text: object = None,
) -> tuple[dict[str, Any], str] | None:
    """Pair a value with the indicator whose evidence sits closest to it.

    Position-in-list pairing assumed L2 returns scopes in value order, which
    the contract never required and the run disproved.  Evidence spans are
    anchored in the sentence, so proximity needs no ordering assumption.

    Distance is measured in both directions on purpose.  A level indicator
    precedes its figure (``수출액은 1223억달러``) but a change indicator follows
    it (``5.1% 증가``), so preferring what comes first would systematically
    mislabel every rate in the corpus.
    """
    if not isinstance(value_start, int) or not isinstance(value_end, int):
        return None
    located = [
        scope for scope in scopes
        if isinstance(scope.get("source_char_start"), int)
        and isinstance(scope.get("source_char_end"), int)
    ]
    if not located:
        return None
    # L2 often returns a whole clause as the evidence span.  When the spans
    # swallow the values, every gap is zero and proximity carries no signal,
    # so pairing must fall back rather than pick an arbitrary winner.
    gaps = [
        _gap(
            scope["source_char_start"],
            scope["source_char_end"],
            value_start,
            value_end,
        )
        for scope in located
    ]
    if len(located) == 1 and gaps[0] == 0:
        # One resolved evidence span is stronger than unresolved alternatives.
        # This occurs when L2 anchors the level clause but cannot reproduce a
        # shortened change clause verbatim.
        return located[0], "SPAN_CONTAINS"
    if gaps and len(set(gaps)) == 1 and gaps[0] == 0:
        sentence = str(sentence_text or "")
        before = sentence[max(0, value_start - 1):value_start]
        after = sentence[value_end:value_end + 8]
        if before == "(" and re.match(r"\)\s*보다", after):
            # A parenthesised comparison cell is a baseline LEVEL, even when
            # every L2 scope spans the whole clause.  Prefer the only scope
            # whose label does not describe a change.
            non_change = [
                scope for scope in located
                if not re.search(
                    r"(?:증가율|감소율|상승률|하락률|증감률|변화율|변동률|"
                    r"증감폭|증가폭|감소폭|차이|증감액|증가\s*금액|감소\s*금액)$",
                    str(scope.get("indicator_label") or "").strip(),
                )
            ]
            if len(non_change) == 1:
                return non_change[0], "PARENTHESIZED_BASELINE"
    if len(set(gaps)) < 2:
        return None
    scope = min(
        located,
        key=lambda item: _gap(
            item["source_char_start"],
            item["source_char_end"],
            value_start,
            value_end,
        ),
    )
    return scope, "SPAN_NEAREST"


def _inherited_period(
    layout: dict[int, dict[str, Any]],
    sentence_id: int,
    region_sentence_id: int | None,
) -> dict[str, Any] | None:
    """Carry the period forward the same way the indicator is carried.

    An article states its timeframe once and then reports figures without
    repeating it.  The walk stops at the region boundary for the same reason
    the indicator walk does: ``통계청 … 지난해`` followed by ``경총 … 올해`` must
    not leak one period into the other's figures.
    """
    for candidate_id in range(sentence_id - 1, -1, -1):
        if candidate_id not in layout:
            continue
        if region_sentence_id is not None and candidate_id < region_sentence_id:
            return None
        period = layout[candidate_id].get("period_context") or {}
        raw = str(period.get("period_raw") or "").strip()
        if raw:
            return {
                "period_raw": raw,
                "period_source": "INHERITED",
                "period_sentence_id": candidate_id,
            }
    return None


def _local_indicator(
    scopes: list[dict[str, Any]],
    value_index: int,
    value_count: int,
    value_start: object = None,
    value_end: object = None,
    sentence_text: object = None,
) -> dict[str, Any] | None:
    """Pick the scope that belongs to this value inside its own sentence."""
    usable = [
        scope for scope in scopes
        if str(scope.get("indicator_label") or "").strip()
    ]
    if not usable:
        return None
    matched = _nearest_scope(
        usable, value_start, value_end, sentence_text,
    )
    if matched is not None:
        scope, pairing = matched
    elif len(usable) == value_count:
        scope, pairing = usable[value_index], "POSITIONAL"
    else:
        # An over-general indicator is recoverable downstream; a missing one
        # is not, so the last scope is reused rather than dropping the value.
        scope = usable[min(value_index, len(usable) - 1)]
        pairing = "COUNT_MISMATCH_FALLBACK"
    return {
        "indicator_label": scope["indicator_label"],
        "indicator_source": "LOCAL",
        "indicator_pairing": pairing,
        "indicator_span_text": scope.get("source_span_text") or "",
    }


def compose_with_inherited(
    local: dict[str, Any],
    inherited: dict[str, Any] | None,
) -> dict[str, Any]:
    """Give a local fragment its subject back from the inherited indicator.

    The local part is kept rather than replaced: dropping ``성장률`` would lose
    the measure, and dropping the inherited subject would lose the only thing
    a table can be found by.  If the inherited label is itself a fragment there
    is nothing to gain, so the local one stands unchanged.
    """
    if inherited is None:
        return local
    head = str(inherited.get("indicator_label") or "").strip()
    fragment = str(local.get("indicator_label") or "").strip()
    if not head or indicator_is_fragment(head):
        return local
    # Only the measure noun is carried over.  The rest of the fragment is a
    # basis (``원화 기준``) or a period (``1분기``), and splicing those into the
    # middle of the parent produces a string no table is titled with —
    # ``작년 1인당 국민소득 미 달러화 기준 증가율`` was the observed damage.  They
    # stay recorded on the row for whoever needs them.
    tail = measure_tail(fragment) or fragment
    label = head if tail in head else f"{head} {tail}"
    return {
        **local,
        "indicator_label": label,
        "indicator_source": "LOCAL_COMPOSED",
        "indicator_local_fragment": fragment,
        "indicator_inherited_from": inherited.get("indicator_sentence_id"),
    }


def assign_roles(
    article_text: str,
    l2_sentences: list[dict[str, Any]],
    *,
    sentence_span_iterator: SentenceSpanIterator | None = None,
) -> list[dict[str, Any]]:
    """Return one role assignment per value candidate."""
    layout = {
        int(row["sentence_id"]): row
        for row in l2_sentences
        if row.get("sentence_id") is not None
    }
    sentences = {
        row["sentence_id"]: row["text"] for row in (
            sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator)
            if sentence_span_iterator is not None else sentence_offset_map(article_text)
        )
    }
    values_by_sentence = (_sentence_values(article_text, sentence_span_iterator=sentence_span_iterator)
                          if sentence_span_iterator is not None else _sentence_values(article_text))

    assignments: list[dict[str, Any]] = []
    for sentence_id, values in sorted(values_by_sentence.items()):
        entry = layout.get(sentence_id, {})
        scopes = entry.get("indicator_scopes") or []
        field_states = entry.get("field_states") if isinstance(entry.get("field_states"), dict) else {}
        indicator_state = field_states.get("indicator") if isinstance(field_states.get("indicator"), dict) else {}
        indicator_requires_clarification = indicator_state.get("state") in {"MISSING", "AMBIGUOUS"}
        region = resolve_region_chain(layout, sentence_id)
        period = entry.get("period_context") or {}
        sentence_text = sentences.get(sentence_id, "")
        if not str(period.get("period_raw") or "").strip():
            local_sentence_periods = [
                extract_sentence_period_span(
                    sentence_text,
                    value.get("sentence_char_start"),
                )
                for value in values
            ]
            inherited_period = _inherited_period(
                layout,
                sentence_id,
                region["region_sentence_id"] if region else None,
            )
        else:
            inherited_period = None
        for index, value in enumerate(values):
            indicator = _local_indicator(
                scopes,
                index,
                len(values),
                value.get("sentence_char_start"),
                value.get("sentence_char_end"),
                sentence_text,
            )
            # Composition overwrites a label the sentence actually states, so
            # it needs stronger evidence than filling an absent one: without a
            # resolved region the backward walk spans the whole article and
            # will happily attach an unrelated subject (`성장률` picked up
            # `10년물 국채 금리` in the 2026-08-03 dev run).  Plain inheritance
            # keeps its existing reach.
            compose = indicator is not None and region is not None and (
                indicator_is_fragment(indicator.get("indicator_label"))
            )
            if (indicator is None or compose) and not indicator_requires_clarification:
                inherited = _inherited_indicator(
                    layout,
                    sentence_id,
                    region["region_sentence_id"] if region else None,
                )
                indicator = (
                    inherited if indicator is None
                    else compose_with_inherited(indicator, inherited)
                )
            assignment = {
                "article_sentence_id": sentence_id,
                "value_span_id": value.get("span_id"),
                "value_text": value.get("text"),
                "value_unit": value.get("unit"),
                # L4's bare-percent branch inspects the predicate following
                # this exact value. Preserve the sentence-relative offsets
                # from L1 instead of silently dropping them at the L3 boundary.
                "value_char_start": value.get("sentence_char_start"),
                "value_char_end": value.get("sentence_char_end"),
                "sentence_text": sentences.get(sentence_id, ""),
                "indicator_label": None,
                "indicator_source": "NONE",
                "source_subtype": "",
                "source_region_sentence_id": None,
                "period_raw": str(period.get("period_raw") or ""),
                "period_source": "LOCAL" if period.get("period_raw") else "NONE",
                "period_char_start": None,
                "period_char_end": None,
                "field_states": {
                    "indicator": dict(indicator_state),
                } if indicator_requires_clarification else {},
                "clarification_required": "indicator" if indicator_requires_clarification else None,
            }
            sentence_period = local_sentence_periods[index] if not period.get("period_raw") else {}
            if not assignment["period_raw"] and sentence_period.get("raw"):
                assignment.update({
                    "period_raw": sentence_period["raw"],
                    "period_source": "LOCAL_SENTENCE_FALLBACK",
                    "period_char_start": sentence_period["start"],
                    "period_char_end": sentence_period["end"],
                })
            if indicator:
                assignment.update(indicator)
            if not assignment["period_raw"] and inherited_period:
                assignment.update(inherited_period)
            if region:
                assignment["source_subtype"] = region["source_subtype"]
                assignment["source_region_sentence_id"] = region[
                    "region_sentence_id"
                ]
                region_period = (
                    layout.get(int(region["region_sentence_id"]), {}).get(
                        "period_context"
                    )
                    or {}
                )
                assignment["region_period_raw"] = str(
                    region_period.get("period_raw") or ""
                )
            assignments.append(assignment)
    return assignments


def attach_indicator_evidence_monthly_v2h(
    assignment: dict[str, Any], article_text: str,
    *, sentence_span_iterator: SentenceSpanIterator | None = None,
) -> dict[str, Any]:
    """Attach only a uniquely reproducible selected-L2-scope receipt."""

    row = dict(assignment)
    source_text = str(row.get("indicator_span_text") or "")
    model_label = str(row.get("indicator_label") or "").strip()
    sentence_id = row.get("indicator_sentence_id", row.get("article_sentence_id", row.get("sentence_id")))
    sentence_rows = (
        sentence_offset_map(article_text, sentence_span_iterator=sentence_span_iterator)
        if sentence_span_iterator is not None else sentence_offset_map(article_text)
    )
    sentence = next(
        (str(item.get("text") or "") for item in sentence_rows if item.get("sentence_id") == sentence_id),
        "",
    )
    matches = list(re.finditer(re.escape(source_text), sentence)) if source_text else []
    if model_label and len(matches) == 1:
        match = matches[0]
        row["indicator_evidence"] = {
            "source_char_start": match.start(),
            "source_char_end": match.end(),
            "source_span_text": match.group(0),
            "model_indicator_label": model_label,
            "sentence_id": sentence_id,
        }
    return row


def assignment_summary(assignments: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    return {
        "values": len(assignments),
        "with_indicator": sum(
            1 for row in assignments if row.get("indicator_label")
        ),
        "indicator_source": dict(
            Counter(row["indicator_source"] for row in assignments)
        ),
        "with_source_subtype": sum(
            1 for row in assignments if row.get("source_subtype")
        ),
        "source_subtype": dict(
            Counter(row["source_subtype"] or "(없음)" for row in assignments)
        ),
        "with_period": sum(1 for row in assignments if row.get("period_raw")),
    }
