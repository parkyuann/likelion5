from __future__ import annotations

import pytest

from src.develop.apply_article_hcx_semantic_adjudications import apply_adjudications


def _review(
    fixture_id: str,
    *,
    sentence: str,
    population: str = "없음",
    item: str = "없음",
    dimension: str = "없음",
    status: str = "확정",
    qc: str = "완료",
) -> list[object]:
    return [
        fixture_id, "1", 0, sentence, "지표", "1.0%", "LEVEL",
        population, item, dimension, status, qc, "검토 메모",
        "지표", "", "", "", "", "r10",
    ]


def _candidate(
    fixture_id: str,
    *,
    kind: str,
    text: str,
    candidate_id: str,
) -> list[object]:
    return [
        fixture_id, "1", 0, "지표", "1.0%", kind, "", text,
        candidate_id, 0, "", "r10",
    ]


def test_apply_adjudications_resolves_exact_particle_composite_and_fallback() -> None:
    scaffold = [
        {
            "fixture_id": "f1",
            "article_idx": "1",
            "eligibility": "KOSIS_CANDIDATE",
            "indicator_norm": "물가 상승률",
            "value_text": "1.0%",
            "value_sentence_id": 0,
            "dimension_texts": [],
            "semantic_role_labeling": {
                "target_value_candidate_ids_draft": ["s0:value_unit:20-24"],
                "indicator_evidence_candidate_ids_draft": ["s0:semantic_evidence:0-2"],
                "dimension_candidate_ids_draft": [],
                "field_status": {"indicator": "DRAFT_CANDIDATE_COMPLETE"},
            },
        },
        {
            "fixture_id": "f2",
            "article_idx": "1",
            "eligibility": "KOSIS_CANDIDATE",
            "indicator_norm": "건설업 성장률",
            "value_text": "1.0%",
            "value_sentence_id": 0,
            "dimension_texts": ["전국", "건설업"],
            "semantic_role_labeling": {
                "target_value_candidate_ids_draft": ["s0:value_unit:20-24"],
                "indicator_evidence_candidate_ids_draft": [],
                "dimension_candidate_ids_draft": [
                    "s0:dimension:0-2",
                    "s0:dimension:3-6",
                ],
                "field_status": {},
            },
        },
    ]
    snapshot = {
        "workbook_path": "confirmed.xlsx",
        "review_rows": [
            _review(
                "f1",
                sentence="쌀은 식료품·에너지 제외 기준이다.",
                item="쌀",
                dimension="식료품·에너지 제외",
            ),
            _review(
                "f2",
                sentence="전국 건설업 성장률은 1.0%다.",
                dimension="전국, 건설업",
            ),
        ],
        "candidate_rows": [
            _candidate(
                "f1",
                kind="semantic_evidence",
                text="쌀은",
                candidate_id="s0:semantic_evidence:0-2",
            ),
            _candidate(
                "f1",
                kind="semantic_evidence",
                text="식료품·에너지",
                candidate_id="s0:semantic_evidence:3-10",
            ),
            _candidate(
                "f1",
                kind="semantic_evidence",
                text="제외",
                candidate_id="s0:semantic_evidence:11-13",
            ),
        ],
    }

    output, report = apply_adjudications(
        scaffold,
        snapshot,
        workbook_sha256="abc",
    )

    assert output[0]["item_texts"] == ["쌀"]
    assert output[0]["dimension_texts"] == ["식료품·에너지 제외"]
    assert output[0]["semantic_role_gold"]["item_evidence_candidate_ids"] == [
        "s0:semantic_evidence:0-2"
    ]
    assert output[0]["semantic_role_gold"]["dimension_candidate_ids"] == [
        "s0:semantic_evidence:3-10",
        "s0:semantic_evidence:11-13",
    ]
    assert output[1]["semantic_role_gold"]["dimension_candidate_ids"] == [
        "s0:dimension:0-2",
        "s0:dimension:3-6",
    ]
    assert report["candidate_resolution_mode_counts"] == {
        "PARTICLE_VARIANT": 1,
        "COMPOSITE": 2,
        "EXACT": 2,
    }
    assert report["scaffold_fallback_candidates"] == 2
    assert report["candidate_resolution_errors"] == 0


def test_apply_adjudications_rejects_unconfirmed_row() -> None:
    scaffold = [{
        "fixture_id": "f1",
        "article_idx": "1",
        "eligibility": "KOSIS_CANDIDATE",
        "value_sentence_id": 0,
        "semantic_role_labeling": {},
    }]
    snapshot = {
        "review_rows": [_review("f1", sentence="문장", status="보류")],
        "candidate_rows": [],
    }

    with pytest.raises(ValueError, match="review status"):
        apply_adjudications(scaffold, snapshot)
