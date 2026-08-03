"""Score the six retrieval fields against the reviewer's claim gold.

Four properties of the gold decide how this is measured, all confirmed by
reading it before writing the scorer:

* ``period gold`` keeps the article's own wording (``지난해``) and appends the
  comparison basis (``·전년동기비``).  It is compared against the raw period,
  never against the absolutised one.
* ``dimension gold`` names the breakdown a statistic is published by
  (``주당 36시간 미만``, ``상위 10개사``).  A facet dictionary cannot produce those,
  so the joint metric is reported with and without dimension.
* ``없음`` is an explicit answer, not a blank.
* Blank-versus-blank agreement is counted separately.  The 2026-07-17 baseline
  reported ``source_role_exact 0.898`` that was 110/119 blank-blank matches;
  folding those into accuracy again would repeat a known measurement error.

Exact match is reported because the historical 0.009 was exact — a relaxed
metric alone would not be comparable to it.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .evaluate_l2_segmentation import label_similarity
except ImportError:  # pragma: no cover - direct script execution
    from evaluate_l2_segmentation import label_similarity


FIELDS = ("indicator", "measurement", "period", "population", "item", "dimension")
# The fields a KOSIS table search actually queries with.  Population, item and
# dimension name the axes a table is broken down by, which the article does not
# state — CLAUDE.md 3절 resolves those against table metadata after retrieval,
# so grading them here charges this layer for a later layer's job.
RETRIEVAL_FIELDS = ("indicator", "measurement", "period")
GOLD_COLUMN = {name: f"{name} gold" for name in FIELDS}
PREDICTION_KEY = {
    "indicator": "indicator",
    "measurement": "measurement_type",
    "period": "period",
    "population": "population",
    "item": "item",
    "dimension": "dimension",
}
ABSENT = {"없음", "-", "n/a", "N/A", "해당없음"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_claim_gold(snapshot_path: Path) -> list[dict[str, Any]]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    matrix = snapshot["matrices"]["claim_gold"]
    values = [row.get("value") if isinstance(row, dict) else row for row in matrix]
    values = [row for row in values if isinstance(row, list)]
    header = [str(cell) for cell in values[0]]
    return [
        dict(zip(header, [*row, *([None] * (len(header) - len(row)))]))
        for row in values[1:]
    ]


def _normalise(value: object) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text in ABSENT:
        return ""
    return text


_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text))


def compare_field(
    name: str,
    gold: object,
    predicted: object,
    *,
    threshold: float = 0.5,
) -> str:
    """Return ``EXACT`` / ``SUBSUMED`` / ``RELAXED`` / ``WRONG`` / ``BOTH_ABSENT``.

    ``SUBSUMED`` marks one label being a strictly more specific form of the
    other (``취업자 수`` inside ``제조업 취업자 수``).  Those are not errors for
    retrieval — the narrower label queries the same table — but they are kept
    separate from ``EXACT`` so the distinction stays visible.
    """
    gold_text = _normalise(gold)
    pred_text = _normalise(predicted)
    if not gold_text and not pred_text:
        return "BOTH_ABSENT"
    if not gold_text or not pred_text:
        return "WRONG"
    if gold_text == pred_text:
        return "EXACT"
    if name == "measurement":
        # A closed enum has no partial credit.
        return "WRONG"
    gold_tokens, pred_tokens = _tokens(gold_text), _tokens(pred_text)
    if gold_tokens and pred_tokens and (
        gold_tokens <= pred_tokens or pred_tokens <= gold_tokens
    ):
        return "SUBSUMED"
    return (
        "RELAXED"
        if label_similarity(gold_text, pred_text) >= threshold
        else "WRONG"
    )


def evaluate(
    gold_rows: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    scope: str = "KOSIS_CANDIDATE",
) -> dict[str, Any]:
    predicted = {
        (str(row.get("article_idx")), str(row.get("value_span_id"))): row
        for row in predictions
    }
    per_field: dict[str, Counter] = {name: Counter() for name in FIELDS}
    joint_exact = joint_relaxed = 0
    joint_exact_no_dim = joint_relaxed_no_dim = 0
    joint_three_exact = joint_three_relaxed = 0
    three_field_misses: Counter = Counter()
    near_miss: dict[str, Counter] = {}
    scored = unjoined = 0

    for row in gold_rows:
        if str(row.get("claim 여부") or "").strip() != "YES":
            continue
        if scope and str(row.get("검증대상 gold") or "").strip() != scope:
            continue
        span_id = str(row.get("candidate span ID") or "").strip()
        key = (str(row.get("article_idx")), span_id)
        prediction = predicted.get(key) if span_id else None
        if prediction is None:
            unjoined += 1
            continue
        scored += 1
        fields = prediction.get("retrieval_fields") or {}
        outcomes = {}
        for name in FIELDS:
            gold_value = row.get(GOLD_COLUMN[name])
            pred_value = fields.get(PREDICTION_KEY[name])
            outcome = compare_field(name, gold_value, pred_value)
            per_field[name][outcome] += 1
            outcomes[name] = outcome
            if outcome == "WRONG" and name in RETRIEVAL_FIELDS:
                # Recording how close a miss was separates a threshold effect
                # from a genuine failure; a cluster just under the cut means
                # the metric is strict, a cluster near zero means the layer is.
                score = label_similarity(
                    _normalise(gold_value), _normalise(pred_value)
                )
                bucket = (
                    "0.0" if score == 0 else
                    "0.0~0.2" if score < 0.2 else
                    "0.2~0.4" if score < 0.4 else
                    "0.4~0.5" if score < 0.5 else "0.5+"
                )
                near_miss.setdefault(name, Counter())[bucket] += 1

        def _ok(names, relaxed: bool) -> bool:
            allowed = {"EXACT", "BOTH_ABSENT"}
            if relaxed:
                allowed = allowed | {"RELAXED", "SUBSUMED"}
            return all(outcomes[name] in allowed for name in names)

        joint_exact += _ok(FIELDS, False)
        joint_relaxed += _ok(FIELDS, True)
        no_dim = tuple(name for name in FIELDS if name != "dimension")
        joint_exact_no_dim += _ok(no_dim, False)
        joint_relaxed_no_dim += _ok(no_dim, True)
        joint_three_exact += _ok(RETRIEVAL_FIELDS, False)
        joint_three_relaxed += _ok(RETRIEVAL_FIELDS, True)
        if not _ok(RETRIEVAL_FIELDS, True):
            blockers = tuple(
                name for name in RETRIEVAL_FIELDS
                if outcomes[name] not in {"EXACT", "RELAXED", "BOTH_ABSENT"}
            )
            three_field_misses[" + ".join(blockers)] += 1

    def _rate(count: int) -> float:
        return count / scored if scored else 0.0

    return {
        "scope": scope,
        "scored": scored,
        "gold_rows_without_matching_prediction": unjoined,
        "per_field": {
            name: {
                **dict(counter),
                "exact_accuracy": (
                    counter["EXACT"] / scored if scored else 0.0
                ),
                "relaxed_accuracy": (
                    (
                        counter["EXACT"]
                        + counter["RELAXED"]
                        + counter["SUBSUMED"]
                        + counter["BOTH_ABSENT"]
                    )
                    / scored
                    if scored
                    else 0.0
                ),
            }
            for name, counter in per_field.items()
        },
        "retrieval_fields": list(RETRIEVAL_FIELDS),
        "joint_three_exact": _rate(joint_three_exact),
        "joint_three_relaxed": _rate(joint_three_relaxed),
        "three_field_blocking_combination": dict(
            three_field_misses.most_common()
        ),
        "wrong_similarity_distribution": {
            name: dict(counter) for name, counter in near_miss.items()
        },
        "joint_six_exact": _rate(joint_exact),
        "joint_six_relaxed": _rate(joint_relaxed),
        "joint_five_exact_without_dimension": _rate(joint_exact_no_dim),
        "joint_five_relaxed_without_dimension": _rate(joint_relaxed_no_dim),
        "note": (
            "BOTH_ABSENT is counted separately per field and treated as "
            "agreement in the joint metric only when gold itself says 없음"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--fields", type=Path, required=True)
    parser.add_argument("--scope", default="KOSIS_CANDIDATE")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(
        load_claim_gold(args.snapshot),
        _read_jsonl(args.fields),
        scope=args.scope,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
