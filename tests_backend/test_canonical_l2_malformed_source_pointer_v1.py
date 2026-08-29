from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterator, Mapping

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.develop.l2_segmentation import (  # noqa: E402
    DOWNSTREAM_L2_ELIGIBLE,
    resolve_prediction,
)
from src.news_verification.runtime.l1_value_candidates import iter_sentence_spans  # noqa: E402
from src.news_verification.runtime.run_pipeline_operational_v2 import (  # noqa: E402
    materialize_operational_l2,
)


ARTICLE = "2025년 전국 출생아 수는 25만4341명이다."


def _isolated_trace_stage_functions() -> dict[str, Any]:
    """Execute only the two changed trace functions without importing its graph.

    Other collection modules intentionally replace the legacy operational
    wrapper with a small test seam.  Importing the complete trace module here
    would bind to that unrelated global seam, so this follows the existing
    AST-isolation pattern used by the resume tests and supplies only the
    direct dependencies of stage 02 and stage 03.
    """
    source = (RUNTIME_ROOT / "src" / "develop" / "run_article_body_pipeline_trace_v1.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"run_l2_stage", "run_layers_stage"}
    }
    assert set(wanted) == {"run_l2_stage", "run_layers_stage"}

    def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    namespace: dict[str, Any] = {
        "Any": Any,
        "Callable": Callable,
        "Iterator": Iterator,
        "Mapping": Mapping,
        "Path": Path,
        "json": json,
        "DOWNSTREAM_L2_ELIGIBLE": DOWNSTREAM_L2_ELIGIBLE,
        "iter_article_body_sentence_spans": iter_sentence_spans,
        "L2ReceiptError": RuntimeError,
        "OperationalPipelineError": RuntimeError,
        "TraceStageError": type("TraceStageError", (RuntimeError,), {}),
        "env_api_key": lambda: "",
        "materialize_operational_l2": materialize_operational_l2,
        "project_trace_operational_l2": lambda value: value,
        "enforce_call_limits": lambda value: dict(value),
        "_atomic_jsonl": _atomic_jsonl,
        "_emit_log": lambda *_args, **_kwargs: None,
        "_sha_file": lambda _path: "0" * 64,
        "_manifest": lambda **_kwargs: {},
        "_publish_manifest": lambda *_args, **_kwargs: None,
        "_STAGE_FILES": {
            "02": ({"02_l2_predictions.jsonl", "02_l2_results.jsonl"}, {"02_trace.log"}),
            "03": ({"03_routed.jsonl"}, {"03_trace.log"}),
        },
    }
    exec(
        compile(
            ast.Module(body=[wanted["run_l2_stage"], wanted["run_layers_stage"]], type_ignores=[]),
            "run_article_body_pipeline_trace_v1.py",
            "exec",
        ),
        namespace,
    )
    return namespace


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


