import pandas as pd

from src.build_labeling_sheet import all_extracted_sentences


def test_all_extracted_sentences_deduplicates_listform_rows():
    pool = pd.DataFrame([
        {"article_idx": 1, "claim_text": "same", "value_list": "1"},
        {"article_idx": 1, "claim_text": "same", "value_list": "2"},
        {"article_idx": 1, "claim_text": "other", "value_list": "3"},
        {"article_idx": 2, "claim_text": "outside", "value_list": "4"},
    ])
    result = all_extracted_sentences(pool, {1})
    assert result["claim_text"].tolist() == ["other", "same"]
