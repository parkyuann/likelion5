from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.news_verification.runtime import run_pipeline_operational_v2 as operational  # noqa: E402
from src.develop import run_article_body_pipeline_trace_v1 as trace  # noqa: E402
from src.news_verification.runtime.article_body_sentence_splitter_v1 import (  # noqa: E402
    SPLITTER_MODE,
    splitter_source_sha256,
)
from src.news_verification.runtime.l1_value_candidates import iter_sentence_spans  # noqa: E402
from src.develop.l2_segmentation import resolve_prediction  # noqa: E402
from backend.develop_verify_service import (  # noqa: E402
    _pre_live_clarification_plan,
    _project_failure_recovery_clarification,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "integration_article.jsonl"
with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
    ARTICLE = json.loads(fixture_file.readline())
ARTICLE = {
    **ARTICLE,
    "article_idx": f"{ARTICLE['article_idx']}:indicator-clarification",
    "date": "2026-08-26",
}
TARGET_ID = f"{ARTICLE['article_idx']}:value-1"


def _routed_row() -> dict[str, object]:
    return {
        "article_idx": ARTICLE["article_idx"],
        "target_id": TARGET_ID,
        "value_span_id": "value-1",
        "article_sentence_id": 0,
        "sentence_text": ARTICLE["article_text"],
        "value_text": "0.80",
        "value_unit": "명",
        "indicator_label": None,
        "indicator_source": "NONE",
        "field_states": {
            "indicator": {
                "contract_version": "l2-indicator-evidence-v1",
                "state": "MISSING",
                "reason_code": "INDICATOR_EVIDENCE_MISSING",
                "value_span_ids": ["value-1"],
                "period_preserved": True,
            }
        },
        "clarification_required": "indicator",
        "routing_class": "CLARIFICATION_REQUIRED",
        "confidence": 0.0,
        "reason": "INDICATOR_FIELD_CLARIFICATION_REQUIRED",
        "authoritative_retrieval_authorized": False,
        "retrieval_fields": {
            "indicator": "",
            "measurement_type": "LEVEL",
            "period_raw": "2025년",
            "period_absolute": "2025",
        },
    }


def _precomputed() -> tuple[dict[str, object], dict[str, object]]:
    body_sha = hashlib.sha256(ARTICLE["article_text"].encode("utf-8")).hexdigest()
    l2_manifest_sha = "l2-manifest-sha"
    common = {
        "article_input_sha256": "fixture-input-sha",
        "ordered_article_ids": [ARTICLE["article_idx"]],
        "article_body_sha256": {ARTICLE["article_idx"]: body_sha},
        "splitter_mode": SPLITTER_MODE,
        "splitter_source_sha256": splitter_source_sha256(),
    }
    l2 = {
        "results": [{
            "article_idx": ARTICLE["article_idx"],
            "status": "L2_CLARIFICATION_REQUIRED",
            "predictions": [],
        }],
        "manifest": {},
        "provenance": {
            **common,
            "stage_manifest_path": "02_manifest.json",
            "stage_manifest_sha256": l2_manifest_sha,
            "predictions": [],
            "results": [],
        },
    }
    routed = {
        "rows": [_routed_row()],
        "provenance": {
            **common,
            "stage_manifest_path": "03_manifest.json",
            "stage_manifest_sha256": "routed-manifest-sha",
            "predecessor_manifest_sha256": l2_manifest_sha,
            "routed": [],
        },
    }
    return l2, routed


def test_first_request_emits_indicator_question_without_authoritative_calls(monkeypatch):
    counters = {"l2": 0, "stack": 0, "retrieval": 0, "cell": 0}

    def raw_l2_runner(articles, **kwargs):
        counters["l2"] += 1
        assert len(articles) == 1
        resolved = resolve_prediction(
            ARTICLE["article_text"],
            {
                "sentences": [{
                    "sentence_id": 0,
                    "indicator_scopes": [{
                        "indicator_label": "인구 성장률",
                        "source_span_text": ARTICLE["article_text"],
                    }],
                    "source_region": {},
                    "period_context": {"period_raw": "2025년"},
                }]
            },
            sentence_span_iterator=iter_sentence_spans,
        )
        predictions = [
            {"article_idx": ARTICLE["article_idx"], **sentence}
            for sentence in resolved["sentences"]
        ]
        return predictions, {
            "errors": [],
            "article_runs": [{
                "article_idx": ARTICLE["article_idx"],
                "status": resolved["canonical_status"],
                "reason_code": resolved["canonical_reason_code"],
                "resolver_version": resolved["resolver_version"],
                "repair_reason_code": resolved["repair_reason_code"],
                "raw_prediction_sha256": resolved["raw_envelope"]["raw_prediction_sha256"],
                "canonical_l2_sha256": resolved["canonical_l2_sha256"],
            }],
        }

    def stack_runner(articles, predictions):
        counters["stack"] += 1
        return operational.run_stack(articles, predictions)

    monkeypatch.setattr(
        operational,
        "_binding_or_retrieve_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("indicator clarification must precede retrieval")
        ),
    )
    result = operational.run_new_articles_v2(
        [ARTICLE],
        l2_api_key="",
        search_channels={},
        release_sha256_by_channel={},
        catalog_records=(),
        reranker=None,
        profile_provider=lambda _key: None,
        cell_fetcher=lambda _query: counters.__setitem__("cell", counters["cell"] + 1) or [],
        hcx_answerer=None,
        l2_runner=raw_l2_runner,
        stack_runner=stack_runner,
        release_bound_mode=True,
    )

    ledger = result["stage_ledger"][0]
    recovery = ledger["failure_recovery_shadow"]
    assert counters == {"l2": 1, "stack": 1, "retrieval": 0, "cell": 0}
    assert result["l2"]["article_runs"][0]["status"] == "L2_CLARIFICATION_REQUIRED"
    assert ledger["resolution"] == "CLARIFICATION_REQUIRED"
    assert recovery["contract_version"] == "failure-recovery-shadow-v1"
    assert recovery["state"] == "DIRECT_FIELD_MISSING"
    assert recovery["reason"] == "INDICATOR_REQUIRED"
    assert recovery["question"]["role"] == "indicator"
    assert recovery["question"]["input_mode"] == "FREE_TEXT"
    assert recovery["question"]["options"] == []
    assert recovery["question"]["model_prefill"] is False
    assert recovery["question"]["internal_ids_exposed"] is False
    assert recovery["state_sha256"] == recovery["sha256"]
    assert ledger["call_ledger"]["cell_api"] == 0
    projected = _project_failure_recovery_clarification(recovery)
    assert projected["contract_version"] == "clarification-plan-v2"
    assert projected["resume_from_stage"] == "retrieval"
    assert projected["reusable_artifacts"] == ["l1", "l2", "layers"]
    assert projected["invalidated_stages"] == ["retrieval", "binding", "cell", "answer"]
    assert projected["cell_api_calls"] == 0


