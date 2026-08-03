"""Gate L2 — score the segmentation layer on its own gold, nothing else.

The point of a layer metric is to say which layer to fix.  End-to-end routing
numbers cannot do that, which is why r11~r16i spent 36 hours without knowing
where the loss came from.

Indicator scopes are scored by span overlap rather than label text: the gold
labels are free-form Korean, so string equality would grade phrasing instead
of segmentation.  Source region is scored as a category, and sentences the
reviewer left undecided are reported separately rather than counted as wrong.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .l2_gold_view import build_gold_view
except ImportError:  # pragma: no cover - direct script execution
    from l2_gold_view import build_gold_view


UNMEASURABLE_DOMINANCE = {"미판정", "판단 불가", "모순"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


_TOKEN_RE = __import__("re").compile(r"[가-힣A-Za-z0-9]+")


def _tokens(label: str) -> set[str]:
    return set(_TOKEN_RE.findall(str(label or "")))


def label_similarity(gold: str, predicted: str) -> float:
    """Token F1 between two indicator labels.

    Gold labels are free-form Korean written by a reviewer, so exact equality
    would grade phrasing rather than whether the same indicator was found.
    """
    gold_tokens, pred_tokens = _tokens(gold), _tokens(predicted)
    if not gold_tokens or not pred_tokens:
        return 0.0
    shared = len(gold_tokens & pred_tokens)
    if not shared:
        return 0.0
    precision = shared / len(pred_tokens)
    recall = shared / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def match_labels(
    gold: list[str],
    predicted: list[str],
    *,
    threshold: float = 0.5,
) -> int:
    """Greedily pair indicator labels that name the same indicator."""
    used: set[int] = set()
    matched = 0
    for gold_label in gold:
        best_index, best_score = None, 0.0
        for index, label in enumerate(predicted):
            if index in used:
                continue
            score = label_similarity(gold_label, label)
            if score > best_score:
                best_score, best_index = score, index
        if best_index is not None and best_score >= threshold:
            used.add(best_index)
            matched += 1
    return matched


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def match_spans(
    gold: list[tuple[int, int]],
    predicted: list[tuple[int, int]],
    *,
    threshold: float = 0.5,
) -> int:
    """Greedily pair spans whose overlap covers half of the longer span."""
    used: set[int] = set()
    matched = 0
    for gold_span in gold:
        best_index = None
        best_score = 0.0
        for index, span in enumerate(predicted):
            if index in used:
                continue
            span_len = max(
                gold_span[1] - gold_span[0], span[1] - span[0], 1
            )
            score = _overlap(gold_span, span) / span_len
            if score > best_score:
                best_score, best_index = score, index
        if best_index is not None and best_score >= threshold:
            used.add(best_index)
            matched += 1
    return matched


def _predicted_spans(sentence: dict[str, Any]) -> list[tuple[int, int]]:
    spans = []
    for scope in sentence.get("indicator_scopes") or []:
        if scope.get("span_status") != "RESOLVED":
            continue
        spans.append((scope["source_char_start"], scope["source_char_end"]))
    return spans


def _predicted_subtype(
    sentence: dict[str, Any],
    by_article: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    region = sentence.get("source_region") or {}
    dominance = str(region.get("dominance") or "")
    if dominance == "지배 없음":
        return "지배 없음", ""
    subtype = str(region.get("source_subtype") or "")
    if not subtype:
        # An inherited sentence points at the sentence that opened the region
        # instead of repeating its subtype, so follow the reference the same
        # way the gold view resolves ``dominant_region_decision``.
        governing = region.get("governing_sentence_id")
        if governing is None:
            governing = region.get("introduced_in_sentence_id")
        source = by_article.get(governing) if governing is not None else None
        if source is not None:
            subtype = str(
                (source.get("source_region") or {}).get("source_subtype") or ""
            )
    return dominance or "미판정", subtype


def evaluate(
    gold_records: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    by_sentence = {
        (str(row.get("article_idx")), int(row.get("sentence_id"))): row
        for row in predictions
    }
    by_article: dict[str, dict[int, dict[str, Any]]] = {}
    for row in predictions:
        by_article.setdefault(str(row.get("article_idx")), {})[
            int(row.get("sentence_id"))
        ] = row
    gold_label_total = predicted_label_total = matched_label_total = 0
    gold_span_total = predicted_span_total = matched_total = 0
    subtype_correct = subtype_total = 0
    dominance_correct = dominance_total = 0
    count_correct = count_total = 0
    unmeasurable = missing = 0
    excluded: Counter[str] = Counter()
    span_status: Counter[str] = Counter()
    per_reason: dict[str, Counter[str]] = {}

    for gold in gold_records:
        key = (str(gold["article_idx"]), int(gold["sentence_id"]))
        predicted = by_sentence.get(key)
        if predicted is None:
            missing += 1
            gold_span_total += len(gold["indicator_spans"])
            gold_label_total += len(gold["indicator_labels"])
            continue

        gold_labels = [label for label in gold["indicator_labels"] if label]
        pred_labels = [
            str(scope.get("indicator_label") or "")
            for scope in predicted.get("indicator_scopes") or []
        ]
        pred_labels = [label for label in pred_labels if label]
        gold_label_total += len(gold_labels)
        predicted_label_total += len(pred_labels)
        matched_label_total += match_labels(gold_labels, pred_labels)

        gold_spans = [tuple(span) for span in gold["indicator_spans"]]
        pred_spans = _predicted_spans(predicted)
        gold_span_total += len(gold_spans)
        predicted_span_total += len(pred_spans)
        matched_total += match_spans(gold_spans, pred_spans)
        for scope in predicted.get("indicator_scopes") or []:
            span_status[str(scope.get("span_status"))] += 1

        count_total += 1
        if len(gold_spans) == len(pred_spans):
            count_correct += 1

        reason = gold.get("review_reason") or gold["row_kind"]
        bucket = per_reason.setdefault(reason, Counter())
        bucket["sentences"] += 1

        if gold["dominance_class"] in UNMEASURABLE_DOMINANCE:
            unmeasurable += 1
            excluded[gold["dominance_class"]] += 1
            continue
        dominance_total += 1
        pred_dominance, pred_subtype = _predicted_subtype(
            predicted, by_article.get(str(gold["article_idx"]), {})
        )
        gold_governed = gold["dominance_class"] in {"정의", "상속"}
        pred_governed = pred_dominance in {"정의", "상속"}
        if gold_governed == pred_governed:
            dominance_correct += 1
            bucket["dominance_correct"] += 1
        if gold_governed:
            subtype_total += 1
            if pred_subtype == gold["source_subtype"]:
                subtype_correct += 1
                bucket["subtype_correct"] += 1

    def _prf(matched: int, predicted_count: int, gold_count: int) -> dict[str, Any]:
        precision = matched / predicted_count if predicted_count else 0.0
        recall = matched / gold_count if gold_count else 0.0
        return {
            "gold": gold_count,
            "predicted": predicted_count,
            "matched": matched,
            "precision": precision,
            "recall": recall,
            "f1": (
                2 * precision * recall / (precision + recall)
                if precision + recall
                else 0.0
            ),
        }

    precision = matched_total / predicted_span_total if predicted_span_total else 0.0
    recall = matched_total / gold_span_total if gold_span_total else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "sentences_scored": count_total,
        "sentences_missing_from_prediction": missing,
        "primary_metric": "indicator_label",
        "indicator_label": _prf(
            matched_label_total, predicted_label_total, gold_label_total
        ),
        "indicator_span_diagnostic_only": {
            "gold": gold_span_total,
            "predicted": predicted_span_total,
            "matched": matched_total,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "note": (
                "gold spans mark three different things (indicator phrase, "
                "source attribution, whole clause), so this is reported for "
                "hygiene only and is not the Gate L2 criterion"
            ),
        },
        "indicator_scope_count_accuracy": (
            count_correct / count_total if count_total else 0.0
        ),
        "source_region_dominance_accuracy": (
            dominance_correct / dominance_total if dominance_total else 0.0
        ),
        "source_subtype_accuracy": (
            subtype_correct / subtype_total if subtype_total else 0.0
        ),
        "dominance_scored": dominance_total,
        "subtype_scored": subtype_total,
        "unmeasurable_sentences": unmeasurable,
        "unmeasurable_reasons": dict(excluded),
        "predicted_span_status": dict(span_status),
        "per_review_reason": {
            key: dict(value) for key, value in per_reason.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold-human", type=Path, required=True)
    parser.add_argument("--gold-context", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    gold = build_gold_view(
        _read_jsonl(args.gold_human), _read_jsonl(args.gold_context)
    )
    result = evaluate(gold, _read_jsonl(args.predictions))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
