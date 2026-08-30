from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import verification_checkpoint_store as store


def test_all_484_options_are_reachable_and_public_page_has_no_prefill_or_internal_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    (work / "articles.jsonl").write_text(json.dumps({"article_idx": "a", "article_text": "본문"}) + "\n", encoding="utf-8")
    question_id = "cq-page-test"
    options = [
        {"option_id": f"co-{index:03d}", "display_label": f"지역 {index:03d}", "description": "선택지", "applicability": [{"axis_id": "hidden"}]}
        for index in range(484)
    ]
    checkpoint = store.create(
        workdir=work, article_body_sha256="a" * 64, title="제목", article_id="a",
        clarification_history=[], runtime_fingerprint="r" * 64, config_sha256="c" * 64,
        resume_from_stage="retrieval",
        clarification_plan={"contract_version": "clarification-plan-v2", "question": {"id": question_id, "role": "region"}},
        option_bundle={"contract_version": "clarification-option-bundle-v2", "question_id": question_id, "role": "region", "options": options},
    )
    seen = []
    cursor = None
    first_cursor = None
    while True:
        page = store.read_option_page(checkpoint.token, question_id=question_id, cursor=cursor, limit=20)
        seen.extend(item["id"] for item in page["options"])
        if first_cursor is None:
            first_cursor = page["page"]["next_cursor"]
        cursor = page["page"]["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 484
    assert len(set(seen)) == 484
    assert "semantic_value" not in page["options"][0]
    assert "axis_id" not in page["options"][0]
    filtered = store.read_option_page(checkpoint.token, question_id=question_id, query="지역 48", limit=20)
    assert filtered["page"]["total"] == 4
    with pytest.raises(store.CheckpointError, match="CLARIFICATION_CURSOR_MISMATCH"):
        store.read_option_page(checkpoint.token, question_id=question_id, query="서울", cursor=first_cursor, limit=20)


def test_option_token_tamper_is_not_accepted_as_a_public_option(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    (work / "articles.jsonl").write_text(json.dumps({"article_idx": "a", "article_text": "본문"}) + "\n", encoding="utf-8")
    checkpoint = store.create(
        workdir=work, article_body_sha256="a" * 64, title="제목", article_id="a",
        clarification_history=[], runtime_fingerprint="r" * 64, config_sha256="c" * 64,
        resume_from_stage="retrieval",
        clarification_plan={"contract_version": "clarification-plan-v2", "question": {"id": "cq-tamper", "role": "region"}},
        option_bundle={"contract_version": "clarification-option-bundle-v2", "question_id": "cq-tamper", "role": "region", "options": [{"option_id": "co-good", "display_label": "전국"}]},
    )
    result = store.read_option_page(checkpoint.token, question_id="cq-tamper")
    assert result["options"] == [{"id": "co-good", "label": "전국", "description": "", "applicable_candidate_count": 0}]
    with pytest.raises(store.CheckpointError, match="CLARIFICATION_OPTION_INVALID"):
        store.consume(
            checkpoint.token, article_body_sha256="a" * 64, title="제목",
            clarification_history=[{"question_id": "cq-tamper", "role": "region", "value": "전국", "option_id": "co-forged"}],
            runtime_fingerprint="r" * 64, config_sha256="c" * 64,
            expected_question_id="cq-tamper", expected_role="region",
        )
    assert json.loads((checkpoint.root / "checkpoint.json").read_text(encoding="utf-8"))["status"] == "ACTIVE"


@pytest.mark.parametrize("input_mode", ["OPTIONS", "SEARCHABLE_OPTIONS"])
def test_direct_input_contract_and_one_answer_per_generation(monkeypatch, tmp_path, input_mode):
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    work = tmp_path / "work-direct"
    (work / "out").mkdir(parents=True)
    (work / "articles.jsonl").write_text(json.dumps({"article_idx": "a", "article_text": "본문"}) + "\n", encoding="utf-8")
    cp = store.create(
        workdir=work, article_body_sha256="a" * 64, title="제목", article_id="a",
        clarification_history=[], runtime_fingerprint="r" * 64, config_sha256="c" * 64,
        resume_from_stage="retrieval",
        clarification_plan={"contract_version": "clarification-plan-v2", "question": {
            "id": "cq-direct", "role": "indicator", "input_mode": input_mode, "allow_direct_input": True,
        }},
        option_bundle={"contract_version": "clarification-option-bundle-v2", "question_id": "cq-direct", "role": "indicator", "options": []},
    )
    resumed = store.consume(
        cp.token, article_body_sha256="a" * 64, title="제목",
        clarification_history=[{"question_id": "cq-direct", "role": "indicator", "value": "합계출산율"}],
        runtime_fingerprint="r" * 64, config_sha256="c" * 64,
        expected_question_id="cq-direct", expected_role="indicator",
    )
    assert resumed.metadata["status"] == "RESUMING"

    with pytest.raises(store.CheckpointError, match="CLARIFICATION_ANSWER_CARDINALITY_INVALID"):
        store.consume(
            cp.token, article_body_sha256="a" * 64, title="제목",
            clarification_history=[
                {"question_id": "cq-direct", "role": "indicator", "value": "합계출산율"},
                {"question_id": "cq-other", "role": "region", "value": "전국"},
            ], runtime_fingerprint="r" * 64, config_sha256="c" * 64,
        )


def test_options_only_requires_option_and_question_role_match(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    work = tmp_path / "work-only"
    (work / "out").mkdir(parents=True)
    (work / "articles.jsonl").write_text(json.dumps({"article_idx": "a", "article_text": "본문"}) + "\n", encoding="utf-8")
    cp = store.create(
        workdir=work, article_body_sha256="a" * 64, title="제목", article_id="a",
        clarification_history=[], runtime_fingerprint="r" * 64, config_sha256="c" * 64,
        resume_from_stage="retrieval", clarification_plan={"contract_version": "clarification-plan-v2", "question": {
            "id": "cq-only", "role": "region", "input_mode": "OPTIONS", "allow_direct_input": False,
        }}, option_bundle={"contract_version": "clarification-option-bundle-v2", "question_id": "cq-only", "role": "region", "options": [{"option_id": "co-all", "display_label": "전국"}]},
    )
    with pytest.raises(store.CheckpointError, match="CLARIFICATION_OPTION_REQUIRED"):
        store.consume(cp.token, article_body_sha256="a" * 64, title="제목", clarification_history=[{"question_id": "cq-only", "role": "region", "value": "전국"}], runtime_fingerprint="r" * 64, config_sha256="c" * 64)
    with pytest.raises(store.CheckpointError, match="CLARIFICATION_QUESTION_MISMATCH"):
        store.consume(cp.token, article_body_sha256="a" * 64, title="제목", clarification_history=[{"question_id": "cq-wrong", "role": "region", "value": "전국", "option_id": "co-all"}], runtime_fingerprint="r" * 64, config_sha256="c" * 64)
