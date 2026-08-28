from __future__ import annotations

import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.news_verification.runtime.operational_live_adapters_v2 import (
    RequestScopedEncoder, RequestScopedProfileProvider, RequestScopedRetrievalCache,
)


class _Encoder:
    model_id = "bge-m3-ko"
    model_revision = "7074d66"
    vector_size = 2

    def __init__(self):
        self.calls = 0

    def __call__(self, text):
        self.calls += 1
        return [1.0, 0.0]


def test_profile_positive_and_negative_hits_are_request_local():
    calls = []

    def provider(key):
        calls.append(key)
        return None if key == "missing" else {"table_key": key, "release_id": "r"}

    cache = RequestScopedProfileProvider(provider)
    assert cache("ok") == cache("ok")
    assert cache("missing") is None
    assert cache("missing") is None
    assert calls == ["ok", "missing"]
    audit = cache.audit()
    assert audit["logical_lookups"] == 4
    assert audit["physical_lookups"] == 2
    assert audit["cache_hits"] == 2
    assert audit["negative_hits"] == 1


def test_encoder_uses_normalized_query_key_and_does_not_cache_invalid_vectors():
    inner = _Encoder()
    cache = RequestScopedEncoder(inner)
    assert cache("  출생아   수 ") == cache("출생아 수")
    assert inner.calls == 1
    assert cache.audit() == {"contract": "request-scoped-query-vector-cache-v1", "logical_calls": 2, "physical_calls": 1, "cache_hits": 1}


def test_retrieval_cache_reuses_only_same_identity_and_does_not_share_instances():
    cache = RequestScopedRetrievalCache()
    calls = []

    def callback():
        calls.append(1)
        return ([{"table_key": "t1"}], {"wall_ms": 2})

    first, _ = cache.get_or_call({"query": "a", "release": "r"}, callback)
    second, _ = cache.get_or_call({"query": "a", "release": "r"}, callback)
    other, _ = cache.get_or_call({"query": "b", "release": "r"}, callback)
    assert len(calls) == 2
    assert first == second == other
    assert cache.audit()["logical_calls"] == 3
    assert cache.audit()["physical_calls"] == 2
    assert cache.audit()["cache_hits"] == 1
