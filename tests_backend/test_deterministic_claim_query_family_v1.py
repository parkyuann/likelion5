from __future__ import annotations

import json
from pathlib import Path

from backend.develop_verify_service import _deterministic_claim_query_from_routed


def _write_routed(root: Path, rows: list[dict]) -> None:
    (root / "03_routed.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_multi_family_routed_article_returns_none_to_preserve_all_targets(tmp_path: Path):
    _write_routed(
        tmp_path,
        [
            {
                "retrieval_fields": {
                    "indicator": "고용자 수 증가율",
                    "measurement_type": "CHANGE_RATE",
                }
            },
            {
                "retrieval_fields": {
                    "indicator": "실업률",
                    "measurement_type": "LEVEL",
                    "period_absolute": "2025",
                }
            },
        ],
    )

    assert _deterministic_claim_query_from_routed(tmp_path) is None


def test_single_family_routed_article_keeps_existing_level_selector(tmp_path: Path):
    _write_routed(
        tmp_path,
        [
            {
                "retrieval_fields": {
                    "indicator": "고용자 수 증가율",
                    "measurement_type": "CHANGE_RATE",
                }
            },
            {
                "retrieval_fields": {
                    "indicator": "고용자 수",
                    "measurement_type": "LEVEL",
                    "period_absolute": "2025",
                }
            },
        ],
    )

    assert _deterministic_claim_query_from_routed(tmp_path) == "고용자 수 2025"
