from __future__ import annotations

import sys
import types
from pathlib import Path


RUNTIME_ROOT = Path(__file__).parents[1].resolve() / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

# Keep this deterministic L4 regression independent of the optional dataframe
# dependency imported by the legacy time normalizer.
if "pandas" not in sys.modules:
    pandas = types.ModuleType("pandas")
    pandas.Series = object
    pandas.DataFrame = object
    sys.modules["pandas"] = pandas

from src.news_verification.runtime import l4_field_normalization as l4


def test_unanchored_hcx_period_inherits_one_adjacent_source_backed_period(monkeypatch):
    monkeypatch.setattr(
        l4,
        "normalize_time_ref",
        lambda value, published_at: {"지난해": "2025"}.get(str(value), str(value)),
    )
    rows = l4.compose_all(
        [
            {
                "article_idx": "article:test",
                "article_sentence_id": 0,
                "sentence_text": "지난해 출생아 수가 증가했다.",
                "period_raw": "지난해",
                "period_source": "LOCAL",
                "indicator_label": "출생아 수",
                "value_text": "100",
                "value_unit": "명",
            },
            {
                "article_idx": "article:test",
                "article_sentence_id": 1,
                "sentence_text": "합계출산율은 0.80명이다.",
                "period_raw": "최근",
                "period_source": "LOCAL",
                "indicator_label": "합계출산율",
                "value_text": "0.80",
                "value_unit": "명",
            },
        ],
        {"article:test": "2026-08-26"},
    )

    second = rows[1]
    assert second["retrieval_fields"]["period_raw"] == "지난해"
    assert second["retrieval_fields"]["period_absolute"] == "2025"
    assert second["period_source"] == "INHERITED_ARTICLE_SCOPE"
    assert second["period_inheritance_provenance"] == {
        "rule_id": "adjacent-article-period-inheritance-v1",
        "source_sentence_id": 0,
        "source_period_raw": "지난해",
        "target_sentence_id": 1,
        "conflict_check": "NO_LOCAL_MEASUREMENT_PERIOD",
    }


def test_duration_expression_preserves_receipt_and_inherits_adjacent_measurement_period(monkeypatch):
    monkeypatch.setattr(
        l4,
        "normalize_time_ref",
        lambda value, published_at: {"지난해": "2025"}.get(str(value), str(value)),
    )
    rows = l4.compose_all(
        [
            {
                "article_idx": "article:test",
                "article_sentence_id": 0,
                "sentence_text": "지난해 출생아 수가 증가했다.",
                "period_raw": "지난해",
                "period_source": "LOCAL",
                "indicator_label": "출생아 수",
                "value_text": "100",
                "value_unit": "명",
            },
            {
                "article_idx": "article:test",
                "article_sentence_id": 1,
                "sentence_text": "합계출산율도 2년 연속 반등해 0.80명을 기록했다.",
                "period_raw": "2년 연속",
                "period_source": "LOCAL",
                "indicator_label": "합계출산율",
                "value_text": "0.80",
                "value_unit": "명",
            },
        ],
        {"article:test": "2026-08-26"},
    )

    second = rows[1]
    assert second["retrieval_fields"]["period"]["measurement"] == {
        "raw": "지난해",
        "absolute": "2025",
    }
    assert second["retrieval_fields"]["period"]["period_expression_role"] == "DURATION_OR_RANGE"
    assert second["field_provenance"]["period_expression_provenance"] == {
        "expression_role": "DURATION_OR_RANGE",
        "original_period_raw": "2년 연속",
        "original_period_source": "LOCAL",
        "original_sentence_id": 1,
        "cell_period_resolution": "BOUNDED_ADJACENT_SOURCE_PERIOD",
    }
    assert second["period_source"] == "INHERITED_ARTICLE_SCOPE"


def test_duration_expression_does_not_inherit_across_local_period_conflict(monkeypatch):
    monkeypatch.setattr(
        l4,
        "normalize_time_ref",
        lambda value, published_at: {"지난해": "2025"}.get(str(value), str(value)),
    )
    rows = l4.compose_all(
        [
            {
                "article_idx": "article:test",
                "article_sentence_id": 0,
                "sentence_text": "지난해 출생아 수가 증가했다.",
                "period_raw": "지난해",
                "period_source": "LOCAL",
                "indicator_label": "출생아 수",
                "value_text": "100",
                "value_unit": "명",
            },
            {
                "article_idx": "article:test",
                "article_sentence_id": 1,
                "sentence_text": "올해 합계출산율도 2년 연속 반등했다.",
                "period_raw": "2년 연속",
                "period_source": "LOCAL",
                "indicator_label": "합계출산율",
                "value_text": "0.80",
                "value_unit": "명",
            },
        ],
        {"article:test": "2026-08-26"},
    )

    second = rows[1]
    assert second["retrieval_fields"]["period"]["measurement"] == {
        "raw": "",
        "absolute": "",
    }
    assert second["period_source"] == "LOCAL"
    assert second["field_provenance"]["period_expression_provenance"]["cell_period_resolution"] == "UNRESOLVED"


def test_non_cell_expression_accepts_hcx_compound_and_duration_only_variants():
    variants = (
        (
            "2007년 이후",
            "증가율은 2007년 이후 18년 만에 가장 높았다.",
        ),
        (
            "2007년 이후 18년 만에",
            "증가율은 2007년 이후 18년 만에 가장 높았다.",
        ),
        (
            "18년 만에",
            "증가율은 2007년 이후 18년 만에 가장 높았다.",
        ),
        (
            "최근 5년",
            "최근 5년간 증가 흐름을 보였다.",
        ),
    )

    for period_raw, sentence in variants:
        assert l4._period_is_non_cell_expression(period_raw, sentence)


def test_four_digit_year_remains_an_annual_measurement_period():
    row = l4.compose_fields(
        {
            "article_idx": "article:test",
            "article_sentence_id": 0,
            "sentence_text": "2025년에는 합계출산율 0.80명을 기록했다.",
            "period_raw": "2025년",
            "period_source": "LOCAL",
            "indicator_label": "합계출산율",
            "value_text": "0.80",
            "value_unit": "명",
        },
        published_at="2026-08-26",
    )

    assert not l4._period_is_non_cell_expression("2025년", "2025년에는 합계출산율 0.80명을 기록했다.")
    assert row["retrieval_fields"]["period"] == {
        "measurement": {"raw": "2025년", "absolute": "2025"},
        "baseline": {"raw": "", "absolute": ""},
        "basis": "NONE",
        "period_expression_role": "MEASUREMENT",
    }
