import json
from pathlib import Path

from src.develop.evaluate_article_hcx_holdout import (
    evaluate_records,
    parse_adjudication_snapshot,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _snapshot() -> dict:
    return {
        "artifact_status": "HUMAN_ADJUDICATED",
        "workbook_path": "unused.xlsx",
        "workbook_sha256": "frozen",
        "matrices": {
            "sentence_review": [[
                "문장검토ID", "article_idx", "sentence_id", "기사제목",
                "원문 문장", "자동 value candidates", "검증가능 claim 수",
                "비대상 수치 claim 수", "문장검토상태", "누락claim메모",
            ], [
                "1-S000", "1", 0, "제목", "취업자는 10명이다.", "10명",
                1, 0, "완료", "",
            ]],
            "claim_gold": [[
                "gold_id", "article_idx", "sentence_id", "원문 문장",
                "target value", "candidate span ID", "행 출처", "claim 여부",
                "검증대상 gold", "indicator gold", "measurement gold",
                "period gold", "population gold", "item gold",
                "dimension gold", "값-pairing", "최종상태", "검토메모",
            ], [
                "1-V0001", "1", 0, "취업자는 10명이다.", "10명",
                "s0:value_unit:5-8", "AUTO_VALUE", "YES",
                "KOSIS_CANDIDATE", "취업자 수", "LEVEL", "현재",
                "취업자", "없음", "전국", "YES", "확정", "",
            ], [
                "1-M01", "1", "", "", "", "", "ADDED_MISSED", "NO",
                "NOT_CLAIM", "없음", "없음", "없음", "없음", "없음",
                "없음", "NO", "제외", "미사용 추가행",
            ]],
        },
    }


def test_parse_adjudication_snapshot_reconciles_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_jsonl(fixture / "sentences.jsonl", [{
        "article_idx": "1",
        "sentence_id": 0,
        "text": "취업자는 10명이다.",
    }])
    _write_jsonl(fixture / "value_candidates.jsonl", [{
        "article_idx": "1",
        "span_id": "s0:value_unit:5-8",
        "text": "10명",
    }])

    sentences, claims, integrity = parse_adjudication_snapshot(
        _snapshot(),
        fixture_root=fixture,
        verify_workbook=False,
    )

    assert len(sentences) == 1
    assert len(claims) == 1
    assert integrity["placeholder_rows_excluded"] == 1
    assert integrity["eligibility_counts"] == {"KOSIS_CANDIDATE": 1}


def test_evaluate_records_reports_detection_routing_and_exactness() -> None:
    records = [{
        "review_id": "1-V0001",
        "article_idx": "1",
        "row_source": "AUTO_VALUE",
        "gold": {
            "claim": True,
            "eligibility": "KOSIS_CANDIDATE",
            "indicator": "취업자 수",
            "measurement_type": "LEVEL",
            "period": "현재",
            "population": ["취업자"],
            "item": [],
            "dimension": ["전국"],
        },
        "automatic": {
            "indicator": "취업자 수",
            "measurement_type": "LEVEL",
            "period": "현재",
            "population": ["취업자"],
            "item": [],
            "dimension": ["전국"],
            "action": "PASS",
        },
        "prediction": {"detected": True},
    }, {
        "review_id": "1-V0002",
        "article_idx": "1",
        "row_source": "AUTO_VALUE",
        "gold": {
            "claim": False,
            "eligibility": "NOT_CLAIM",
            "indicator": "",
            "measurement_type": "",
            "period": "",
            "population": [],
            "item": [],
            "dimension": [],
        },
        "automatic": {
            "indicator": "",
            "measurement_type": "",
            "period": "",
            "population": [],
            "item": [],
            "dimension": [],
            "action": "MISSED",
        },
        "prediction": {"detected": False},
    }]

    report = evaluate_records(records)

    assert report["claim_detection"]["true_positive"] == 1
    assert report["claim_detection"]["false_positive"] == 1
    assert report["semantic_selection"]["f1"] == 1.0
    assert report["routing"]["f1"] == 1.0
    assert report["complete_record"]["six_field_exact_end_to_end_rows"] == 1