def test_operational_and_article_trace_consumers_preserve_and_route_new_status(
    tmp_path,
):
    article = {"article_idx": "a", "article_text": ARTICLE}
    canonical = _resolve()
    prediction = {"article_idx": "a", **canonical["sentences"][0]}
    raw_manifest = {
        "contract_version": "test-l2-receipt-v1",
        "producer": "focused-regression",
        "articles": 1,
        "sentences_predicted": 1,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "latency_ms_total": 1.0,
        "article_runs": [{
            "article_idx": "a",
            "attempts": 1,
            "status": canonical["canonical_status"],
            "reason_code": canonical["canonical_reason_code"],
            "resolver_version": canonical["resolver_version"],
            "repair_reason_code": canonical["repair_reason_code"],
            "raw_prediction_sha256": canonical["raw_envelope"]["raw_prediction_sha256"],
            "canonical_l2_sha256": canonical["canonical_l2_sha256"],
        }],
        "errors": [],
        "generation_config": None,
        "hcx_l2_calls": 1,
    }

    materialized = materialize_operational_l2(
        [article],
        [prediction],
        raw_manifest,
        external_model_calls=1,
        sentence_span_iterator=iter_sentence_spans,
    )
    assert materialized["results"] == [{
        "article_idx": "a",
        "status": "REPAIRED_SOURCE_NOT_PROVIDED",
        "predictions": [prediction],
        "canonical_status": "REPAIRED_SOURCE_NOT_PROVIDED",
        "canonical_reason_code": "MALFORMED_SOURCE_POINTER_WITHOUT_EXACT_EVIDENCE",
        "resolver_version": canonical["resolver_version"],
        "repair_reason_code": "MALFORMED_SOURCE_POINTER_WITHOUT_EXACT_EVIDENCE",
        "raw_prediction_sha256": canonical["raw_envelope"]["raw_prediction_sha256"],
        "canonical_l2_sha256": canonical["canonical_l2_sha256"],
    }]

    article_path = tmp_path / "articles.jsonl"
    article_path.write_text(json.dumps(article, ensure_ascii=False) + "\n", encoding="utf-8")
    run_root = tmp_path / "trace"
    run_root.mkdir()
    trace = _isolated_trace_stage_functions()
    trace["run_l2"] = lambda *_args, **_kwargs: ([prediction], raw_manifest)
    trace["run_l2_stage"](
        [article],
        article_path,
        run_root,
        api_key="focused-test-key",
        sentence_span_iterator=iter_sentence_spans,
    )
    saved_predictions = [
        json.loads(line)
        for line in (run_root / "02_l2_predictions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    saved_results = [
        json.loads(line)
        for line in (run_root / "02_l2_results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert saved_predictions == [prediction]
    assert saved_results == [{
        "article_idx": "a",
        "status": "REPAIRED_SOURCE_NOT_PROVIDED",
        "prediction_row_start": 0,
        "prediction_row_end": 1,
    }]

    routed_input: dict[str, object] = {}

    def _capture_run_stack(articles, rows, **_kwargs):
        routed_input["articles"] = articles
        routed_input["rows"] = rows
        return [{
            "article_idx": "a",
            "value_span_id": "s0:value_unit:18-26",
            "routing_class": "KOSIS",
            "retrieval_queries": [],
        }]

    trace["run_stack"] = _capture_run_stack
    trace["run_layers_stage"](
        [article],
        article_path,
        run_root,
        sentence_span_iterator=iter_sentence_spans,
    )
    assert routed_input["articles"] == [article]
    assert routed_input["rows"] == [prediction]

    for hold_status in ("HOLD_NOT_FOUND", "HOLD_AMBIGUOUS", "L2_UNAVAILABLE"):
        hold_manifest = {
            **raw_manifest,
            "article_runs": [{
                **raw_manifest["article_runs"][0],
                "status": hold_status,
            }],
        }
        materialized_hold = materialize_operational_l2(
            [article],
            [prediction],
            hold_manifest,
            external_model_calls=1,
            sentence_span_iterator=iter_sentence_spans,
        )
        assert materialized_hold["results"][0]["status"] == hold_status
        assert materialized_hold["results"][0]["predictions"] == []

        trace["run_l2"] = lambda *_args, _manifest=hold_manifest, **_kwargs: ([prediction], _manifest)
        trace["run_l2_stage"](
            [article],
            article_path,
            run_root,
            api_key="focused-test-key",
            sentence_span_iterator=iter_sentence_spans,
        )
        assert (run_root / "02_l2_predictions.jsonl").read_text(encoding="utf-8") == ""
        hold_results = [
            json.loads(line)
            for line in (run_root / "02_l2_results.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert hold_results == [{
            "article_idx": "a",
            "status": hold_status,
            "prediction_row_start": 0,
            "prediction_row_end": 0,
        }]

        blocked_input: dict[str, object] = {}

        def _capture_blocked_stack(articles, rows, **_kwargs):
            blocked_input["articles"] = articles
            blocked_input["rows"] = rows
            return []

        trace["run_stack"] = _capture_blocked_stack
        trace["run_layers_stage"](
            [article],
            article_path,
            run_root,
            sentence_span_iterator=iter_sentence_spans,
        )
        assert blocked_input == {"articles": [], "rows": []}
