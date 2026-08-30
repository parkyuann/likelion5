from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


RUNTIME_ROOT = Path(__file__).parents[1].resolve() / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

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

from src.news_verification.runtime import run_pipeline_operational_v2 as operational
from src.news_verification.runtime.r4c1_projection_v2 import CandidateProjection


def test_generic_top50_pins_raw_and_projection_profiles_without_reread(monkeypatch):
    table_key = "org:table"
    raw_profile = {"table_key": table_key, "marker": "raw"}
    provider_calls: list[str] = []

    def profile_provider(key: str):
        provider_calls.append(key)
        return raw_profile

    projection = CandidateProjection(
        table_key=table_key,
        assignments=(),
        abstained=(),
        projection_status="ABSTAIN",
        hold_reasons=("NO_COMPATIBLE_SERIES",),
        canonical_sha256="projection-sha",
    )
    monkeypatch.setattr(operational, "project_candidate_v2", lambda *args, **kwargs: projection)

    result = operational.resolve_top50(
        {"atoms": {}},
        [{"table_key": table_key}],
        profile_provider,
        profile_transform=lambda profile: {**profile, "marker": "projection"},
    )

    assert provider_calls == [table_key]
    assert result.pinned_raw_profiles[table_key]["marker"] == "raw"
    assert result.pinned_projection_profiles[table_key]["marker"] == "projection"
    with pytest.raises(TypeError):
        result.pinned_raw_profiles["other:table"] = raw_profile
    with pytest.raises(TypeError):
        result.pinned_projection_profiles[table_key]["marker"] = "changed"


def test_generic_top50_keeps_four_positional_constructor_compatible():
    result = operational.Top50Resolution(None, (), (), 0)

    assert result.pinned_raw_profiles == {}
    assert result.pinned_projection_profiles == {}
