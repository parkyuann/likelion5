import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from src.evaluate_claim_filter import evaluate


def test_filter_evaluation_reports_claims_and_abstention(tmp_path):
    path = tmp_path / "claims.csv"
    pd.DataFrame([
        {"article_idx": 1, "gold_is_claim": "True", "gold_claim_class": "집계통계", "gold_source_scope": "KOSIS등재", "gold_verifiability_prefilter": "검증시도"},
        {"article_idx": 2, "gold_is_claim": "True", "gold_claim_class": "전망예측", "gold_source_scope": "KOSIS등재", "gold_verifiability_prefilter": "제외"},
        {"article_idx": 3, "gold_is_claim": "False", "gold_claim_class": "", "gold_source_scope": "불명", "gold_verifiability_prefilter": "제외"},
    ]).to_csv(path, index=False)
    result = evaluate(path)
    assert result["claim_rows"] == 2
    assert result["kosis_attempt_rows"] == 1
    assert result["abstained_claim_rows"] == 1
    assert result["prefilter_consistency"] == 1.0