def test_indicator_speculative_first_plan_resumes_from_retrieval(monkeypatch):
    class SpeculativeChannel:
        def speculative(self, *args, **kwargs):
            return None

    class SpeculativeProfiles:
        def speculative(self, table_key, *, timeout_seconds):
            assert table_key == "fixture-table"
            assert timeout_seconds > 0
            return {
                "meta_status": "READY",
                "profile_sha256": "fixture-profile-sha",
                "items": [{"itm_id": "TFR", "itm_nm": "합계출산율"}],
            }

    monkeypatch.setattr(
        operational,
        "_retrieve_with_request_cache",
        lambda *args, **kwargs: (
            [SimpleNamespace(table_key="fixture-table")],
            {"all_paths_failed": False, "candidate_membership": ["fixture-table"]},
        ),
    )
    plan, audit = operational._speculative_clarification_plan(
        _routed_row(),
        article_text=ARTICLE["article_text"],
        search_channels={"bm25": SpeculativeChannel()},
        release_sha256_by_channel={"bm25": "fixture-release-sha"},
        profile_provider=SpeculativeProfiles(),
        retrieval_cache=None,
        deadline_ms=2500,
    )

    assert audit["status"] == "OPTIONS_READY"
    assert plan is not None
    assert plan["reason"] == "INDICATOR_REQUIRED"
    assert plan["question"]["role"] == "indicator"
    assert plan["resume_from_stage"] == "retrieval"
    assert plan["reusable_artifacts"] == ["l1", "l2", "layers"]
    assert plan["invalidated_stages"] == ["retrieval", "binding", "cell", "answer"]


