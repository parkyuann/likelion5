from __future__ import annotations

import sys
import types
from pathlib import Path


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

from src.develop.annual_requery_shadow_v1 import AnnualRequeryError
from src.news_verification.runtime import run_pipeline_operational_v2 as operational


def test_annual_requery_error_projects_bounded_reason_without_detail_leakage():
    reason = operational._bounded_annual_requery_reason(
        AnnualRequeryError("ANNUAL_CHANGE_NOT_UNIQUE:2")
    )
    assert reason == "ANNUAL_CHANGE_NOT_UNIQUE"
    assert ":2" not in reason

    unknown = operational._bounded_annual_requery_reason(
        AnnualRequeryError("unexpected internal detail")
    )
    assert unknown == "ANNUAL_REQUERY_NOT_EVALUATED"
    assert "internal detail" not in unknown
