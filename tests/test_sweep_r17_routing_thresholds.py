import json

from src.develop.sweep_r17_routing_thresholds import (
    build_sweep_report,
    score_routing_row,
    sweep_condition,
)


def _row(review_id, eligibility, action, errors=()):
    return {
        "review_id": review_id,
        "gold": {"eligibility": eligibility},
        "prediction": {"detected": True},
        "automatic": {
            "action": action,
            "semantic_errors": list(errors),
            "binding_errors": [],
            "scope_errors": [],
        },
    }


def test_anchor_claim_and_observation_codes_receive_one_penalty():
    row = _row(
        "a",
        "KOSIS_CANDIDATE",
        "BLOCKED",
        (
            "INDICATOR_ANCHOR_NOT_FOUND_IN_VALUE_SENTENCE",
            "VALUE_OUTSIDE_INDICATOR_SCOPE",
        ),
    )

    scored = score_routing_row(
        row,
        soften_anchor=True,
        soften_grounding=False,
    )

    assert scored["hard_blocked"] is False
    assert scored["score"] == 0.75
    assert scored["soft_groups"] == ["ANCHOR_SCOPE"]


def test_rounds_add_anchor_then_grounding_recall():
    rows = [
        _row("pass", "KOSIS_CANDIDATE", "PASS"),
        _row(
            "anchor",
            "KOSIS_CANDIDATE",
            "BLOCKED",
            ("INDICATOR_ANCHOR_NOT_FOUND_IN_VALUE_SENTENCE",),
        ),
        _row(
            "ground",
            "KOSIS_CANDIDATE",
            "BLOCKED",
            (
                "SEMANTIC_VALIDATION_BLOCKED",
                "INDICATOR_NORM_NOT_GROUNDED_IN_CLAIM_SENTENCES",
            ),
        ),
        _row(
            "source",
            "OUT_OF_SCOPE",
            "BLOCKED",
            ("PRIVATE_SOURCE_CONTEXT_OUT_OF_SCOPE",),
        ),
    ]

    round1 = sweep_condition(
        rows,
        soften_anchor=True,
        soften_grounding=False,
    )
    round2 = sweep_condition(
        rows,
        soften_anchor=True,
        soften_grounding=True,
    )
    at_075 = next(row for row in round1 if row["threshold"] == 0.75)
    at_065 = next(row for row in round2 if row["threshold"] == 0.65)

    assert at_075["true_positive"] == 2
    assert at_065["true_positive"] == 3
    assert at_065["false_positive"] == 0


def test_report_requires_ambiguous_retry_zero(tmp_path):
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps(
            _row("pass", "KOSIS_CANDIDATE", "PASS"),
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "ambiguous_retry_audit": {
            "targets": 8,
            "resolved_non_ambiguous": 8,
            "still_ambiguous": 0,
            "missing_from_retry": 0,
            "current_candidate_class_ambiguous": 0,
        },
    }), encoding="utf-8")

    report, curve = build_sweep_report(
        {"LEXICAL_ON": predictions, "LEXICAL_OFF": predictions},
        audit,
    )

    assert report["ambiguous_retry_verification"][
        "excluded_from_softening"
    ] is True
    assert report["operating_threshold_selected"] is False
    assert len(curve) == 36
