from __future__ import annotations

import json
from pathlib import Path

from backend.develop_verify_service import _pre_live_clarification_plan


def _routed(root: Path, row: dict) -> None:
    (root / "03_routed.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")


def test_relative_period_without_date_asks_after_layers_without_live():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _routed(root, {"article_idx": "a", "retrieval_fields": {"period_raw": "지난 4월"}})
        plan = _pre_live_clarification_plan(root, body="지난 4월 출생아는 1명", article_date="")
    assert plan is not None
    assert plan["question"]["role"] == "article_date"
    assert plan["resume_from_stage"] == "layers"
    assert plan["invalidated_stages"] == ["layers", "retrieval", "binding", "cell", "answer"]


def test_relative_period_without_routed_projection_still_asks_for_article_date():
    """A partial routing result must never hide an unfixed relative period."""
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan = _pre_live_clarification_plan(
            root,
            body="지난 4월 출생아는 2만4521명이었다.",
            article_date="",
        )
    assert plan is not None
    assert plan["reason"] == "ARTICLE_DATE_REQUIRED_FOR_RELATIVE_PERIOD"
    assert plan["question"]["role"] == "article_date"
    assert plan["resume_from_stage"] == "layers"


def test_absolute_period_does_not_ask_for_article_date():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _routed(root, {"article_idx": "a", "retrieval_fields": {"period_raw": "2026년 4월", "indicator": "출생아 수"}})
        plan = _pre_live_clarification_plan(root, body="2026년 4월 출생아 수는 1명", article_date="")
    assert plan is None


def test_missing_indicator_is_explicit_speculative_question_not_fabricated_value():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _routed(root, {"article_idx": "a", "retrieval_fields": {"indicator": "UNKNOWN"}})
        plan = _pre_live_clarification_plan(root, body="지난해 0.80명을 기록했다", article_date="2026-08-26")
    assert plan is not None
    assert plan["question"]["role"] == "indicator"
    assert plan["speculative"] is True
    assert plan["question"]["options"] == []
    assert plan["question"]["allow_direct_input"] is True
