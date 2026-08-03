from src.develop.build_article_hcx_semantic_labeling_scaffold import (
    build_semantic_labeling_scaffold,
)
from src.develop.article_claim_pipeline import build_claim_skeleton_candidate_catalog


def test_semantic_labeling_scaffold_preserves_gold_and_marks_unreviewed_roles():
    gold = [{
        "fixture_id": "1-01",
        "article_idx": "1",
        "eligibility": "KOSIS_CANDIDATE",
        "indicator_norm": "보험사 연체율",
        "measurement_type": "LEVEL",
        "value_text": "1.46%",
        "value_sentence_id": 0,
        "dimension_texts": [],
    }]
    articles = {
        "1": {
            "title": "연체율",
            "article_text": "개인사업자의 보험사 연체율은 1.46%로 집계됐다.",
        }
    }

    rows, report = build_semantic_labeling_scaffold(gold, articles)
    draft = rows[0]["semantic_role_labeling"]

    assert {key: gold[0][key] for key in gold[0]} == gold[0]
    assert draft["adjudication_status"] == "DRAFT_NEEDS_HUMAN_ADJUDICATION"
    assert draft["field_status"]["indicator"] == "DRAFT_CANDIDATE_COMPLETE"
    assert draft["field_status"]["population"] == "NEEDS_HUMAN_LABEL"
    assert draft["field_status"]["item"] == "NEEDS_HUMAN_LABEL"
    assert draft["population_evidence_candidate_ids_draft"] == []
    assert draft["item_evidence_candidate_ids_draft"] == []
    assert report["target_value_candidate_exact_rows"] == 1
    assert report["indicator_candidate_complete_rows"] == 1
    assert report["population_labels_adjudicated"] == 0
    assert report["item_labels_adjudicated"] == 0
    catalog = build_claim_skeleton_candidate_catalog(
        articles["1"]["article_text"],
        include_semantic_evidence=True,
    )
    evidence_by_id = {
        item["semantic_evidence_candidate_id"]: item["text"]
        for item in catalog["semantic_evidence_candidates"]
    }
    assert {
        evidence_by_id[candidate_id]
        for candidate_id in draft["indicator_evidence_candidate_ids_draft"]
    } == {"보험사", "연체율"}


def test_semantic_labeling_scaffold_matches_existing_dimension_gold_only():
    gold = [{
        "fixture_id": "2-01",
        "article_idx": "2",
        "eligibility": "KOSIS_CANDIDATE",
        "indicator_norm": "실질 지역내총생산 성장률",
        "measurement_type": "CHANGE_RATE",
        "value_text": "1.4%",
        "value_sentence_id": 0,
        "dimension_texts": ["울산"],
    }]
    articles = {
        "2": {
            "title": "지역 성장률",
            "article_text": "실질 지역내총생산 성장률은 울산(1.4%)이 높았다.",
        }
    }

    rows, report = build_semantic_labeling_scaffold(gold, articles)
    draft = rows[0]["semantic_role_labeling"]

    assert len(draft["dimension_candidate_ids_draft"]) == 1
    assert draft["field_status"]["dimension"] == "DRAFT_FROM_EXISTING_GOLD"
    assert report["dimension_gold_count"] == 1
    assert report["dimension_candidate_match_count"] == 1
