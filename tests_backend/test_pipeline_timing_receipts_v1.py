from __future__ import annotations

import sys
import types

RUNTIME_ROOT = __import__("pathlib").Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas

from src.news_verification.runtime.operational_answer_v2 import build_evidence_packet, render_answer
from src.news_verification.runtime.operational_live_adapters_v2 import MonotonicTimingRecorder


def _packet():
    return build_evidence_packet(
        verdict="VERIFIED", claim_source={"sentence": "출생아 수는 1명"},
        binding_plan={"table": "sealed"}, official_cell={"cell": {"DT": "1"}},
        comparison={"verdict": "VERIFIED"}, limitation={},
        placeholders={"CLAIM": "출생아 수", "OFFICIAL_VALUE": "1", "LIMITATION": ""},
    )


def test_timing_recorder_uses_non_negative_monotonic_aggregate():
    recorder = MonotonicTimingRecorder()
    with recorder.span("parallel_channel"):
        pass
    snapshot = recorder.snapshot()
    assert snapshot["contract_version"] == "pipeline-timing-v1"
    assert snapshot["stages"]["parallel_channel"]["calls"] == 1
    assert snapshot["stages"]["parallel_channel"]["sum_ms"] >= 0
    assert snapshot["total_wall_ms"] >= snapshot["stages"]["parallel_channel"]["sum_ms"]


def test_deterministic_answer_default_does_not_call_hcx_or_expose_packet_query():
    class ExplodingHcx:
        def render(self, *_args, **_kwargs):
            raise AssertionError("deterministic mode must not call HCX")

    answer = render_answer(_packet(), ExplodingHcx())
    assert answer["answer_timing"]["calls"] == 1
    assert answer["answer_shadow"] == {"mode": "DETERMINISTIC_ONLY", "calls": 0}
    serialized = str(answer)
    assert "출생아 수는 1명" not in serialized
    assert "vector" not in serialized.lower()


def test_hcx_shadow_is_observational_and_keeps_deterministic_answer():
    class Hcx:
        def render(self, *_args, **_kwargs):
            return {"text": "모델이 임의로 바꾼 답변", "verdict": "REFUTED"}

    answer = render_answer(_packet(), Hcx(), mode="HCX_SHADOW_SYNC")
    assert answer["verdict"] == "VERIFIED"
    assert answer["answer_shadow"]["calls"] == 1
    assert "모델이 임의로" not in str(answer)
