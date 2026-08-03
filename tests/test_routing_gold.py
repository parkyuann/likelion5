import json

import pytest

from src.develop.build_routing_gold_scaffold import build_scaffold, scaffold_summary
from src.develop.routing_gold_app import RoutingStore

ARTICLE = {
    "article_idx": "1964",
    "title": "5월 수출 -1.3%",
    "date": "2025-06-01",
    "article_text": (
        "5월 수출이 감소했다. "
        "산업통상자원부에 따르면 지난달 수출액은 569억달러로 1.3% 줄었다. "
        "반도체 수출은 138억달러였다."
    ),
}


def test_scaffold_makes_one_row_per_value_candidate():
    rows = build_scaffold([ARTICLE])

    assert rows
    assert all(row["value_text"] for row in rows)
    assert all(row["judged_class"] == "" for row in rows)


def test_scaffold_carries_neighbouring_sentences():
    rows = build_scaffold([ARTICLE])
    later = [r for r in rows if r["sentence_id"] == 2][0]

    positions = {c["position"] for c in later["context"]}
    assert "before" in positions
    assert any(c["sentence_id"] == 1 for c in later["context"])


def test_scaffold_flags_the_sentence_that_states_the_source():
    rows = build_scaffold([ARTICLE])
    attributed = [r for r in rows if r["sentence_id"] == 1]

    assert attributed
    assert attributed[0]["sentence_has_attribution"] is True


def test_scaffold_marks_attribution_in_context_so_it_is_findable():
    rows = build_scaffold([ARTICLE])
    later = [r for r in rows if r["sentence_id"] == 2][0]

    flagged = [c for c in later["context"] if c["has_attribution"]]
    assert flagged and flagged[0]["sentence_id"] == 1


def test_scaffold_contains_no_model_output():
    rows = build_scaffold([ARTICLE])
    blob = json.dumps(rows, ensure_ascii=False)

    for leaked in ("prediction", "predicted", "block_reason", "PASS", "score"):
        assert leaked not in blob
    assert scaffold_summary(rows)["contains_model_output"] is False


def test_value_offsets_locate_the_value_inside_its_sentence():
    for row in build_scaffold([ARTICLE]):
        start, end = row["value_char_start"], row["value_char_end"]
        assert row["sentence_text"][start:end] == row["value_text"]


def _store(tmp_path):
    path = tmp_path / "working.jsonl"
    rows = build_scaffold([ARTICLE])
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return RoutingStore(path)


def test_store_saves_and_counts_progress(tmp_path):
    store = _store(tmp_path)
    first = store.rows[0]["judgement_id"]

    store.save(first, "KOSIS_CANDIDATE", "")
    progress = store.progress()

    assert progress["done"] == 1
    assert progress["classes"]["KOSIS_CANDIDATE"] == 1


def test_sentence_scope_judges_every_value_in_the_sentence(tmp_path):
    """`26.2%에서 18.6%로` is one judgement written twice."""
    store = _store(tmp_path)
    target = next(
        row for row in store.rows
        if sum(
            1 for other in store.rows
            if (other["article_idx"], other["sentence_id"])
            == (row["article_idx"], row["sentence_id"])
        ) > 1
    )

    rows = store.save_sentence(target["judgement_id"], "KOSIS_CANDIDATE", "")

    assert len(rows) > 1
    assert {row["judged_class"] for row in rows} == {"KOSIS_CANDIDATE"}
    assert all(
        row["sentence_id"] == target["sentence_id"] for row in rows
    )


def test_sentence_scope_does_not_reach_a_neighbouring_sentence(tmp_path):
    store = _store(tmp_path)
    target = store.rows[0]

    store.save_sentence(target["judgement_id"], "NOT_CLAIM", "")

    others = [
        row for row in store.rows
        if row["sentence_id"] != target["sentence_id"]
    ]
    assert others
    assert not any(row.get("judged_class") for row in others)


def test_a_single_value_can_still_be_corrected_afterwards(tmp_path):
    store = _store(tmp_path)
    target = next(
        row for row in store.rows
        if sum(
            1 for other in store.rows
            if (other["article_idx"], other["sentence_id"])
            == (row["article_idx"], row["sentence_id"])
        ) > 1
    )
    group = store.sentence_ids(target["judgement_id"])
    store.save_sentence(target["judgement_id"], "KOSIS_CANDIDATE", "")

    store.save(group[1], "NOT_CLAIM", "분모")

    by_id = {row["judgement_id"]: row for row in store.rows}
    assert by_id[group[0]]["judged_class"] == "KOSIS_CANDIDATE"
    assert by_id[group[1]]["judged_class"] == "NOT_CLAIM"


def test_store_rejects_an_unknown_class(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="unknown class"):
        store.save(store.rows[0]["judgement_id"], "MAYBE", "")


def test_clearing_a_class_returns_the_row_to_unreviewed(tmp_path):
    store = _store(tmp_path)
    first = store.rows[0]["judgement_id"]
    store.save(first, "NOT_CLAIM", "")

    store.save(first, "", "")

    assert store.progress()["done"] == 0
    assert store.rows[0]["review_status"] == "미검토"


def test_saves_survive_a_restart(tmp_path):
    store = _store(tmp_path)
    first = store.rows[0]["judgement_id"]
    store.save(first, "OUT_OF_SCOPE", "정책 목표치")

    reopened = RoutingStore(store.working_path)

    assert reopened.rows[0]["judged_class"] == "OUT_OF_SCOPE"
    assert reopened.rows[0]["judge_note"] == "정책 목표치"
