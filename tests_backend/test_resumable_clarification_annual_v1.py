from __future__ import annotations

import ast
import json
import hashlib
from datetime import date as calendar_date
from pathlib import Path
import sys
import types
from decimal import Decimal
from typing import Any, Mapping

from backend import develop_verify_service as service
from backend.app import DevelopVerifyRequest
from backend import verification_checkpoint_store as checkpoint_store
from backend.verification_checkpoint_store import CheckpointError, consume, create

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

# The focused helper tests do not need the full operational runner (whose
# optional HTTP dependencies are not part of this backend test environment).
# Keep the production helper importable while isolating only its two injected
# deterministic functions.
_operational_stub = types.ModuleType("src.develop.run_pipeline_operational_v2")

def _compare_stub(claim_value_text, _claim_unit_text, cell, _official_unit_text):
    return {
        "verdict": "VERIFIED" if Decimal(str(claim_value_text)) == Decimal(str(cell["DT"])) else "REFUTED",
        "reason": "MATCH",
    }

def _fetch_stub(query_plan, fetcher, **_guard_receipt):
    response = fetcher(dict(query_plan))
    if isinstance(response, list) and len(response) == 1:
        return {"status": "CELL_RESOLVED", "query": dict(query_plan), "cell": dict(response[0])}
    return {"status": "BASELINE_CELL_NOT_RESOLVED", "query": dict(query_plan)}

_operational_stub.compare_official_cell = _compare_stub
_operational_stub.fetch_exact_single_cell = _fetch_stub
_operational_stub.OperationalPipelineError = RuntimeError
_operational_stub.materialize_operational_l2 = lambda *args, **kwargs: None
_operational_stub.project_trace_operational_l2 = lambda *args, **kwargs: None
_operational_stub.run_live_from_files = lambda *args, **kwargs: None

from src.develop.annual_requery_shadow_v1 import verify_annual_requery

_trace_source = (
    RUNTIME_ROOT / "src" / "develop" / "run_article_body_pipeline_trace_v1.py"
).read_text(encoding="utf-8")
_trace_tree = ast.parse(_trace_source)
_apply_node = next(
    node for node in _trace_tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "_apply_clarification_context"
)
_trace_namespace = {
    "Any": Any,
    "Mapping": Mapping,
    "TraceStageError": type("TraceStageError", (RuntimeError,), {}),
    "calendar_date": calendar_date,
    "hashlib": hashlib,
    "json": json,
    "re": __import__("re"),
}
exec(
    compile(
        ast.Module(body=[_apply_node], type_ignores=[]),
        "run_article_body_pipeline_trace_v1.py",
        "exec",
    ),
    _trace_namespace,
)
_apply_clarification_context = _trace_namespace["_apply_clarification_context"]


BODY = "지난해 출생아 수는 25만4300명이다."


