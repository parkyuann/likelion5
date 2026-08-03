import json

import pandas as pd
import pytest

from src.develop.freeze_stratum_splits import freeze_stratum_splits


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _article(article_idx, density):
    return {
        "article_idx": str(article_idx),
        "기사제목": f"제목 {article_idx}",
        "작성일": "2025-01-01",
        "density_bin": density,
        "article_sha256": f"sha-{article_idx}",
        "article_text": f"본문 {article_idx}",
    }


def test_freeze_stratum_splits_seals_holdout2_and_enforces_high_quota(
    tmp_path,
):
    rows = []
    internal = []
    for index in range(45):
        density = "HIGH" if index < 12 else ("MID" if index < 30 else "LOW")
        rows.append({
            "article_idx": str(index),
            "density_bin": density,
            "judged_source_scope": (
                "KOSIS등재" if index < 34 else "민간기관"
            ),
            "judge_note": "",
        })
        internal.append(_article(index, density))
    judgment = tmp_path / "judgment.csv"
    pd.DataFrame(rows).to_csv(
        judgment, index=False, encoding="utf-8-sig"
    )
    internal_path = tmp_path / "internal.jsonl"
    _write_jsonl(internal_path, internal)
    clean_path = tmp_path / "clean.jsonl"
    _write_jsonl(clean_path, [
        _article(1736, "MID"),
        _article(439, "LOW"),
    ])

    report = freeze_stratum_splits(
        judgment,
        internal_path,
        clean_path,
        tmp_path / "frozen",
    )

    assert report["splits"]["dev"]["article_count"] == 12
    assert report["splits"]["holdout_1"]["article_count"] == 12
    assert report["splits"]["holdout_2"]["article_count"] == 12
    assert all(
        split["density_distribution"]["HIGH"] >= 3
        for split in report["splits"].values()
    )
    assert (tmp_path / "frozen" / "sealed_holdout_2" / "input.jsonl").exists()
    assert not (tmp_path / "frozen" / "holdout_2").exists()


def test_freeze_stratum_splits_stops_when_high_quota_is_impossible(tmp_path):
    rows = []
    internal = []
    for index in range(45):
        density = "HIGH" if index < 4 else "LOW"
        rows.append({
            "article_idx": str(index),
            "density_bin": density,
            "judged_source_scope": (
                "KOSIS등재" if index < 34 else "민간기관"
            ),
            "judge_note": "",
        })
        internal.append(_article(index, density))
    judgment = tmp_path / "judgment.csv"
    pd.DataFrame(rows).to_csv(
        judgment, index=False, encoding="utf-8-sig"
    )
    internal_path = tmp_path / "internal.jsonl"
    _write_jsonl(internal_path, internal)
    clean_path = tmp_path / "clean.jsonl"
    _write_jsonl(clean_path, [
        _article(1736, "LOW"),
        _article(439, "LOW"),
    ])

    with pytest.raises(ValueError, match="insufficient HIGH"):
        freeze_stratum_splits(
            judgment,
            internal_path,
            clean_path,
            tmp_path / "frozen",
        )
