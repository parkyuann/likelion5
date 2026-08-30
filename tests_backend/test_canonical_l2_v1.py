from __future__ import annotations

from copy import deepcopy
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

from src.develop.l2_segmentation import resolve_prediction
from src.news_verification.runtime.l1_value_candidates import iter_sentence_spans
from src.news_verification.runtime.run_pipeline_operational_v2 import materialize_operational_l2


def _prediction(source_span: str = "통계청", *, indicator_span: str = "출생아 수") -> dict:
    return {
        "sentences": [{
            "sentence_id": 0,
            "indicator_scopes": [{"indicator_label": "출생아 수", "source_span_text": indicator_span}],
            "source_region": {
                "opens_region": True,
                "governing_sentence_id": 0,
                "source_subtype": "공식집계",
                "source_span_text": source_span,
            },
            "period_context": {"period_raw": "2025년"},
        }],
        "model_value": "10명",
        "model_unit": "명",
    }


def test_exact_source_is_ready_and_only_cross_sentence_exact_is_repaired():
    article = "통계청은 출생아 수를 발표했다."
    ready = resolve_prediction(article, _prediction())
    assert ready["canonical_status"] == "L2_READY"
    assert ready["sentences"][0]["indicator_scopes"][0]["span_status"] == "RESOLVED"
    assert ready["raw_envelope"]["raw_prediction"]["model_value"] == "10명"

    article = "통계청이 발표했다. 출생아 수는 10명이다."
    repaired_prediction = deepcopy(_prediction())
    repaired_prediction["sentences"][0]["sentence_id"] = 1
    repaired_prediction["sentences"][0]["indicator_scopes"][0]["source_span_text"] = "출생아 수"
    repaired_prediction["sentences"][0]["source_region"]["source_span_text"] = "통계청"
    repaired_prediction["sentences"][0]["source_region"]["opens_region"] = False
    repaired = resolve_prediction(article, repaired_prediction, sentence_span_iterator=iter_sentence_spans)
    assert repaired["canonical_status"] == "REPAIRED_SOURCE_EXACT"
    assert repaired["sentences"][0]["source_region"]["source_sentence_id"] == 0


def test_missing_ambiguous_and_ownership_conflict_are_holds_without_value_mutation():
    missing = resolve_prediction("통계청은 출생아 수를 발표했다.", _prediction(source_span="없는 기관"))
    assert missing["canonical_status"] == "HOLD_NOT_FOUND"
    assert missing["unresolved_span_details"][0]["span_error_code"] == "NOT_FOUND"

    ambiguous = resolve_prediction("기관은 기관의 수치를 발표했다.", _prediction(source_span="기관", indicator_span="기관"))
    assert ambiguous["canonical_status"] == "HOLD_AMBIGUOUS"
    assert ambiguous["unresolved_span_details"][0]["span_error_code"] == "AMBIGUOUS"

    ownership = resolve_prediction(
        "출처A가 발표했다. 수치는 10명이다. 출처A가 다시 언급됐다.",
        {
            "sentences": [{
                "sentence_id": 1,
                "indicator_scopes": [],
                "source_region": {
                    "opens_region": False,
                    "governing_sentence_id": None,
                    "source_span_text": "출처A",
                },
            }],
        },
        sentence_span_iterator=iter_sentence_spans,
    )
    assert ownership["canonical_status"] == "HOLD_AMBIGUOUS"
    assert ownership["unresolved_span_details"][0]["field"] == "source_region"

    assert missing["raw_envelope"]["raw_prediction"]["model_value"] == "10명"
    assert missing["sentences"][0]["period_context"] == {"period_raw": "2025년"}


def test_raw_sha_does_not_change_canonical_semantic_sha_and_approximate_text_holds():
    article = "통계청은 출생아 수를 발표했다."
    first = _prediction()
    second = deepcopy(first)
    second["debug_attempt"] = 2
    one = resolve_prediction(article, first)
    two = resolve_prediction(article, second)
    assert one["raw_envelope"]["raw_prediction_sha256"] != two["raw_envelope"]["raw_prediction_sha256"]
    assert one["canonical_l2_sha256"] == two["canonical_l2_sha256"]

    approximate = resolve_prediction(article, _prediction(indicator_span="출생아수"))
    assert approximate["canonical_status"] == "HOLD_NOT_FOUND"


def test_exact_only_rejects_quote_whitespace_morphology_and_cross_owner_shortcuts():
    article = "통계청은 ‘출생아 수’를 발표했다. 출생아 수는 증가했다."
    for source_span, indicator_span in (
        ("'출생아 수'", "출생아 수"),
        ("통 계 청", "출생아 수"),
        ("통계청", "출생아수"),
    ):
        result = resolve_prediction(article, _prediction(source_span, indicator_span=indicator_span))
        assert result["canonical_status"] == "HOLD_NOT_FOUND"

    ownership = resolve_prediction(
        "통계청이 발표했다. 출생아 수는 증가했다. 보도자료는 다른 기관이 냈다.",
        {
            "sentences": [{
                "sentence_id": 1,
                "indicator_scopes": [{"indicator_label": "출생아 수", "source_span_text": "출생아 수"}],
                "source_region": {"opens_region": False, "governing_sentence_id": 0, "source_span_text": "다른 기관"},
            }],
        },
        sentence_span_iterator=iter_sentence_spans,
    )
    assert ownership["canonical_status"] == "HOLD_NOT_FOUND"


def test_call_failure_materializes_unavailable_and_invalid_span_receipt_is_not_silently_ready():
    result = materialize_operational_l2(
        [{"article_idx": "a", "article_text": "통계청은 수치를 발표했다."}],
        [],
        {
            "errors": [{"kind": "CALL_FAILED", "article_idx": "a"}],
            "article_runs": [{"article_idx": "a", "status": "CALL_FAILED"}],
        },
        external_model_calls=1,
    )
    assert result["results"][0]["status"] == "L2_UNAVAILABLE"

    held = materialize_operational_l2(
        [{"article_idx": "a", "article_text": "통계청은 출생아 수를 발표했다."}],
        [{"article_idx": "a", "sentence_id": 0, "indicator_scopes": [], "source_region": {}}],
        {"errors": [], "article_runs": [{"article_idx": "a", "status": "HOLD_NOT_FOUND"}]},
        external_model_calls=1,
    )
    assert held["results"][0]["status"] == "HOLD_NOT_FOUND"
    assert held["results"][0]["predictions"] == []
