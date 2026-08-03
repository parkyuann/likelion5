import csv
import json

from src.develop.build_article_hcx_holdout_fixture import (
    build_holdout_fixture,
    select_holdout_seeds,
)


def _write_csv(path, headers, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_holdout_fixture_selects_kosis_articles_and_freezes_all_sentences(
    tmp_path,
):
    evaluation = tmp_path / "evaluation.csv"
    _write_csv(
        evaluation,
        [
            "article_idx",
            "기사제목",
            "claim_text",
            "gold_source_scope",
            "evaluation_set",
        ],
        [
            {
                "article_idx": "0",
                "기사제목": "기사 A",
                "claim_text": "인구는 10명이다.",
                "gold_source_scope": "KOSIS등재",
                "evaluation_set": "holdout",
            },
            {
                "article_idx": "1",
                "기사제목": "기사 B",
                "claim_text": "기업 사례",
                "gold_source_scope": "민간기관",
                "evaluation_set": "holdout",
            },
        ],
    )
    news = tmp_path / "news.csv"
    _write_csv(
        news,
        [
            "기사제목",
            "작성일",
            "URL",
            "본문_정제",
            "섹션",
        ],
        [
            {
                "기사제목": "기사 A",
                "작성일": "2026-01-01",
                "URL": "https://example.test/a",
                "본문_정제": "인구는 10명이다. 전년보다 2명 늘었다.",
                "섹션": "사회",
            },
            {
                "기사제목": "기사 B",
                "작성일": "2026-01-02",
                "URL": "https://example.test/b",
                "본문_정제": "개별 기업 사례다.",
                "섹션": "경제",
            },
        ],
    )
    pipeline = tmp_path / "pipeline.py"
    pipeline.write_text("# frozen\n", encoding="utf-8")
    output = tmp_path / "fixture"

    seeds = select_holdout_seeds([evaluation])
    manifest = build_holdout_fixture(
        news_csv=news,
        evaluation_paths=[evaluation],
        pipeline_path=pipeline,
        output_root=output,
    )

    assert [row["article_idx"] for row in seeds] == ["0"]
    assert manifest["selected_article_ids"] == ["0"]
    assert manifest["article_count"] == 1
    assert manifest["sentence_count"] == 2
    assert manifest["value_candidate_count"] == 2
    saved = [
        json.loads(line)
        for line in (output / "input.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert saved[0]["article_text"].startswith("인구는 10명")