def test_backend_early_gate_does_not_repeat_answered_indicator(tmp_path):
    (tmp_path / "03_routed.jsonl").write_text(
        json.dumps(_routed_row(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    pending = _pre_live_clarification_plan(
        tmp_path,
        body=ARTICLE["article_text"],
        article_date=ARTICLE["date"],
        clarification_history=[{
            "question_id": "clarify-indicator-answered",
            "role": "indicator",
            "value": "합계출산율",
        }],
    )

    assert pending is None


def test_retrieval_resume_rebinds_predecessor_context(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    context = tmp_path / "clarification_context.json"
    context.write_text(json.dumps({"contract_version": "clarification-context-v2"}), encoding="utf-8")
    (root / "03_manifest.json").write_text(
        json.dumps({"stage": "03", "clarification_context_sha256": "old-context-sha"}),
        encoding="utf-8",
    )

    trace.prepare_resume(root, "retrieval", clarification_context_path=context)

    manifest = json.loads((root / "03_manifest.json").read_text(encoding="utf-8"))
    assert manifest["clarification_context_sha256"] == hashlib.sha256(context.read_bytes()).hexdigest()


def test_indicator_answer_resumes_from_retrieval_without_l1_l2_or_layers(monkeypatch):
    counters = {"l1": 0, "l2": 0, "layers": 0, "live": 0, "retrieval": 0, "cell": 0}
    l2, routed = _precomputed()
    merged_rows: list[dict[str, object]] = []
    resumed_article = {
        **ARTICLE,
        "clarification_answers": [{
            "question_id": "clarify-indicator-answered",
            "role": "indicator",
            "value": "합계출산율",
        }],
    }

    def unexpected_l2(*args, **kwargs):
        counters["l2"] += 1
        raise AssertionError("resume must not rerun L1/L2")

    def unexpected_stack(*args, **kwargs):
        counters["layers"] += 1
        raise AssertionError("resume must not rerun layers")

    def resumed_retrieval(*args, **kwargs):
        counters["retrieval"] += 1
        return [], {"all_paths_failed": False, "candidate_membership": []}

    original_merge = operational._merge_user_clarifications

    def capture_merge(row, article):
        merged = original_merge(row, article)
        merged_rows.append(merged)
        return merged

    monkeypatch.setattr(operational, "_binding_or_retrieve_candidates", resumed_retrieval)
    monkeypatch.setattr(operational, "_merge_user_clarifications", capture_merge)
    counters["live"] += 1
    result = operational.run_new_articles_v2(
        [resumed_article],
        l2_api_key="",
        search_channels={},
        release_sha256_by_channel={},
        catalog_records=(),
        reranker=None,
        profile_provider=lambda _key: None,
        cell_fetcher=lambda _query: counters.__setitem__("cell", counters["cell"] + 1) or [],
        hcx_answerer=None,
        l2_runner=unexpected_l2,
        stack_runner=unexpected_stack,
        release_bound_mode=True,
        precomputed_l2=l2,
        precomputed_routed=routed,
    )

    ledger = result["stage_ledger"][0]
    assert counters == {
        "l1": 0, "l2": 0, "layers": 0, "live": 1, "retrieval": 1, "cell": 0,
    }
    assert ledger["resolution"] == "NO_CANDIDATES"
    assert ledger.get("failure_recovery_shadow", {}).get("reason") != "INDICATOR_REQUIRED"
    assert all(answer.get("verdict") != "CLARIFICATION_REQUIRED" for answer in result["answers"])
    assert merged_rows[0]["retrieval_fields"]["indicator"] == "합계출산율"
    assert merged_rows[0]["user_clarifications"]["indicator"]["source"] == "USER_CLARIFICATION"
