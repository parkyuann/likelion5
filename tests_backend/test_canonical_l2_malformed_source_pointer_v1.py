from __future__ import annotations

from pathlib import Path
import sys
import types

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas

from src.develop.l2_segmentation import (  # noqa: E402
    DOWNSTREAM_L2_ELIGIBLE,
    resolve_prediction,
)
from src.news_verification.runtime.l1_value_candidates import iter_sentence_spans  # noqa: E402


ARTICLE = "2025년 전국 출생아 수는 25만4341명이다."


def _prediction(
    *,
    source_span_text: str = "[0]",
    source_subtype: str = "",
    indicator_scopes: list[dict] | None = None,
    governing_sentence_id: int | None = 0,
) -> dict:
    return {
        "sentences": [{
            "sentence_id": 0,
            "indicator_scopes": indicator_scopes if indicator_scopes is not None else [{
                "indicator_label": "출생아 수",
                "source_span_text": "출생아 수",
            }],
            "source_region": {
                "opens_region": True,
                "governing_sentence_id": governing_sentence_id,
                "source_subtype": source_subtype,
                "source_span_text": source_span_text,
            },
            "period_context": {},
        }],
    }


def _resolve(article: str = ARTICLE, prediction: dict | None = None) -> dict:
    return resolve_prediction(
        article,
        _prediction() if prediction is None else prediction,
        sentence_span_iterator=iter_sentence_spans,
    )


def test_current_ec2_raw_fixture_pointer_is_boundedly_normalized():
    result = _resolve()

    assert result["canonical_status"] == "REPAIRED_SOURCE_NOT_PROVIDED"
    assert result["canonical_reason_code"] == "MALFORMED_SOURCE_POINTER_WITHOUT_EXACT_EVIDENCE"
    assert result["canonical_status"] in DOWNSTREAM_L2_ELIGIBLE
    region = result["sentences"][0]["source_region"]
    assert region["opens_region"] is False
    assert region["governing_sentence_id"] is None
    assert region["source_subtype"] == ""
    assert region["source_span_text"] == ""
    assert region["span_status"] == "NOT_PROVIDED"
    assert region["dominance"] == "지배 없음"
    receipt = result["repair_receipts"][0]
    assert receipt["repair_action"] == "NORMALIZE_MODEL_POINTER_TO_NOT_PROVIDED"
    assert receipt["reason_code"] == "MALFORMED_SOURCE_POINTER_WITHOUT_EXACT_EVIDENCE"
    assert receipt["sentence_id"] == 0
    assert receipt["original_source_span_text"] == "[0]"
    assert receipt["pointer_artifact_class"] == "BRACKETED_INTEGER"
    assert receipt["exact_source_span_match_count"] == 0
    assert receipt["exact_source_cue_match_count"] == 0
    assert receipt["exact_indicator_count"] == 1
    assert receipt["owned_l1_value_count"] == 1
    assert receipt["ownership_conflict"] is False
    assert receipt["source_cue_registry_version"] >= 1
    assert receipt["source_cue_registry_sha256"]
    assert result["canonicalization_receipt_sha256"]


def test_pointer_artifact_variants_share_canonical_hash_but_not_raw_receipt_hash():
    first = _resolve(prediction=_prediction(source_span_text="[0]"))
    second = _resolve(prediction=_prediction(source_span_text="[1]"))

    assert first["canonical_l2_sha256"] == second["canonical_l2_sha256"]
    assert first["raw_envelope"]["raw_prediction_sha256"] != second["raw_envelope"]["raw_prediction_sha256"]
    assert first["canonicalization_receipt_sha256"] != second["canonicalization_receipt_sha256"]


def test_pointer_literal_present_is_resolved_and_never_downgraded():
    article = "2025년 전국 출생아 수는 [0] 25만4341명이다."
    result = _resolve(article)

    region = result["sentences"][0]["source_region"]
    assert region["span_status"] == "RESOLVED"
    assert region["source_span_text"] == "[0]"
    assert result["repair_receipts"] == []
    assert result["unresolved_spans"] == 0


def test_arbitrary_missing_source_text_remains_hold():
    result = _resolve(prediction=_prediction(source_span_text="통계청"))

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert result["repair_receipts"] == []
    assert result["sentences"][0]["source_region"]["span_status"] == "UNRESOLVED"


def test_exact_source_cue_in_current_sentence_blocks_downgrade():
    article = "통계청에 따르면 2025년 출생아 수는 25만4341명이다."
    result = _resolve(article)

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert result["repair_receipts"] == []
    assert result["sentences"][0]["source_region"]["span_status"] == "UNRESOLVED"


def test_ordinary_suffix_word_is_not_treated_as_an_organization_source_cue():
    article = "일부에 따르면 2025년 출생아 수는 25만4341명이다."
    result = _resolve(article)

    assert result["canonical_status"] == "REPAIRED_SOURCE_NOT_PROVIDED"
    assert result["repair_receipts"][0]["exact_source_cue_match_count"] == 0


