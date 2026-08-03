import json
from pathlib import Path

from src.develop.complete_article_hcx_bindings import complete_missing_bindings


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_complete_missing_bindings_reuses_existing_binding(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    output = tmp_path / "completed"
    run.mkdir()
    article = "취업자는 10명이다."
    target = "s0:value_unit:5-8"
    _write_jsonl(run / "input.jsonl", [{
        "article_idx": "1",
        "title": "제목",
        "article_text": article,
    }])
    _write_jsonl(run / "raw.jsonl", [{
        "article_idx": "1",
        "article_sha256": "hash",
        "semantic_prediction": {"claims": [{
            "is_kosis_candidate": True,
            "claim_type": "취업자",
            "indicator_norm": "취업자 수",
            "context_sentence_ids": [0],
            "observation_sentence_ids": [0],
            "target_value_span_ids": [target],
        }]},
    }])
    existing = {
        "article_idx": "1",
        "article_sha256": "hash",
        "claim_index": 0,
        "binding": {"observations": []},
    }
    _write_jsonl(run / "bindings.jsonl", [existing])

    manifest = complete_missing_bindings(
        run,
        output,
        api_key="unused",
    )

    assert manifest["bindings_reused"] == 1
    assert manifest["bindings_created"] == 0
    assert len((output / "span_candidates.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()) == 1
