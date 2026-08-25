"""L5 — decide whether a value goes to KOSIS retrieval.

팀 인계: 현재 검증 대상 선별 관문이다. 모든 수치 후보에 routing class·확신도·사유를
기록하며, 불확실한 값을 조용히 버리지 않는다.

The previous contract made this decision with 161 status codes and 627 Korean
literals, and blocked at 0.330 precision: it discarded two verifiable claims
for every correct block.  Here the decision reads the source region L2 already
established, because who published a figure is what determines whether KOSIS
can hold it.

Blocking is a score, not a veto.  A wrongly routed claim ends as
``UNVERIFIABLE`` downstream, which 프로젝트 사양 2.2절 accepts; a dropped claim is
unrecoverable.  So an undetermined region routes with low confidence rather
than being discarded, and the threshold decides.
"""

from __future__ import annotations

import re
from typing import Any

KOSIS_CANDIDATE = "KOSIS_CANDIDATE"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
NOT_CLAIM = "NOT_CLAIM"

OFFICIAL_SUBTYPE = "공식집계"
OUT_OF_SCOPE_SUBTYPES = frozenset({
    "민간조사", "정책목표", "잠정추산", "법정기준",
})

DEFAULT_THRESHOLD = 0.5


def indicator_repeats_value(assignment: dict[str, Any]) -> bool:
    """Whether a value candidate was copied into its own indicator label.

    ``소득 하위 20%`` and ``1달러=1코인`` use the number as a category or
    definition, not as the observation the surrounding sentence reports.  The
    check is structural and corpus-independent: no subject vocabulary is added.
    """
    value = re.sub(r"\s+", "", str(assignment.get("value_text") or ""))
    indicator = re.sub(
        r"\s+", "",
        str((assignment.get("retrieval_fields") or {}).get("indicator") or ""),
    )
    return bool(value and re.search(r"\d", value) and value in indicator)


def route_value(assignment: dict[str, Any]) -> dict[str, Any]:
    """Return the routing class, its confidence and the reason."""
    subtype = str(assignment.get("source_subtype") or "").strip()
    indicator = str(assignment.get("indicator_label") or "").strip()

    if subtype in OUT_OF_SCOPE_SUBTYPES:
        return {
            "routing_class": OUT_OF_SCOPE,
            "confidence": 1.0,
            "reason": f"SOURCE_SUBTYPE_{subtype}",
        }
    if indicator_repeats_value(assignment):
        return {
            "routing_class": NOT_CLAIM,
            "confidence": 1.0,
            "reason": "VALUE_REPEATED_INSIDE_INDICATOR",
        }
    if subtype == OFFICIAL_SUBTYPE:
        return {
            "routing_class": KOSIS_CANDIDATE,
            "confidence": 1.0,
            "reason": "OFFICIAL_AGGREGATE_REGION",
        }
    if indicator:
        # L2 did not resolve a region, but the value measures something.
        # Routing it costs an UNVERIFIABLE at worst; dropping it is final.
        return {
            "routing_class": KOSIS_CANDIDATE,
            "confidence": 0.5,
            "reason": "INDICATOR_WITHOUT_RESOLVED_REGION",
        }
    return {
        "routing_class": NOT_CLAIM,
        "confidence": 1.0,
        "reason": "NO_INDICATOR_NO_REGION",
    }


def route_all(
    assignments: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Apply routing and demote low-confidence KOSIS calls below ``threshold``."""
    routed = []
    for assignment in assignments:
        decision = route_value(assignment)
        if (
            decision["routing_class"] == KOSIS_CANDIDATE
            and decision["confidence"] < threshold
        ):
            decision = {
                **decision,
                "routing_class": NOT_CLAIM,
                "reason": decision["reason"] + "_BELOW_THRESHOLD",
            }
        routed.append({**assignment, **decision, "threshold": threshold})
    return routed


def routing_summary(routed: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    return {
        "values": len(routed),
        "classes": dict(Counter(row["routing_class"] for row in routed)),
        "reasons": dict(Counter(row["reason"] for row in routed)),
        "confidence": dict(Counter(row["confidence"] for row in routed)),
    }


def evaluate_routing(
    gold_rows: list[dict[str, Any]],
    routed: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score routing against per-candidate gold, keyed by value span."""
    from collections import Counter

    predicted = {
        (str(row.get("article_idx")), str(row.get("value_span_id"))): row
        for row in routed
    }
    confusion: Counter = Counter()
    true_positive = false_positive = false_negative = true_negative = 0
    missing = 0
    for gold in gold_rows:
        gold_class = str(gold.get("judged_class") or "").strip()
        if not gold_class:
            continue
        key = (str(gold.get("article_idx")), str(gold.get("value_span_id")))
        row = predicted.get(key)
        if row is None:
            missing += 1
            if gold_class == KOSIS_CANDIDATE:
                false_negative += 1
            continue
        predicted_class = row["routing_class"]
        confusion[f"{gold_class}->{predicted_class}"] += 1
        gold_kosis = gold_class == KOSIS_CANDIDATE
        pred_kosis = predicted_class == KOSIS_CANDIDATE
        true_positive += gold_kosis and pred_kosis
        false_positive += (not gold_kosis) and pred_kosis
        false_negative += gold_kosis and (not pred_kosis)
        true_negative += (not gold_kosis) and (not pred_kosis)

    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    blocked = false_negative + true_negative
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "values_missing_from_prediction": missing,
        "precision": precision,
        "recall": recall,
        "f1": (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        ),
        # The metric that exposed the old contract: of everything blocked, how
        # much deserved it.  The previous pipeline scored 0.330 here.
        "abstention_precision": (
            true_negative / blocked if blocked else 0.0
        ),
        "confusion": dict(confusion),
    }