def _write_fake_outputs(root: Path, *, final: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "01_value_candidates.jsonl").write_text(
        json.dumps({"kind": "value_unit"}) + "\n", encoding="utf-8"
    )
    (root / "01_sentences.jsonl").write_text(
        json.dumps({"sentence_id": 0, "char_start": 0, "char_end": len(BODY), "text": BODY}) + "\n",
        encoding="utf-8",
    )
    (root / "03_routed.jsonl").write_text(
        json.dumps({
            "article_idx": "article-1", "article_sentence_id": 0,
            "value_span_id": "s0:value_unit:0-10", "sentence_text": BODY,
        }) + "\n",
        encoding="utf-8",
    )
    (root / "04_stage_ledger.jsonl").write_text(
        json.dumps({
            "article_idx": "article-1", "article_sentence_id": 0,
            "value_span_id": "s0:value_unit:0-10",
            "resolution": "DONE" if final else "ARTICLE_DATE_PROVENANCE_INVALID",
        }) + "\n",
        encoding="utf-8",
    )
    (root / "04_answers.jsonl").write_text(
        json.dumps({
            "article_idx": "article-1", "article_sentence_id": 0,
            "value_span_id": "s0:value_unit:0-10",
            "verdict": "VERIFIED" if final else "UNVERIFIABLE",
            "explanation": "공식 통계 근거를 확인했습니다." if final else "날짜가 필요합니다.",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_resume_uses_checkpoint_without_repeating_l1_l2(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_LIVE_STAGE_ENABLED", "true")
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    calls: list[str] = []
    call_kwargs: list[dict] = []

    class FakeTraceError(RuntimeError):
        pass

    def fake_run_trace(*, output_root, stage, **_kwargs):
        calls.append(stage)
        call_kwargs.append(dict(_kwargs))
        output = Path(output_root)
        if stage == "live":
            _write_fake_outputs(output, final=len(calls) > 4)
        elif stage == "layers":
            _write_fake_outputs(output, final=False)
        elif stage == "l1":
            _write_fake_outputs(output, final=False)
        elif stage == "l2":
            _write_fake_outputs(output, final=False)

    fake_run_trace._prepare_resume = lambda *_args: None
    monkeypatch.setattr(service, "_load_trace_runner", lambda: (fake_run_trace, FakeTraceError))

    first = service.verify_article_develop(BODY)
    assert first["type"] == "needs_user_input"
    assert first["question"]["role"] == "article_date"
    assert first["resume_from_stage"] == "layers"
    token = first["resume_token"]

    second = service.verify_article_develop(
        BODY,
        clarification_answers=[{
            "question_id": "clarify-article_date",
            "role": "article_date",
            "value": "2026-08-26",
        }],
        resume_token=token,
    )
    assert second["type"] == "article"
    assert calls == ["l1", "l2", "layers", "live", "layers", "live"]
    assert "clarification_context_path" in call_kwargs[4]
    assert "clarification_context_path" in call_kwargs[5]
    assert call_kwargs[4]["clarification_context_path"] == call_kwargs[5]["clarification_context_path"]


def test_article_date_hold_projects_to_user_question(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "04_stage_ledger.jsonl").write_text(
        json.dumps({"resolution": "ARTICLE_DATE_PROVENANCE_INVALID"}) + "\n", encoding="utf-8"
    )
    result = service._pending_article_date_from_live(output)
    assert result["question"]["role"] == "article_date"
    assert "YYYY-MM-DD" in result["question"]["prompt"]


def test_period_invalid_relative_period_projects_to_date_question_only_without_date(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "04_stage_ledger.jsonl").write_text(
        json.dumps({"resolution": "PERIOD_INVALID"}) + "\n", encoding="utf-8"
    )
    body = "지난해 출생아 수는 25만4300명이다."
    result = service._pending_article_date_from_live(output, body=body, article_date="")
    assert result["question"]["role"] == "article_date"
    assert service._pending_article_date_from_live(
        output, body=body, article_date="2026-08-26"
    ) is None


def test_checkpoint_default_root_uses_tempdir_when_temp_is_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("PIPELINE_CHECKPOINT_ROOT", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setattr(checkpoint_store.tempfile, "gettempdir", lambda: str(tmp_path))
    assert checkpoint_store._root() == (tmp_path / "kosis_verify_checkpoints").resolve()


def test_checkpoint_resume_reseals_copied_stage_manifest_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    workdir = tmp_path / "work"
    output = workdir / "out"
    output.mkdir(parents=True)
    article = {"article_idx": "fixture-1", "title": "fixture", "article_text": BODY, "date": ""}
    article_path = workdir / "articles.jsonl"
    article_path.write_text(json.dumps(article, ensure_ascii=False) + "\n", encoding="utf-8")
    article_sha = hashlib.sha256(article_path.read_bytes()).hexdigest()
    body_sha = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    stage_files = {
        "01": ("01_sentences.jsonl", "01_value_candidates.jsonl", "01_trace.log"),
        "02": ("02_l2_predictions.jsonl", "02_l2_results.jsonl", "02_trace.log"),
        "03": ("03_routed.jsonl", "03_trace.log"),
    }
    payload_snapshot: dict[str, dict[str, bytes]] = {}
    for stage, names in stage_files.items():
        payload_snapshot[stage] = {}
        for index, name in enumerate(names):
            content = (
                json.dumps({"stage": stage, "index": index}, ensure_ascii=False) + "\n"
                if name.endswith(".jsonl") else f"[{stage}] fixture\n"
            ).encode("utf-8")
            (output / name).write_bytes(content)
            payload_snapshot[stage][name] = content

    def record(path: Path) -> dict[str, object]:
        data = path.read_bytes()
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "rows": len([line for line in data.decode("utf-8").splitlines() if line.strip()]),
        }

    def manifest(stage: str, predecessor: str | None) -> dict[str, object]:
        names = stage_files[stage]
        data_names = [name for name in names if name.endswith(".jsonl")]
        log_names = [name for name in names if name.endswith(".log")]
        return {
            "contract_version": "article-body-pipeline-trace-v1",
            "status": "COMPLETE",
            "stage": stage,
            "article_input": {"path": str(article_path.resolve()), "sha256": article_sha},
            "ordered_article_ids": ["fixture-1"],
            "article_body_sha256": {"fixture-1": body_sha},
            "splitter_mode": "fixture",
            "splitter_source_sha256": "fixture",
            "predecessor_manifest_sha256": predecessor,
            "data_payloads": {name: record(output / name) for name in data_names},
            "sealed_logs": {name: record(output / name) for name in log_names},
            "runtime_payloads": {},
        }

    previous_sha = None
    for stage in ("01", "02", "03"):
        path = output / f"{stage}_manifest.json"
        value = manifest(stage, previous_sha)
        path.write_bytes(checkpoint_store._manifest_bytes(value))
        previous_sha = hashlib.sha256(path.read_bytes()).hexdigest()

    checkpoint = create(
        workdir=workdir,
        article_body_sha256=body_sha,
        title="fixture",
        article_id="fixture-1",
        clarification_history=[],
        runtime_fingerprint="runtime-sha",
        config_sha256="config-sha",
        resume_from_stage="layers",
    )
    try:
        copied_input = checkpoint.article_path.resolve()
        copied_output = checkpoint.output_root
        manifest_sha: dict[str, str] = {}
        for stage in ("01", "02", "03"):
            value = json.loads((copied_output / f"{stage}_manifest.json").read_text(encoding="utf-8"))
            assert value["article_input"] == {"path": str(copied_input), "sha256": article_sha}
            assert value["ordered_article_ids"] == ["fixture-1"]
            assert value["article_body_sha256"] == {"fixture-1": body_sha}
            for name, original in payload_snapshot[stage].items():
                assert (copied_output / name).read_bytes() == original
            manifest_sha[stage] = hashlib.sha256(
                (copied_output / f"{stage}_manifest.json").read_bytes()
            ).hexdigest()
        assert json.loads((copied_output / "01_manifest.json").read_text(encoding="utf-8"))["predecessor_manifest_sha256"] is None
        assert json.loads((copied_output / "02_manifest.json").read_text(encoding="utf-8"))["predecessor_manifest_sha256"] == manifest_sha["01"]
        assert json.loads((copied_output / "03_manifest.json").read_text(encoding="utf-8"))["predecessor_manifest_sha256"] == manifest_sha["02"]
        resumed = consume(
            checkpoint.token,
            article_body_sha256=body_sha,
            title="fixture",
            clarification_history=[],
            runtime_fingerprint="runtime-sha",
            config_sha256="config-sha",
        )
        assert resumed.article_path == checkpoint.article_path
    finally:
        checkpoint_store.discard(checkpoint)


def test_clarification_context_hydrates_article_date_and_provenance():
    body_sha = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    article = {"article_idx": "a", "date": "", "article_text": BODY}
    context = {
        "contract_version": "clarification-context-v1",
        "article_body_sha256": body_sha,
        "clarification_answers": [{
            "question_id": "clarify-article_date",
            "role": "article_date",
            "value": "2026-08-26",
        }],
    }
    hydrated = _apply_clarification_context([article], context)[0]
    assert hydrated["date"] == "2026-08-26"
    assert hydrated["article_date"] == "2026-08-26"
    assert hydrated["article_date_provenance"]["source"] == "USER_CLARIFICATION"
    assert hydrated["article_date_provenance"]["date_source"] == "user_feedback"
    assert hydrated["article_date_provenance"]["source_path"] == "clarification_context"
    assert hydrated["article_date_provenance"]["article_text_sha256"] == body_sha


def test_annual_change_only_does_not_compare_current_cell_as_level(monkeypatch):
    monkeypatch.setitem(sys.modules, "src.develop.run_pipeline_operational_v2", _operational_stub)
    change_row = {
        "value_span_id": "change-1",
        "value_text": "6.7",
        "value_unit": "%",
        "retrieval_fields": {
            "indicator": "출생아 수 전년 대비 증가율",
            "measurement_type": "CHANGE_RATE",
            "value_direction": "INCREASE",
            "period": {
                "measurement": {"absolute": "2026"},
                "baseline": {"absolute": "2025"},
            },
        },
    }
    current_plan = {
        "org_id": "org", "tbl_id": "tbl", "itm_id": "births", "prd_se": "Y",
        "start_prd_de": "2026", "end_prd_de": "2026", "obj_levels": {},
    }
    baseline_calls: list[str] = []

    def fetcher(plan):
        baseline_calls.append(plan["start_prd_de"])
        return [{"DT": "100", "ORG_ID": "org", "TBL_ID": "tbl", "ITM_ID": "births", "PRD_DE": "2025", "PRD_SE": "Y"}]

    result = verify_annual_requery(
        rows=[change_row],
        current_plan=current_plan,
        current_cell_result={"status": "CELL_RESOLVED", "cell": {"DT": "106.7"}},
        current_target_id="change-1",
        cell_fetcher=fetcher,
        official_unit="명",
    )
    assert result["verdict"] == "VERIFIED"
    assert "current_level" not in result["components"]
    assert result["components"]["change"]["verdict"] == "VERIFIED"
    assert result["claims"]["current"] is None
    assert Decimal(result["official"]["signed_change"]) == Decimal("6.7")
    assert baseline_calls == ["2025"]


def test_checkpoint_fingerprint_mismatch_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    workdir = tmp_path / "work"
    (workdir / "out").mkdir(parents=True)
    (workdir / "articles.jsonl").write_text(
        json.dumps({"article_idx": "a", "article_text": BODY}) + "\n", encoding="utf-8"
    )
    checkpoint = create(
        workdir=workdir,
        article_body_sha256="body-sha",
        title="제목",
        article_id="a",
        clarification_history=[],
        runtime_fingerprint="runtime-sha",
        config_sha256="config-sha",
        resume_from_stage="layers",
    )
    try:
        consume(
            checkpoint.token,
            article_body_sha256="different-body-sha",
            title="제목",
            clarification_history=[],
            runtime_fingerprint="runtime-sha",
            config_sha256="config-sha",
        )
    except CheckpointError as exc:
        assert exc.code == "RESUME_CHECKPOINT_FINGERPRINT_MISMATCH"
    else:  # pragma: no cover
        raise AssertionError("fingerprint mismatch must be rejected")


def test_annual_request_accepts_resume_token_and_annual_contract_is_not_monthly():
    request = DevelopVerifyRequest(text=BODY, resume_token="t" * 32)
    assert request.resume_token == "t" * 32
    source = (Path(__file__).parents[1] / "deploy" / "pipeline_runtime" / "src" / "news_verification" / "runtime" / "run_pipeline_operational_v2.py").read_text(encoding="utf-8")
    assert "monthly_contract = evidence_first_statistics_shadow and _uses_monthly_claim_contract(row)" in source
    assert "annual_requery = verify_annual_requery" in source
