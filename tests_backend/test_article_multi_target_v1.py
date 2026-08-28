from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from backend import develop_verify_service as service


RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
for import_root in (RUNTIME_ROOT, RUNTIME_ROOT / "src" / "news_verification" / "runtime"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas
if "requests" not in sys.modules:
    requests = types.ModuleType("requests")
    requests.RequestException = RuntimeError
    requests.get = lambda *args, **kwargs: None
    requests.post = lambda *args, **kwargs: None
    requests.Session = lambda: None
    sys.modules["requests"] = requests

import src.news_verification.runtime.run_pipeline_operational_v2 as operational
from src.news_verification.runtime.release_bound_live_adapters_v1 import CanonicalMetadataProfileProvider


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _official_ledger(target_id: str, value_span_id: str, value: str, *, release_id: str = "release:test") -> dict:
    query = {
        "org_id": "101",
        "tbl_id": "DT_TEST",
        "itm_id": "T1",
        "prd_se": "Y",
        "start_prd_de": "2025",
        "end_prd_de": "2025",
        "obj_levels": {"objL1": "00"},
    }
    return {
        "article_idx": "article:test",
        "target_id": target_id,
        "value_span_id": value_span_id,
        "resolution": {"outcome": "QUERY_READY", "chosen_table_key": "101:DT_TEST"},
        "query_plan": query,
        "selected_table": {
            "table_key": "101:DT_TEST",
            "send_de": "2026-07-29",
            "release_id": release_id,
            "profile_sha256": "a" * 64,
            "query_plan_sha256": service._receipt_sha256(query),
        },
        "call_ledger": {"cell_api": 1},
        "official_unit": "명",
        "cell": {
            "status": "CELL_RESOLVED",
            "query": query,
            "response_sha256": "b" * 64,
            "cell": {"DT": value},
        },
    }


def test_article_live_passes_all_routed_targets_without_representative_query(
    monkeypatch,
) -> None:
    captured: list[dict] = []

    class FakeTraceError(Exception):
        pass

    def fake_run_trace(*, articles_path, output_root, stage, **kwargs):
        captured.append({"stage": stage, **kwargs})
        output_root.mkdir(parents=True, exist_ok=True)
        if stage == "l1":
            _write_jsonl(
                output_root / "01_value_candidates.jsonl",
                [{"kind": "value_unit"}, {"kind": "value_unit"}],
            )
            _write_jsonl(
                output_root / "01_sentences.jsonl",
                [
                    {"sentence_id": 0, "char_start": 0, "char_end": 5},
                    {"sentence_id": 1, "char_start": 6, "char_end": 11},
                ],
            )
        if stage == "layers":
            _write_jsonl(
                output_root / "03_routed.jsonl",
                [
                    {
                        "article_idx": "article:test",
                        "target_id": "target:births",
                        "value_span_id": "span:births",
                        "article_sentence_id": 0,
                        "value_text": "10",
                        "retrieval_fields": {
                            "indicator": "출생아 수",
                            "measurement_type": "LEVEL",
                        },
                    },
                    {
                        "article_idx": "article:test",
                        "target_id": "target:tfr",
                        "value_span_id": "span:tfr",
                        "article_sentence_id": 1,
                        "value_text": "0.8",
                        "retrieval_fields": {
                            "indicator": "합계출산율",
                            "measurement_type": "LEVEL",
                        },
                    },
                ],
            )
        if stage == "live":
            _write_jsonl(
                output_root / "04_stage_ledger.jsonl",
                [
                    _official_ledger("target:births", "span:births", "10"),
                    _official_ledger("target:tfr", "span:tfr", "0.8"),
                ],
            )
            _write_jsonl(
                output_root / "04_answers.jsonl",
                [
                    {"article_idx": "article:test", "target_id": "target:births", "verdict": "VERIFIED", "explanation": "공식값"},
                    {"article_idx": "article:test", "target_id": "target:tfr", "verdict": "VERIFIED", "explanation": "공식값"},
                ],
            )

    monkeypatch.setenv("PIPELINE_LIVE_STAGE_ENABLED", "true")
    monkeypatch.setenv("KOSIS_RELEASE_ID", "release:test")
    monkeypatch.setattr(service, "_load_trace_runner", lambda: (fake_run_trace, FakeTraceError))

    result = service.verify_article_develop(
        "출생아 10명. 합계출산율 0.8.",
        date="2026-08-24",
        date_source="user_feedback",
    )

    live_calls = [row for row in captured if row["stage"] == "live"]
    assert len(live_calls) == 1
    assert live_calls[0]["claim_query"] is None
    assert result["status"] == "completed"
    assert len(result["target_receipts"]) == 2
    assert {row["target_id"] for row in result["target_receipts"]} == {
        "target:births",
        "target:tfr",
    }


def test_live_status_never_calls_zero_cells_completed(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "03_routed.jsonl",
        [
            {"target_id": "target:one", "value_span_id": "span:one"},
            {"target_id": "target:two", "value_span_id": "span:two"},
        ],
    )
    _write_jsonl(
        tmp_path / "04_stage_ledger.jsonl",
        [
            {"target_id": "target:one", "resolution": "NO_CANDIDATES", "cell": {"status": "NO_CELL"}},
            {"target_id": "target:two", "resolution": "PERIOD_INVALID", "cell": {"status": "NO_CELL"}},
        ],
    )

    status, receipts = service._live_status(tmp_path)

    assert status == "unverifiable"
    assert len(receipts) == 2


def test_live_status_is_partial_when_only_some_targets_have_official_cells(
    monkeypatch, tmp_path: Path,
) -> None:
    _write_jsonl(
        tmp_path / "03_routed.jsonl",
        [
            {"target_id": "target:one", "value_span_id": "span:one"},
            {"target_id": "target:two", "value_span_id": "span:two"},
        ],
    )
    _write_jsonl(
        tmp_path / "04_stage_ledger.jsonl",
        [
            _official_ledger("target:one", "span:one", "1"),
            {"target_id": "target:two", "resolution": "NO_CANDIDATES", "cell": {"status": "NO_CELL"}},
        ],
    )

    monkeypatch.setenv("KOSIS_RELEASE_ID", "release:test")
    status, _ = service._live_status(tmp_path)

    assert status == "completed_with_limits"


def test_live_status_rejects_incomplete_cell_resolved_receipt(
    monkeypatch, tmp_path: Path,
) -> None:
    _write_jsonl(
        tmp_path / "03_routed.jsonl",
        [{"article_idx": "article:test", "target_id": "target:one", "value_span_id": "span:one"}],
    )
    _write_jsonl(
        tmp_path / "04_stage_ledger.jsonl",
        [{
            "article_idx": "article:test",
            "target_id": "target:one",
            "value_span_id": "span:one",
            "resolution": {"outcome": "QUERY_READY"},
            "cell": {"status": "CELL_RESOLVED", "cell": {"DT": "1"}},
        }],
    )
    monkeypatch.setenv("KOSIS_RELEASE_ID", "release:test")

    status, receipts = service._live_status(tmp_path)

    assert status == "unverifiable"
    assert receipts[0]["official_cell_evidence"] is False
    assert receipts[0]["limitation_code"] == "SELECTED_TABLE_MISSING"


def test_operational_article_mode_preserves_all_same_family_targets(monkeypatch) -> None:
    routed = [
        {
            "article_idx": "article:test",
            "article_sentence_id": 0,
            "value_span_id": "level",
            "sentence_text": "출생아 수는 10명이다.",
            "value_text": "10",
            "value_unit": "명",
            "retrieval_fields": {"indicator": "출생아 수", "measurement_type": "LEVEL", "period_absolute": "2025"},
        },
        {
            "article_idx": "article:test",
            "article_sentence_id": 0,
            "value_span_id": "rate",
            "sentence_text": "증가율은 6.7%다.",
            "value_text": "6.7",
            "value_unit": "%",
            "retrieval_fields": {"indicator": "출생아 수 증가율", "measurement_type": "CHANGE_RATE", "period_absolute": "2025"},
        },
        {
            "article_idx": "article:test",
            "article_sentence_id": 0,
            "value_span_id": "point",
            "sentence_text": "증가폭은 16024명이다.",
            "value_text": "16024",
            "value_unit": "명",
            "retrieval_fields": {"indicator": "출생아 수 증가폭", "measurement_type": "CHANGE_POINT", "period_absolute": "2025"},
        },
    ]
    monkeypatch.setattr(
        operational,
        "run_operational_l2",
        lambda *_args, **_kwargs: {"results": [{"article_idx": "article:test", "status": "L2_READY", "predictions": []}], "manifest": {}},
    )
    monkeypatch.setattr(
        operational,
        "select_primary_target",
        lambda rows: {"primary": rows[0], "group_key": ("article:test", 0, "출생아", "2025"), "value_span_ids": ["level", "rate", "point"]},
    )
    monkeypatch.setattr(operational, "retrieve_parallel", lambda *_args, **_kwargs: ([], {"candidate_membership": []}))

    result = operational.run_new_articles_v2(
        [{"article_idx": "article:test", "title": "기사", "date": "2026-08-26", "article_text": "출생아 수는 10명이다."}],
        l2_api_key="",
        search_channels={},
        release_sha256_by_channel={},
        catalog_records=(),
        reranker=None,
        profile_provider=lambda _key: None,
        cell_fetcher=lambda _query: [],
        hcx_answerer=None,
        stack_runner=lambda *_args: routed,
        claim_query=None,
        evidence_first_statistics_shadow=True,
        release_bound_mode=True,
    )

    assert result["routed_targets"] == 3
    assert [row["target_id"] for row in result["stage_ledger"]] == [
        "article:test:level", "article:test:rate", "article:test:point",
    ]
    assert result["claim_query_selection"]["status"] == "FAMILY_TARGETS_PRESERVED"


def test_routed_target_ledger_records_channel_and_metadata_deltas(monkeypatch) -> None:
    class CounterChannel:
        def __init__(self) -> None:
            self.calls = 0

    class CounterProfile:
        def __init__(self) -> None:
            self.metadata_api_calls = 4

        def __call__(self, _table_key):
            return None

    channels = {name: CounterChannel() for name in ("official", "bm25", "dense")}
    profile = CounterProfile()

    monkeypatch.setattr(
        operational,
        "run_operational_l2",
        lambda *_args, **_kwargs: {"results": [{"article_idx": "article:test", "status": "L2_READY", "predictions": []}], "manifest": {}},
    )
    monkeypatch.setattr(
        operational,
        "select_primary_target",
        lambda rows: {"primary": rows[0], "group_key": ("article:test", 0, "출생아", "2025"), "value_span_ids": ["span:one"]},
    )

    def fake_retrieve(_claim, channel_map, **_kwargs):
        channel_map["official"].calls += 1
        channel_map["bm25"].calls += 2
        channel_map["dense"].calls += 3
        profile.metadata_api_calls += 2
        return (), {"paths": {"indicator:official": 0}}

    monkeypatch.setattr(operational, "retrieve_parallel", fake_retrieve)

    result = operational.run_new_articles_v2(
        [{"article_idx": "article:test", "title": "기사", "date": "2026-08-26", "article_text": "출생아 수는 10명이다."}],
        l2_api_key="",
        search_channels=channels,
        release_sha256_by_channel={},
        catalog_records=(),
        reranker=None,
        profile_provider=profile,
        cell_fetcher=lambda _query: [],
        hcx_answerer=None,
        stack_runner=lambda *_args: [{
            "article_idx": "article:test",
            "value_span_id": "span:one",
            "sentence_text": "출생아 수는 10명이다.",
            "value_text": "10",
            "value_unit": "명",
            "retrieval_fields": {"indicator": "출생아 수", "measurement_type": "LEVEL"},
        }],
        claim_query=None,
        evidence_first_statistics_shadow=True,
        release_bound_mode=True,
    )

    ledger = result["stage_ledger"][0]
    assert ledger["retrieval"]["calls"] == 6
    assert ledger["retrieval"]["channel_calls"] == {"official": 1, "bm25": 2, "dense": 3}
    assert ledger["candidate_membership"] == []
    assert ledger["metadata_api_calls"] == 2


def test_canonical_profile_lookup_delta_is_distinct_from_metadata_api_calls(monkeypatch) -> None:
    class Connection:
        def close(self) -> None:
            return None

    provider = CanonicalMetadataProfileProvider(dsn="postgresql://readonly", release_id="release:test")
    monkeypatch.setattr(provider, "_connect", lambda: Connection())
    monkeypatch.setattr(provider, "_release_attestation", lambda _connection: None)
    monkeypatch.setattr(provider, "_profile", lambda _table_key, _connection: None)

    snapshot = operational._snapshot_target_call_counts({}, provider)
    assert provider("101:DT_TEST") is None
    delta = operational._target_call_deltas(snapshot, {}, provider)

    assert provider.metadata_api_calls == 0
    assert provider.lookups == 1
    assert delta["metadata_api_calls"] == 0
    assert delta["metadata_lookups"] == 1


def test_public_target_receipt_prefers_top_level_membership_and_exposes_bounded_failure_delta(
    tmp_path: Path,
) -> None:
    _write_jsonl(
        tmp_path / "03_routed.jsonl",
        [{"article_idx": "article:test", "target_id": "target:one", "value_span_id": "span:one"}],
    )
    _write_jsonl(
        tmp_path / "04_stage_ledger.jsonl",
        [{
            "article_idx": "article:test",
            "target_id": "target:one",
            "resolution": "QDRANT_UNAVAILABLE",
            "candidate_membership": ["top-level-table"],
            "retrieval": {
                "candidate_membership": ["nested-table"],
                "calls": 6,
                "channel_calls": {"official": 1, "bm25": 2, "dense": 3},
            },
            "metadata_api_calls": 2,
            "metadata_lookups": 3,
            "failure": {"error_code": "QDRANT_UNAVAILABLE", "error_type": "RuntimeError"},
            "cell": {"status": "NO_CELL"},
        }],
    )

    status, receipts = service._live_status(tmp_path)

    assert status == "unverifiable"
    assert receipts[0]["retrieval"] == {
        "calls": 6,
        "channel_calls": {"official": 1, "bm25": 2, "dense": 3},
        "channel_audits": {},
        "candidate_table_keys": ["top-level-table"],
    }
    assert receipts[0]["metadata_binding"]["calls"] == 2
    assert receipts[0]["metadata_binding"]["lookups"] == 3
    assert receipts[0]["limitation"] == {
        "error_code": "QDRANT_UNAVAILABLE",
        "call_delta": {
            "retrieval": 6,
            "channel_calls": {"official": 1, "bm25": 2, "dense": 3},
            "metadata_api": 2,
            "metadata_lookups": 3,
        },
    }
