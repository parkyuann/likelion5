import pandas as pd

from src.baseline.report_hcx_comparison import write_report


def test_report_includes_precision_recall_and_f1(tmp_path):
    summary = tmp_path / "summary.csv"
    pd.DataFrame([{
        "model": "HCX-007", "use_response_format": True,
        "claim_detection_precision": 0.8, "claim_detection_recall": 0.7,
        "claim_detection_f1": 0.75, "rlt_score": 0.4,
    }]).to_csv(summary, index=False)
    output = tmp_path / "comparison.md"
    write_report(summary, output)
    text = output.read_text(encoding="utf-8")
    assert "claim detection precision" in text
    assert "claim detection recall" in text
    assert "claim detection f1" in text
