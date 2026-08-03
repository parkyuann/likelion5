import json

import pandas as pd

from src.develop.build_stratum_candidates import (
    FORBIDDEN_JUDGMENT_COLUMNS,
    JUDGMENT_COLUMNS,
    build_stratum_candidates,
    density_bin,
)


def test_density_boundaries():
    assert density_bin(9) == "LOW"
    assert density_bin(10) == "MID"
    assert density_bin(25) == "MID"
    assert density_bin(26) == "HIGH"


def test_build_stratum_candidates_keeps_judgment_sheet_uncontaminated(
    tmp_path,
    monkeypatch,
):
    evaluation_path = tmp_path / "eval.csv"
    pd.DataFrame([
        {"article_idx": str(index), "gold_source_scope": "불명"}
        for index in range(6)
    ]).to_csv(evaluation_path, index=False, encoding="utf-8-sig")
    news_path = tmp_path / "news.csv"
    news = pd.DataFrame([
        {
            "기사제목": f"제목 {index}",
            "작성일": "2025-01-01",
            "본문_정제": (
                f"한국은행에 따르면 값 {index}이다."
                if index < 5 else "기관 언급이 없다."
            ),
            "기사 본문 전체": "",
        }
        for index in range(2704)
    ])
    news.to_csv(news_path, index=False, encoding="utf-8-sig")
    catalog_path = tmp_path / "org.json"
    catalog_path.write_text(
        json.dumps({"1": "한국은행"}, ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_candidates(text):
        count = 30 if "값 0" in text or "값 1" in text else 5
        return [{"kind": "value_unit"}] * count

    monkeypatch.setattr(
        "src.develop.build_stratum_candidates.build_span_candidates",
        fake_candidates,
    )
    result = build_stratum_candidates(
        [evaluation_path],
        news_path,
        catalog_path,
        sample_size=3,
        min_high=1,
    )

    assert len(result["selected"]) == 3
    assert len(result["excluded_no_hit"]) == 1
    assert set(result["selected"][0]) == set(JUDGMENT_COLUMNS)
    assert not (set(result["selected"][0]) & FORBIDDEN_JUDGMENT_COLUMNS)
    assert all(not row["judged_source_scope"] for row in result["selected"])
    assert result["manifest"]["sample_density_counts"]["HIGH"] >= 1
