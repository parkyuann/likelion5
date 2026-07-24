from pathlib import Path

import pytest

from src.build_retrieval_gold_bootstrap import build_rows
from src.evaluate_table_retrieval import evaluate_candidates, load_adjudicated_matches
from src.source_scope_classifier import resolve_org_routing_candidates


def test_gold_bootstrap_keeps_reference_candidates_separate_from_empty_gold_fields():
    claims = [{
        "claim_id": "c1", "is_claim": True, "claim_class": "집계통계", "indicator_raw": "고용률",
        "sample_category_path": "노동 > 조사", "sample_seed_table_key": "101:SEED", "observations": [],
    }]
    hybrid = [{"claim_id": "c1", "selected_tables": [{"table_key": "101:CAND", "tbl_name": "후보 표"}]}]
    rows = build_rows(claims, hybrid, 1, 1)
    assert rows[0]["candidate_1_table_key"] == "101:CAND"
    assert rows[0]["gold_table_key"] == ""
    assert rows[0]["review_status"] == "pending"
    assert rows[0]["retrieval_split"] in {"dev", "test"}


def test_retrieval_metric_rejects_pending_gold(tmp_path: Path):
    path = tmp_path / "gold.csv"
    path.write_text("claim_id,gold_table_key,gold_match_status,review_status,reviewer\nc1,101:T1,MATCH,pending,human_reviewer\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="human-adjudicated MATCH"):
        load_adjudicated_matches(path)


def test_retrieval_metric_uses_adjudicated_table_key_only(tmp_path: Path):
    path = tmp_path / "gold.csv"
    path.write_text("claim_id,gold_table_key,gold_match_status,review_status,reviewer\nc1,101:T1,MATCH,adjudicated,human_reviewer\n", encoding="utf-8-sig")
    gold = load_adjudicated_matches(path)
    result = evaluate_candidates(gold, [{"claim_id": "c1", "selected_tables": [{"table_key": "101:T1"}]}])
    assert result["recall_at"]["1"] == 1.0
    assert result["mrr"] == 1.0


def test_final_metric_requires_explicit_opt_in_for_model_adjudication(tmp_path: Path):
    path = tmp_path / "gold.csv"
    path.write_text("claim_id,gold_table_key,gold_match_status,review_status,reviewer\nc1,101:T1,MATCH,adjudicated,codex_api_evidence\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="human-adjudicated MATCH"):
        load_adjudicated_matches(path)
    assert load_adjudicated_matches(path, allow_model_adjudication=True) == {"c1": "101:T1"}


def test_org_routing_reuses_verified_alias_without_fuzzy_candidates():
    catalog = {"101": "국가데이터처", "102": "한국은행"}
    candidates = resolve_org_routing_candidates("통계청", catalog)
    assert [(candidate.org_id, candidate.confidence) for candidate in candidates] == [("101", 1.0)]
    assert resolve_org_routing_candidates("통계청과 비슷한 기관", catalog) == []
