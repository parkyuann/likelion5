from __future__ import annotations

import sys
import time
import types

from pathlib import Path

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas

from src.news_verification.runtime.run_pipeline_operational_v2 import _speculative_clarification_plan
from src.news_verification.runtime.operational_live_adapters_v2 import CountingAdapter, RequestScopedProfileProvider


def test_speculative_probe_has_two_queries_top30_profiles_and_no_cell_or_hcx():
    calls = {"official": 0, "bm25": 0, "dense": 0, "profiles": 0}

    class NativeChannel:
        def speculative(self, query, _fields, top_k, *, timeout_seconds):
            assert timeout_seconds > 0
            return [{"record_id": "r1", "table_key": "101:T1", "field": "TITLE", "score": 1.0}]

    channels = {name: NativeChannel() for name in ("official", "bm25", "dense")}

    class NativeProfile:
        def speculative(self, key, *, timeout_seconds):
            calls["profiles"] += 1
            return {"table_key": key, "meta_status": "READY", "profile_sha256": "a" * 64, "items": [{"itm_id": "I1", "itm_nm": "출생아 수"}]}

    plan, audit = _speculative_clarification_plan(
        {"target_id": "a:1", "sentence_text": "지난해 출생아 수는 1만 명", "source_text": "국가데이터처"},
        article_text="지난해 출생아 수는 1만 명", search_channels=channels,
        release_sha256_by_channel={"official": "o", "bm25": "b", "dense": "d"},
        profile_provider=NativeProfile(), retrieval_cache=None, deadline_ms=2500,
    )
    assert plan is not None
    assert plan["speculative"] is True
    assert audit["queries_attempted"] <= 2
    assert audit["union_top_k"] == 30
    assert audit["profile_limit"] == 30
    assert audit["retry_limit"] == 0
    assert audit["cell_api_calls"] == 0
    assert audit["hcx_answer_calls"] == 0
    assert plan["question"]["options"][0]["label"] == "출생아 수"


def test_nested_unsupported_wrappers_submit_nothing_and_call_no_inner_transport():
    calls = {"channel": 0, "profile": 0}

    class UnsupportedChannel:
        def __call__(self, *_args, **_kwargs):
            calls["channel"] += 1
            return []

    class UnsupportedProfile:
        release_id = "release-1"
        def __call__(self, _key):
            calls["profile"] += 1
            return None

    started = time.monotonic()
    plan, audit = _speculative_clarification_plan(
        {"target_id": "a:slow", "sentence_text": "지난해 수치는 1만 명"},
        article_text="지난해 수치는 1만 명",
        search_channels={name: CountingAdapter(UnsupportedChannel()) for name in ("official", "bm25", "dense")},
        release_sha256_by_channel={"official": "o", "bm25": "b", "dense": "d"},
        profile_provider=RequestScopedProfileProvider(UnsupportedProfile()), retrieval_cache=None, deadline_ms=80,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.25
    assert plan is not None
    assert audit["status"] == "DEADLINE_EXCEEDED"
    assert audit["cell_api_calls"] == audit["hcx_answer_calls"] == 0
    assert audit["executor_tasks_submitted"] == 0
    assert audit["native_timeout"]["supported_channels"] == []
    assert audit["native_timeout"]["profile_supported"] is False
    assert calls == {"channel": 0, "profile": 0}


def test_nested_native_wrappers_forward_remaining_timeout_and_build_options():
    observed = {"channel_timeouts": [], "profile_timeouts": []}

    class NativeChannel:
        def __call__(self, *_args, **_kwargs):
            raise AssertionError("normal transport path must not be used")
        def speculative(self, _query, _fields, _top_k, *, timeout_seconds):
            observed["channel_timeouts"].append(timeout_seconds)
            return [{"record_id": "r1", "table_key": "101:T1", "field": "TITLE", "score": 1.0}]

    class NativeProfile:
        release_id = "release-1"
        def __call__(self, _key):
            raise AssertionError("normal profile path must not be used")
        def speculative(self, key, *, timeout_seconds):
            observed["profile_timeouts"].append(timeout_seconds)
            return {"table_key": key, "release_id": "release-1", "meta_status": "READY", "profile_sha256": "a" * 64, "items": [{"itm_id": "I1", "itm_nm": "합계출산율"}]}

    plan, audit = _speculative_clarification_plan(
        {"target_id": "a:native", "sentence_text": "지난해 수치는 0.8명"},
        article_text="지난해 수치는 0.8명",
        search_channels={name: CountingAdapter(NativeChannel()) for name in ("official", "bm25", "dense")},
        release_sha256_by_channel={"official": "o", "bm25": "b", "dense": "d"},
        profile_provider=RequestScopedProfileProvider(NativeProfile()), retrieval_cache=None, deadline_ms=500,
    )
    assert audit["status"] == "OPTIONS_READY"
    assert audit["native_timeout"]["supported_channels"] == ["bm25", "dense", "official"]
    assert audit["native_timeout"]["profile_supported"] is True
    assert observed["channel_timeouts"] and all(0 < value <= 0.5 for value in observed["channel_timeouts"])
    assert observed["profile_timeouts"] and all(0 < value <= 0.5 for value in observed["profile_timeouts"])
    assert plan["question"]["options"][0]["label"] == "합계출산율"
