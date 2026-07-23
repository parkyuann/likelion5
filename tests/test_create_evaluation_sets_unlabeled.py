import pandas as pd

from src.create_evaluation_sets import prepare


def test_prepare_clears_upstream_silver_values():
    frame = pd.DataFrame([{
        "article_idx": 1,
        "gold_claim_class": "집계통계",
        "gold_source_scope": "KOSIS등재",
        "gold_verifiability_prefilter": "검증시도",
    }])
    result = prepare(frame, "validation300")
    assert result.loc[0, "gold_claim_class"] == ""
    assert result.loc[0, "gold_source_scope"] == ""
    assert result.loc[0, "gold_verifiability_prefilter"] == ""
    assert result.loc[0, "human_review_status"] == "pending"
    assert result.loc[0, "human_reviewer"] == ""
