import json

import pytest

from src.develop.retrieval_gold_app import AdjudicationStore


def _row(**overrides):
    row = {
        "target_id": "1:s0:value_unit:0-3",
        "article_idx": "1",
        "value_span_id": "s0:value_unit:0-3",
        "value_text": "9.2%",
        "sentence_text": "노년 부양비는 9.2%다.",
        "indicator": "노년 부양비",
        "period": "지난해",
        "candidates": [
            {"rank": 1, "table_key": "101:A", "tbl_name": "노년부양비 및 노령화지수",
             "category_paths": ["인구"], "score": 9.1},
            {"rank": 2, "table_key": "101:B", "tbl_name": "어가 인구",
             "category_paths": ["수산"], "score": 3.2},
        ],
        "gold_match_status": "",
        "gold_table_key": "",
        "gold_tbl_name": "",
        "gold_from_candidate_rank": "",
        "adjudication_note": "",
        "review_status": "미검토",
    }
    row.update(overrides)
    return row


def _store(tmp_path, rows=None):
    path = tmp_path / "adjudication.jsonl"
    rows = rows or [_row()]
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return AdjudicationStore(path)


def test_choosing_a_candidate_records_its_rank(tmp_path):
    store = _store(tmp_path)

    row = store.save(store.rows[0]["target_id"], {
        "gold_match_status": "후보에서 찾음", "gold_table_key": "101:A",
    })

    assert row["gold_from_candidate_rank"] == 1
    assert row["gold_tbl_name"] == "노년부양비 및 노령화지수"


def test_rejecting_every_candidate_is_a_first_class_answer(tmp_path):
    """If 없음 were awkward the gold would drift to whatever was surfaced."""
    store = _store(tmp_path)

    row = store.save(store.rows[0]["target_id"], {
        "gold_match_status": "후보 밖에 있음", "adjudication_note": "경제활동인구조사",
    })

    assert row["review_status"] == "검토완료"
    assert row["gold_table_key"] == ""
    assert row["gold_from_candidate_rank"] == ""


def test_missing_from_candidates_and_absent_from_kosis_stay_apart(tmp_path):
    """Collapsing them would hide the number this exercise exists to produce."""
    store = _store(tmp_path, [_row(), _row(target_id="b")])

    store.save(store.rows[0]["target_id"], {"gold_match_status": "후보 밖에 있음"})
    store.save("b", {"gold_match_status": "표 없음"})

    statuses = store.progress()["statuses"]
    assert statuses["후보 밖에 있음"] == 1
    assert statuses["표 없음"] == 1


def test_a_found_verdict_requires_a_table(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="requires a table"):
        store.save(store.rows[0]["target_id"], {
            "gold_match_status": "후보에서 찾음",
        })


def test_a_table_outside_the_candidate_list_is_refused(tmp_path):
    """Its rank would be unknown, so it cannot be a 후보에서 찾음."""
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="후보 목록에 없습니다"):
        store.save(store.rows[0]["target_id"], {
            "gold_match_status": "후보에서 찾음", "gold_table_key": "999:Z",
        })


def test_an_unknown_status_is_refused(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="unknown status"):
        store.save(store.rows[0]["target_id"], {"gold_match_status": "몰라"})


def test_rank_distribution_is_reported(tmp_path):
    store = _store(tmp_path, [_row(), _row(target_id="b")])

    store.save(store.rows[0]["target_id"], {
        "gold_match_status": "후보에서 찾음", "gold_table_key": "101:A"})
    store.save("b", {
        "gold_match_status": "후보에서 찾음", "gold_table_key": "101:B"})

    assert store.progress()["found_at_rank"] == {1: 1, 2: 1}


def test_saves_survive_a_restart(tmp_path):
    store = _store(tmp_path)
    store.save(store.rows[0]["target_id"], {
        "gold_match_status": "표 없음", "adjudication_note": "해외 통계",
    })

    reopened = AdjudicationStore(store.working_path)

    assert reopened.rows[0]["gold_match_status"] == "표 없음"
    assert reopened.rows[0]["adjudication_note"] == "해외 통계"
