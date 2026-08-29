from __future__ import annotations

import time
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

from src.news_verification.runtime.operational_retrieval_v2 import (
    build_context_query_register,
    build_corrective_query_register,
    build_query_register,
    retrieve_parallel,
)


class _Channel:
    def __init__(self, *, fail: BaseException | None = None, delay: float = 0.0, mismatch: bool = False):
        self.fail = fail
        self.delay = delay
        self.mismatch = mismatch
        self.calls: list[str] = []

    def __call__(self, query, fields, top_k):
        self.calls.append(query.query_id)
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return [{
            "record_id": f"{query.query_id}:record",
            "table_key": "table-1",
            "field": fields[0],
            "score": 1.0,
            **({"release_sha256": "wrong"} if self.mismatch else {}),
        }]


def _claim(**extra):
    return {"indicator": "출생아 수", "item": "출생아 수", "sentence": "출생아 수는 10명이다.", **extra}


def test_default_register_is_exactly_six_paths_and_sentence_official_is_not_submitted():
    register = build_query_register(_claim())
    jobs = {f"{query.query_id}:{channel}" for query in register for channel in query.channels}
    assert jobs == {
        "indicator:official", "indicator:bm25", "indicator:dense",
        "item:bm25", "item:dense", "sentence:dense",
    }
    assert "sentence:official" not in jobs

    channels = {name: _Channel() for name in ("official", "bm25", "dense")}
    _, audit = retrieve_parallel(_claim(), channels, release_sha256_by_channel={name: "r" for name in channels})
    assert audit["disabled_paths"][0]["path"] == "sentence:official"
    assert "official" not in channels["official"].calls
    assert "sentence" not in channels["official"].calls


def test_source_and_corrective_registers_are_separate_and_path_errors_are_isolated():
    assert {query.query_id for query in build_query_register(_claim(source_terms=[{"role": "report", "text": "통계청"}]))} == {
        "indicator", "item", "sentence",
    }
    source_register = build_context_query_register(_claim(source_terms=[{"role": "report", "text": "통계청"}]))
    assert any(query.query_id.startswith("source_report") for query in source_register)
    corrective_register = build_corrective_query_register({"corrective_terms": [{"case_id": "c", "role": "item", "text": "월별 출생아 수"}]})
    assert {query.query_id for query in corrective_register} == {"corrective_c_0"}

    channels = {
        "official": _Channel(fail=TimeoutError("late")),
        "bm25": _Channel(),
        "dense": _Channel(),
    }
    candidates, audit = retrieve_parallel(_claim(), channels, release_sha256_by_channel={name: "r" for name in channels})
    assert candidates
    assert any(value == "FAILED_TIMEOUT" for value in audit["path_status"].values())
    assert audit["successful_path_count"] > 0

    _, context_audit = retrieve_parallel(
        _claim(source_terms=[{"role": "report", "text": "통계청"}]),
        {name: _Channel() for name in ("official", "bm25", "dense")},
        release_sha256_by_channel={name: "r" for name in ("official", "bm25", "dense")},
        register_kind="context",
    )
    assert context_audit["query_register_kind"] == "context"
    assert all(key.startswith("source_") for key in context_audit["path_status"])


def test_malformed_or_release_mismatch_fails_closed_and_completion_order_is_deterministic():
    bad = {
        "official": _Channel(mismatch=True),
        "bm25": _Channel(delay=0.01),
        "dense": _Channel(delay=0.0),
    }
    candidates, audit = retrieve_parallel(_claim(), bad, release_sha256_by_channel={name: "r" for name in bad})
    assert candidates
    assert any(value == "FAILED_CONTRACT" for value in audit["path_status"].values())
    assert "RETRIEVAL_RELEASE_MISMATCH" in set(audit["path_errors"].values())

    first, first_audit = retrieve_parallel(_claim(), {name: _Channel(delay=(0.01 if name == "official" else 0.0)) for name in ("official", "bm25", "dense")}, release_sha256_by_channel={"official": "r", "bm25": "r", "dense": "r"})
    second, second_audit = retrieve_parallel(_claim(), {name: _Channel(delay=(0.0 if name == "official" else 0.01)) for name in ("official", "bm25", "dense")}, release_sha256_by_channel={"official": "r", "bm25": "r", "dense": "r"})
    assert [candidate.table_key for candidate in first] == [candidate.table_key for candidate in second]
    assert first_audit["retrieval_semantic_sha256"] == second_audit["retrieval_semantic_sha256"]


def test_all_path_transport_failure_is_retryable_insufficiency_without_local_correction():
    channels = {name: _Channel(fail=ConnectionError("down")) for name in ("official", "bm25", "dense")}
    candidates, audit = retrieve_parallel(_claim(), channels, release_sha256_by_channel={name: "r" for name in channels})
    assert candidates == ()
    assert audit["all_paths_failed"] is True
    assert audit["failed_path_count"] == len(audit["path_status"])
    assert "corrective" not in audit
