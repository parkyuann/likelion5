from __future__ import annotations

import json
import hashlib
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend import develop_verify_service as service, search_adapter


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

from src.news_verification.runtime.operational_live_adapters_v2 import CountingAdapter
from src.news_verification.runtime.release_bound_live_adapters_v1 import ReleaseBoundDenseChannel
from src.news_verification.runtime import run_pipeline_operational_v2 as operational


RELEASE = "release-dense-boundary-v1"
COLLECTION = "dense-release-dense-boundary-v1"


def _authority() -> dict[str, bool]:
    return {
        "candidate_generation_only": True,
        "dimension_value_evidence_authority": False,
        "dimension_binding_authority": False,
        "dimension_completeness_authority": False,
        "binding_assignment_authority": False,
    }


def _point(index: int, score: float) -> dict[str, Any]:
    table_key = f"org:{index:04d}"
    return {
        "id": f"point-{index:04d}",
        "score": score,
        "payload": {
            "record_id": f"record-{index:04d}",
            "snapshot_id": RELEASE,
            "table_key": table_key,
            "field": "TITLE",
            "source_id": f"source-{index:04d}",
            "text_sha256": "a" * 64,
            "authority": _authority(),
        },
    }


class _SearchParams:
    def __init__(self, *, exact: bool) -> None:
        self.exact = exact


class _Models:
    SearchParams = _SearchParams

    class FieldCondition:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class MatchValue:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class MatchAny:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class Filter:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs


class _Client:
    def __init__(self, points: list[dict[str, Any]]) -> None:
        self.points = points
        self.windows: list[int] = []

    def get_collections(self, **kwargs: Any) -> dict[str, Any]:
        return {"collections": [{"name": COLLECTION}]}

    def get_collection(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "green",
            "config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}}},
        }

    def count(self, **kwargs: Any) -> dict[str, Any]:
        return {"count": len(self.points)}

    def query_points_groups(self, **kwargs: Any) -> dict[str, Any]:
        self.windows.append(int(kwargs["limit"]))
        points = self.points[: int(kwargs["limit"])]
        return {"groups": [{"id": point["payload"]["table_key"], "hits": [point]} for point in points]}


def _adapter(client: _Client) -> search_adapter.QdrantDenseAdapter:
    config = search_adapter.QdrantConfig(
        "http://qdrant",
        RELEASE,
        COLLECTION,
        1024,
        "b" * 64,
    )
    return search_adapter.QdrantDenseAdapter(config, client=client)


def test_open_cutoff_tie_at_search_window_max_drops_tied_candidates(monkeypatch: Any) -> None:
    monkeypatch.setattr(search_adapter, "qdrant_models", _Models)
    points = [_point(index, 1.0) for index in range(1000)]
    client = _Client(points)

    result = _adapter(client).search_grouped_by_table([0.0] * 1024)

    assert client.windows == [101, 1000]
    assert result["candidates"] == []
    assert result["window"] == []
    assert result["audit"] == {
        "boundary_status": "DROPPED_UNCLOSED_CUTOFF_TIE",
        "cutoff_score": 1.0,
        "observed_tied_count": 1000,
        "requested_window": 1000,
        "expansions": [1000],
    }


def test_open_cutoff_tie_returns_only_strictly_better_candidates(monkeypatch: Any) -> None:
    monkeypatch.setattr(search_adapter, "qdrant_models", _Models)
    points = [_point(index, 2.0) for index in range(99)]
    points.extend(_point(index, 1.0) for index in range(99, 1000))
    client = _Client(points)

    result = _adapter(client).search_grouped_by_table([0.0] * 1024)

    assert len(result["candidates"]) == 99
    assert all(candidate["score"] > 1.0 for candidate in result["candidates"])
    assert all(
        candidate["evidence"]["boundary_status"] == "DROPPED_UNCLOSED_CUTOFF_TIE"
        for candidate in result["candidates"]
    )
    assert result["audit"]["observed_tied_count"] == 901


def test_closed_cutoff_keeps_existing_top_100_behavior(monkeypatch: Any) -> None:
    monkeypatch.setattr(search_adapter, "qdrant_models", _Models)
    points = [_point(index, 1.0 - index / 1000) for index in range(101)]
    client = _Client(points)
    adapter = _adapter(client)

    result = adapter.search_grouped_by_table([0.0] * 1024)

    assert len(result["candidates"]) == 100
    assert result["candidates"][0]["table_key"] == "org:0000"
    assert result["candidates"][-1]["table_key"] == "org:0099"
    assert result["audit"]["boundary_status"] == "CLOSED"
    assert all(candidate["evidence"]["boundary_status"] == "CLOSED" for candidate in result["candidates"])


