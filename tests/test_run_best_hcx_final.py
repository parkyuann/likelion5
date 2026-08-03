import pandas as pd

from src.baseline.select_hcx_model import select


def test_selection_prefers_highest_rlt_score(tmp_path):
    summary = tmp_path / "summary.csv"
    pd.DataFrame([
        {"model": "HCX-003", "rlt_score": 0.4, "claim_class_macro_f1": 0.3,
         "source_scope_macro_f1": 0.2, "claim_class_macro_recall": 0.3,
         "source_scope_macro_recall": 0.2},
        {"model": "HCX-007", "rlt_score": 0.5, "claim_class_macro_f1": 0.2,
         "source_scope_macro_f1": 0.3, "claim_class_macro_recall": 0.2,
         "source_scope_macro_recall": 0.3},
    ]).to_csv(summary, index=False)
    assert select(summary)["model"] == "HCX-007"
