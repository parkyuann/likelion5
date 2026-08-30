from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import types


RUNTIME_ROOT = Path(__file__).parents[1].resolve() / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

# The focused L4 tests exercise deterministic normalization only.  The shared
# legacy normalizer imports pandas at module load, so keep this test independent
# of the optional deployment dataframe dependency.
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas

from src.news_verification.runtime import l4_field_normalization as l4


def _fake_normalize_time_ref(value, published_at):
    return {"지난해": "2025", "작년": "2025", "올해": "2026"}.get(str(value), str(value))


def test_l4_does_not_promote_duration_or_lower_bound_to_measurement_cell():
    compose_fields = l4.compose_fields
    duration = compose_fields(
        {
            "article_idx": "a",
            "article_sentence_id": 0,
            "sentence_text": "증가율은 15년 만에 가장 높았다.",
            "period_raw": "15년 만에",
            "period_source": "LOCAL",
            "indicator_label": "출생아 수 증가율",
            "value_unit": "%",
            "value_text": "6.7",
        },
        published_at="2026-08-26",
    )
    lower_bound = compose_fields(
        {
            "article_idx": "a",
            "article_sentence_id": 0,
            "sentence_text": "증가율은 2007년 이후 가장 높았다.",
            "period_raw": "2007년 이후",
            "period_source": "LOCAL",
            "indicator_label": "출생아 수 증가율",
            "value_unit": "%",
            "value_text": "6.7",
        },
        published_at="2026-08-26",
    )
    for row in (duration, lower_bound):
        period = row["retrieval_fields"]["period"]
        assert period["measurement"] == {"raw": "", "absolute": ""}
        assert row["retrieval_fields"]["period_absolute"] == ""
        assert period["period_expression_role"] == "DURATION_OR_RANGE"


def test_l4_replaces_unanchored_synthetic_year_with_bounded_article_scope_period(monkeypatch):
    monkeypatch.setattr(l4, "normalize_time_ref", _fake_normalize_time_ref)
    assignments = [
        {
            "article_idx": "a",
            "article_sentence_id": 0,
            "sentence_text": "지난해 출생아 수가 1년 전보다 6.7% 늘었다.",
            "period_raw": "지난해",
            "period_source": "LOCAL",
            "indicator_label": "출생아 수 증가율",
            "value_unit": "%",
            "value_text": "6.7",
        },
        {
            "article_idx": "a",
            "article_sentence_id": 1,
            "sentence_text": "합계출산율도 2년 연속 반등해 0.80명을 기록했다.",
            "period_raw": "2022년",
            "period_source": "LOCAL",
            "indicator_label": "합계출산율",
            "value_unit": "명",
            "value_text": "0.80",
        },
    ]
    rows = l4.compose_all(assignments, {"a": "2026-08-26"})
    second = rows[1]
    fields = second["retrieval_fields"]
    assert fields["period_raw"] == "지난해"
    assert fields["period_absolute"] == "2025"
    assert fields["period"]["measurement"]["absolute"] == "2025"
    assert second["period_source"] == "INHERITED_ARTICLE_SCOPE"
    assert second["period_inheritance_provenance"]["source_sentence_id"] == 0
    assert "2022" not in str(fields)


def test_l4_does_not_inherit_when_target_sentence_has_conflicting_local_period(monkeypatch):
    monkeypatch.setattr(l4, "normalize_time_ref", _fake_normalize_time_ref)
    assignments = [
        {
            "article_idx": "a",
            "article_sentence_id": 0,
            "sentence_text": "지난해 출생아 수가 늘었다.",
            "period_raw": "지난해",
            "period_source": "LOCAL",
            "indicator_label": "출생아 수",
            "value_unit": "명",
            "value_text": "100",
        },
        {
            "article_idx": "a",
            "article_sentence_id": 1,
            "sentence_text": "올해 합계출산율은 0.80명이다.",
            "period_raw": "2022년",
            "period_source": "LOCAL",
            "indicator_label": "합계출산율",
            "value_unit": "명",
            "value_text": "0.80",
        },
    ]
    second = l4.compose_all(assignments, {"a": "2026-08-26"})[1]
    assert second["retrieval_fields"]["period_raw"] == ""
    assert second["retrieval_fields"]["period_absolute"] == ""
    assert "2022" not in str(second["retrieval_fields"])


def test_operational_preserves_indicator_families_and_uses_level_unit_for_annual_change_projection():
    repo_root = Path(__file__).parents[1].resolve()
    script = f"""
import importlib
import sys
import types

sys.path.insert(1, {str(repo_root)!r})
requests = types.ModuleType("requests")
requests.RequestException = RuntimeError
requests.get = lambda *args, **kwargs: None
requests.post = lambda *args, **kwargs: None
requests.Session = lambda: None
sys.modules["requests"] = requests
pandas = types.ModuleType("pandas")
pandas.Series = object
pandas.DataFrame = object
sys.modules["pandas"] = pandas

module = importlib.import_module("src.news_verification.runtime.run_pipeline_operational_v2")
rows = [
    {{
        "article_idx": "a", "article_sentence_id": 0, "value_span_id": "s0",
        "indicator_label": "출생아 수 증가율",
        "retrieval_fields": {{
            "indicator": "출생아 수 증가율", "measurement_type": "CHANGE_RATE",
            "period_absolute": "2025",
            "period": {{"measurement": {{"absolute": "2025"}}}},
        }},
    }},
    {{
        "article_idx": "a", "article_sentence_id": 1, "value_span_id": "s1",
        "indicator_label": "합계출산율",
        "retrieval_fields": {{
            "indicator": "합계출산율", "measurement_type": "LEVEL",
            "period_absolute": "2025",
            "period": {{"measurement": {{"absolute": "2025"}}}},
        }},
    }},
]
groups = module._evidence_family_groups(rows)
assert len(groups) == 2
row = {{
    "article_idx": "a", "article_sentence_id": 0,
    "sentence_text": "지난해 출생아 수 증가율은 6.7%였다.",
    "value_span_id": "a:0-3", "value_text": "6.7", "value_unit": "%",
    "article_date": "2026-08-26",
    "retrieval_fields": {{
        "indicator": "출생아 수 증가율", "measurement_type": "CHANGE_RATE",
        "period_raw": "지난해", "period_absolute": "2025",
        "period": {{"measurement": {{"raw": "지난해", "absolute": "2025"}}, "baseline": {{"absolute": "2024"}}}},
    }},
}}
core = module._annual_change_projection_core(row)
assert core.atoms["unit"].status == "UNKNOWN"
assert core.atoms["unit"].surface == ""
assert core.atoms["unit"].provenance["claim_unit"] == "%"
print("ANNUAL_FAMILY_PROJECTION_OK")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RUNTIME_ROOT) + os.pathsep + str(repo_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=RUNTIME_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "ANNUAL_FAMILY_PROJECTION_OK" in result.stdout