def test_dense_boundary_audit_survives_release_channel_counter_ledger_and_public_receipt(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    monkeypatch.setattr(search_adapter, "qdrant_models", _Models)
    adapter = _adapter(_Client([_point(index, 1.0) for index in range(1000)]))
    channel = CountingAdapter(ReleaseBoundDenseChannel(adapter, lambda _text: [0.0] * 1024))
    snapshot = operational._snapshot_target_call_counts({"dense": channel}, object())

    assert list(channel(SimpleNamespace(text="출생아 수"), ("TITLE",), 20)) == []
    deltas = operational._target_call_deltas(snapshot, {"dense": channel}, object())

    assert deltas["retrieval"]["channel_audits"] == {
        "dense": [{
            "query_sha256": hashlib.sha256("출생아 수".encode("utf-8")).hexdigest(),
            "boundary_status": "DROPPED_UNCLOSED_CUTOFF_TIE",
            "cutoff_score": 1.0,
            "observed_tied_count": 1000,
            "requested_window": 1000,
            "expansions": [1000],
        }],
    }
    (tmp_path / "03_routed.jsonl").write_text(
        json.dumps({"article_idx": "article:test", "target_id": "target:one", "value_span_id": "span:one"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "04_stage_ledger.jsonl").write_text(
        json.dumps({
            "article_idx": "article:test", "target_id": "target:one", "resolution": "NO_CANDIDATES",
            "retrieval": deltas["retrieval"], "candidate_membership": [],
            "metadata_api_calls": 0, "metadata_lookups": 0, "cell": {"status": "NO_CELL"},
        }) + "\n",
        encoding="utf-8",
    )

    receipts = service._target_receipts(tmp_path)

    assert receipts[0]["retrieval"]["candidate_table_keys"] == []
    assert receipts[0]["retrieval"]["channel_audits"] == deltas["retrieval"]["channel_audits"]


def test_parallel_dense_audit_events_preserve_closed_and_degraded_results_in_public_receipt(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    monkeypatch.setattr(search_adapter, "qdrant_models", _Models)
    closed_points = [_point(index, 2.0 - index / 1000) for index in range(101)]
    dropped_points = [_point(index, 1.0) for index in range(1000)]

    class RaceClient(_Client):
        def __init__(self) -> None:
            super().__init__(dropped_points)

        def query_points_groups(self, **kwargs: Any) -> dict[str, Any]:
            points = closed_points if float(kwargs["query"][0]) == 1.0 else dropped_points
            if points is closed_points:
                time.sleep(0.15)
            self.windows.append(int(kwargs["limit"]))
            selected = points[: int(kwargs["limit"])]
            return {"groups": [{"id": point["payload"]["table_key"], "hits": [point]} for point in selected]}

    adapter = _adapter(RaceClient())
    encoder = lambda text: [1.0] * 1024 if text == "closed" else [2.0] * 1024
    channel = CountingAdapter(ReleaseBoundDenseChannel(adapter, encoder))
    channels = {
        "official": lambda *_args: [],
        "bm25": lambda *_args: [],
        "dense": channel,
    }
    snapshot = operational._snapshot_target_call_counts(channels, object())

    candidates, _ = operational.retrieve_parallel(
        {"indicator": "closed", "item": "dropped", "sentence": ""},
        channels,
        release_sha256_by_channel={"official": "release", "bm25": "release", "dense": "release"},
    )
    deltas = operational._target_call_deltas(snapshot, channels, object())

    assert len(candidates) == 20
    assert channel.calls == 2
    assert [event["boundary_status"] for event in channel.audits_since(0)] == [
        "DROPPED_UNCLOSED_CUTOFF_TIE", "CLOSED",
    ]
    events = deltas["retrieval"]["channel_audits"]["dense"]
    assert {event["boundary_status"] for event in events} == {"CLOSED", "DROPPED_UNCLOSED_CUTOFF_TIE"}
    assert all(set(event) == {
        "query_sha256", "boundary_status", "cutoff_score", "observed_tied_count", "requested_window", "expansions",
    } for event in events)

    (tmp_path / "03_routed.jsonl").write_text(
        json.dumps({"article_idx": "article:test", "target_id": "target:race", "value_span_id": "span:race"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "04_stage_ledger.jsonl").write_text(
        json.dumps({
            "article_idx": "article:test", "target_id": "target:race", "resolution": "NO_CANDIDATES",
            "retrieval": deltas["retrieval"], "candidate_membership": [],
            "metadata_api_calls": 0, "metadata_lookups": 0, "cell": {"status": "NO_CELL"},
        }) + "\n",
        encoding="utf-8",
    )
    receipts = service._target_receipts(tmp_path)

    assert receipts[0]["retrieval"]["channel_audits"]["dense"] == events
    assert any(event["boundary_status"] == "DROPPED_UNCLOSED_CUTOFF_TIE" for event in events)
