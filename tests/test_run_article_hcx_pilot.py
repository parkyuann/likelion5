import json

from src.develop.run_article_hcx_pilot import select_saved_articles


def test_select_saved_articles_reuses_exact_audited_article_input(tmp_path):
    first = tmp_path / "기사_20"
    second = tmp_path / "기사_10"
    first.mkdir()
    second.mkdir()
    (first / "input.jsonl").write_text(json.dumps({"article_idx": "20", "title": "둘", "article_text": "두 번째 원문"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (second / "input.jsonl").write_text(json.dumps({"article_idx": "10", "title": "첫", "article_text": "첫 번째 원문"}, ensure_ascii=False) + "\n", encoding="utf-8")

    result = select_saved_articles(tmp_path, limit=2)

    assert result == [
        {"article_idx": "10", "title": "첫", "article_text": "첫 번째 원문"},
        {"article_idx": "20", "title": "둘", "article_text": "두 번째 원문"},
    ]


def test_select_saved_articles_accepts_flat_multi_article_run_input(tmp_path):
    rows = [
        {"article_idx": "10", "title": "첫", "article_text": "첫 번째 원문"},
        {"article_idx": "20", "title": "둘", "article_text": "두 번째 원문"},
        {"article_idx": "30", "title": "셋", "article_text": "세 번째 원문"},
    ]
    (tmp_path / "input.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = select_saved_articles(tmp_path, limit=2)

    assert result == rows[:2]


def test_select_saved_articles_preserves_publication_date(tmp_path):
    row = {
        "article_idx": "10",
        "title": "첫",
        "article_text": "첫 번째 원문",
        "published_at": "2025-04-02",
    }
    (tmp_path / "input.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    assert select_saved_articles(tmp_path, limit=1) == [row]
