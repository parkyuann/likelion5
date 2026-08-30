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

from src.news_verification.runtime import run_pipeline_operational_v2 as operational


def test_role_aware_update_preserves_inherited_period_source_sentence_when_source_id_missing(monkeypatch):
    def source_sentence_must_not_receive_missing_id(*args, **kwargs):
        raise AssertionError("source sentence lookup must be skipped for a missing source id")

    monkeypatch.setattr(operational, "source_sentence", source_sentence_must_not_receive_missing_id)
    row = {
        "article_sentence_id": 1,
        "source_region_sentence_id": None,
        "sentences": {
            0: "지난해 출생아 수가 증가했다.",
            1: "합계출산율도 2년 연속 반등했다.",
        },
    }

    source_id, source_text = operational._preserve_role_aware_sentence_inventory(
        row,
        "지난해 출생아 수가 증가했다. 합계출산율도 2년 연속 반등했다.",
        "합계출산율도 2년 연속 반등했다.",
    )

    assert source_id is None
    assert source_text == ""
    assert row["sentences"] == {
        0: "지난해 출생아 수가 증가했다.",
        1: "합계출산율도 2년 연속 반등했다.",
    }
    assert None not in row["sentences"]
