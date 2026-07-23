from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claim_extractor import extract_from_article, iter_sentence_spans
from retrieval_schema import Claim, validate_claim


def test_sentence_spans_preserve_cleaned_text_and_order() -> None:
    text = "첫 문장이다.  둘째 문장에는 12명이 있다. 셋째 문장이다."
    spans = list(iter_sentence_spans(text))

    assert [item[0] for item in spans] == [0, 1, 2]
    for _, start, end, sentence in spans:
        assert text[start:end] == sentence


def test_extracted_row_contains_sentence_location() -> None:
    text = "숫자 없는 문장이다. 둘째 문장에는 12명이 있다."
    rows = extract_from_article(7, "제목", "2026-07-22", "label", text)

    assert len(rows) == 1
    row = rows[0]
    assert row["sentence_index"] == 1
    assert text[row["sentence_char_start"]:row["sentence_char_end"]] == row["claim_text"]


def test_schema_accepts_sentence_location_metadata() -> None:
    claim = Claim(
        claim_id="article_1_row_000001",
        article_idx=1,
        claim_text="12명이 있다.",
        sentence_index=3,
        sentence_char_start=20,
        sentence_char_end=28,
    )

    assert validate_claim(claim) == []