def test_incoherent_governing_pointer_never_downgrades():
    article = "출생아 수는 10명이다. 출생아 수는 11명이다."
    prediction = _prediction()
    prediction["sentences"][0]["sentence_id"] = 1
    prediction["sentences"][0]["source_region"]["governing_sentence_id"] = 0
    result = _resolve(
        article,
        prediction,
    )

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert all(
        receipt.get("repair_action") != "NORMALIZE_MODEL_POINTER_TO_NOT_PROVIDED"
        for receipt in result["repair_receipts"]
    )
    unresolved_row = next(row for row in result["sentences"] if row["sentence_id"] == 1)
    assert unresolved_row["source_region"]["span_status"] == "UNRESOLVED"


def test_exact_source_cue_in_valid_governing_context_blocks_downgrade():
    article = "통계청에 따르면 출생아 수는 10명이다. 출생아 수는 11명이다."
    prediction = {
        "sentences": [
            {
                "sentence_id": 0,
                "indicator_scopes": [{"indicator_label": "출생아 수", "source_span_text": "출생아 수"}],
                "source_region": {
                    "opens_region": True,
                    "governing_sentence_id": 0,
                    "source_subtype": "공식집계",
                    "source_span_text": "통계청에 따르면",
                },
                "period_context": {},
            },
            {
                "sentence_id": 1,
                "indicator_scopes": [{"indicator_label": "출생아 수", "source_span_text": "출생아 수"}],
                "source_region": {
                    "opens_region": True,
                    "governing_sentence_id": 0,
                    "source_subtype": "",
                    "source_span_text": "[0]",
                },
                "period_context": {},
            },
        ],
    }
    result = _resolve(article, prediction)

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert result["repair_receipts"] == []
    assert result["sentences"][1]["source_region"]["span_status"] == "UNRESOLVED"


def test_nonempty_source_subtype_blocks_downgrade():
    result = _resolve(prediction=_prediction(source_subtype="공식집계"))

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert result["repair_receipts"] == []


def test_zero_or_multiple_indicator_and_value_ownership_remain_hold():
    no_indicator = _resolve(prediction=_prediction(indicator_scopes=[]))
    multiple_indicators = _resolve(prediction=_prediction(indicator_scopes=[
        {"indicator_label": "출생아 수", "source_span_text": "출생아 수"},
        {"indicator_label": "출생아 수", "source_span_text": "출생아 수"},
    ]))
    multiple_values = _resolve(
        "출생아 수는 10명에서 11명으로 늘었다.",
        _prediction(),
    )

    assert no_indicator["canonical_status"] == "HOLD_NOT_FOUND"
    assert multiple_indicators["canonical_status"] == "HOLD_NOT_FOUND"
    assert multiple_values["canonical_status"] == "HOLD_NOT_FOUND"
    assert no_indicator["repair_receipts"] == []
    assert multiple_indicators["repair_receipts"] == []
    assert multiple_values["repair_receipts"] == []


def test_invalid_indicator_span_remains_unresolved_and_is_not_overwritten():
    result = _resolve(prediction=_prediction(indicator_scopes=[{
        "indicator_label": "출생아 수",
        "source_span_text": "출생아수",
    }]))

    assert result["canonical_status"] == "HOLD_NOT_FOUND"
    assert result["sentences"][0]["indicator_scopes"][0]["span_status"] == "UNRESOLVED"
    assert result["sentences"][0]["source_region"]["span_status"] == "UNRESOLVED"
    assert result["repair_receipts"] == []


def test_existing_exact_source_span_is_preserved():
    article = "통계청에 따르면 2025년 출생아 수는 25만4341명이다."
    prediction = _prediction(
        source_span_text="통계청에 따르면",
        source_subtype="공식집계",
    )
    result = _resolve(article, prediction)

    region = result["sentences"][0]["source_region"]
    assert region["span_status"] == "RESOLVED"
    assert region["source_span_text"] == "통계청에 따르면"
    assert region["source_subtype"] == "공식집계"
    assert result["repair_receipts"] == []


def test_another_registered_source_less_metric_is_repaired():
    article = "합계출산율은 0.80명을 기록했다."
    prediction = _prediction(
        indicator_scopes=[{"indicator_label": "합계출산율", "source_span_text": "합계출산율"}],
    )
    result = _resolve(article, prediction)

    assert result["canonical_status"] == "REPAIRED_SOURCE_NOT_PROVIDED"
    assert result["sentences"][0]["indicator_scopes"][0]["span_status"] == "RESOLVED"
    assert result["repair_receipts"][0]["exact_indicator_count"] == 1
    assert result["repair_receipts"][0]["owned_l1_value_count"] == 1
