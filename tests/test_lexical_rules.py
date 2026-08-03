from src.develop import article_claim_pipeline
from src.develop.lexical_rules import (
    DISABLE_DEV6_ENV,
    UNKNOWN_PROVENANCE,
    executable_korean_literal_inventory,
    merged_executable_korean_literal_inventory,
)


def test_dev6_flag_disables_only_provenance_tagged_category_rule(monkeypatch):
    sentence = {
        "text": "주 36시간 미만으로 일한 근로자는 100명이었다.",
        "char_start": 0,
    }
    value_span = {
        "unit": "시간",
        "char_start": 2,
        "char_end": 6,
    }

    enabled = article_claim_pipeline._local_value_role(
        sentence,
        value_span,
        set(),
    )
    monkeypatch.setenv(DISABLE_DEV6_ENV, "1")
    disabled = article_claim_pipeline._local_value_role(
        sentence,
        value_span,
        set(),
    )

    assert enabled == (
        "CATEGORY_DEFINITION",
        "VALUE_CATEGORY_DEFINITION",
    )
    assert disabled == ("TARGET_CANDIDATE", None)


def test_literal_inventory_excludes_docstrings_and_marks_unknown(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text(
        '"""문서 전용 표면형"""\n'
        'PROMPT = "실행 프롬프트"\n'
        'if "분류 기준" in PROMPT:\n'
        '    pass\n',
        encoding="utf-8",
    )

    rows = executable_korean_literal_inventory(source)
    by_surface = {row["surface"]: row for row in rows}

    assert "문서 전용 표면형" not in by_surface
    assert "실행 프롬프트" in by_surface
    assert by_surface["분류 기준"]["provenance_article_idx"] == (
        UNKNOWN_PROVENANCE
    )


def test_merged_literal_inventory_deduplicates_across_sources(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text('RULE = "공통 표면형"\n', encoding="utf-8")
    second.write_text('RULE = "공통 표면형"\n', encoding="utf-8")

    rows = merged_executable_korean_literal_inventory([first, second])

    assert len(rows) == 1
    assert rows[0]["source_files"] == sorted([str(first), str(second)])
