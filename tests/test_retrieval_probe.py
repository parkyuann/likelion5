import json

import pytest

from src.develop.retrieval_probe_app import ProbeStore, search_terms


def _row(**overrides):
    row = {
        "probe_id": "353:s2:value_unit:4-9",
        "bucket": "확신",
        "confidence": 1.0,
        "value": "9.2%",
        "sentence": "달걀 가격은 전년 동월 대비 9.2% 올랐다.",
        "indicator": "달걀 가격 상승률",
        "measurement": "CHANGE_RATE",
        "period": "전년 동월",
        "population": [],
        "item": ["달걀 가격"],
        "dimension": [],
        "source_subtype": "공식집계",
        "kosis_table_found": "",
        "kosis_table_name": "",
        "blocking_field": "",
        "probe_note": "",
    }
    row.update(overrides)
    return row


def test_search_terms_go_from_specific_to_broad():
    """A miss on the full indicator that a bare item finds localises the fault."""
    terms = search_terms(_row())

    assert terms[0] == "달걀 가격 상승률"
    assert "달걀 가격" in terms
    assert terms.index("달걀 가격 상승률") < terms.index("달걀 가격")


def test_an_empty_quantifier_is_not_offered_as_the_broad_query():
    """`전체` retrieves nothing about taxes; `국세 수입` retrieves the family."""
    terms = search_terms(_row(
        indicator="전체 국세 수입 중 근로소득세 비율", item=["근로소득세"]))

    assert "전체" not in terms
    assert "국세 수입" in terms


def test_the_broad_query_keeps_two_tokens_before_one():
    from src.develop.retrieval_probe_app import broad_terms

    assert broad_terms("국세 수입 중 근로소득세 비율") == ["국세 수입", "국세"]


def test_an_all_quantifier_indicator_yields_no_broad_query():
    from src.develop.retrieval_probe_app import broad_terms

    assert broad_terms("전체") == []


def test_search_terms_include_population_when_present():
    terms = search_terms(_row(population=["근로자"], item=["단기 근로자"]))

    assert "근로자" in terms


def test_search_terms_do_not_repeat():
    terms = search_terms(_row(indicator="달걀", item=["달걀"]))

    assert len(terms) == len(set(terms))


def _store(tmp_path, rows=None):
    path = tmp_path / "probe.jsonl"
    rows = rows or [_row()]
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return ProbeStore(path)


def test_store_precomputes_search_terms(tmp_path):
    store = _store(tmp_path)

    assert store.rows[0]["search_terms"][0] == "달걀 가격 상승률"


def test_store_records_a_verdict(tmp_path):
    store = _store(tmp_path)

    store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "찾음",
        "kosis_table_name": "소비자물가조사",
    })

    assert store.progress()["done"] == 1
    assert store.progress()["verdicts"]["찾음"] == 1


def test_too_many_candidates_is_its_own_verdict(tmp_path):
    """A query returning hundreds of undifferentiated tables is a finding."""
    store = _store(tmp_path)

    row = store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "후보 과다",
        "blocking_fields": ["indicator 표현"],
    })

    assert row["kosis_table_found"] == "후보 과다"
    assert store.progress()["verdicts"]["후보 과다"] == 1


def test_period_granularity_is_a_distinct_blocking_reason(tmp_path):
    """A weekly claim against a monthly table is not a dimension problem."""
    store = _store(tmp_path)

    row = store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "못찾음",
        "blocking_fields": ["period 단위 불일치"],
    })

    assert row["blocking_fields"] == ["period 단위 불일치"]


def test_period_granularity_can_accompany_a_find(tmp_path):
    """A monthly table for a weekly claim is still a retrieval success.

    The probe measures whether the fields work as a query; whether KOSIS holds
    the data at that granularity is Task 3-2's problem, so the two are counted
    separately rather than collapsed into 못찾음.
    """
    store = _store(tmp_path)

    row = store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "찾음",
        "kosis_table_name": "주유소 제품별 평균 판매가격",
        "blocking_fields": ["period 단위 불일치"],
    })

    assert row["kosis_table_found"] == "찾음"
    assert store.progress()["blocking_fields"]["period 단위 불일치"] == 1


def test_the_query_that_worked_is_recorded(tmp_path):
    store = _store(tmp_path)

    row = store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "찾음",
        "tried_terms": ["달걀 가격 상승률", "달걀 가격"],
        "found_via": "달걀 가격",
    })

    assert row["found_via"] == "달걀 가격"
    assert row["tried_terms"] == ["달걀 가격 상승률", "달걀 가격"]


def test_found_via_is_tallied_by_rank_not_text(tmp_path):
    """Rank 2+ counts the rows whose full indicator could not retrieve.

    Term 1 is always the full indicator, so the rank is comparable across
    rows in a way the term text never is.
    """
    store = _store(tmp_path, [_row(probe_id="a"), _row(probe_id="b")])

    store.save("a", {"kosis_table_found": "찾음", "found_via": "달걀 가격 상승률"})
    store.save("b", {"kosis_table_found": "찾음", "found_via": "달걀 가격"})

    assert store.progress()["found_via_rank"] == {1: 1, 2: 1}


def test_a_hand_typed_query_is_kept_and_counted_apart(tmp_path):
    """Only a hand-typed query working is the sharpest field defect signal."""
    store = _store(tmp_path)

    row = store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "찾음",
        "custom_terms": ["국세 수입"],
        "tried_terms": ["달걀 가격 상승률", "국세 수입"],
        "found_via": "국세 수입",
    })

    assert row["found_via"] == "국세 수입"
    progress = store.progress()
    assert progress["found_via_rank"] == {}
    assert progress["found_via_custom"][0]["found_via"] == "국세 수입"


def test_an_unannounced_query_is_still_dropped(tmp_path):
    """found_via must come from a term the row actually offered or recorded."""
    store = _store(tmp_path)

    row = store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "찾음", "found_via": "물가", "tried_terms": ["물가"],
    })

    assert row["found_via"] == ""
    assert row["tried_terms"] == []


def test_store_rejects_an_unknown_verdict(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="unknown verdict"):
        store.save(store.rows[0]["probe_id"], {"kosis_table_found": "몰라"})


def test_blocking_fields_are_restricted_to_the_known_set(tmp_path):
    store = _store(tmp_path)

    row = store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "못찾음",
        "blocking_fields": ["indicator 표현", "존재하지않는필드"],
    })

    assert row["blocking_fields"] == ["indicator 표현"]


def test_progress_splits_by_bucket(tmp_path):
    """The probe exists to compare confident routing against low-confidence."""
    store = _store(tmp_path, [
        _row(probe_id="a", bucket="확신"),
        _row(probe_id="b", bucket="저확신"),
    ])

    store.save("a", {"kosis_table_found": "찾음"})
    store.save("b", {"kosis_table_found": "못찾음"})

    by_bucket = store.progress()["by_bucket"]
    assert by_bucket["확신"] == {"찾음": 1}
    assert by_bucket["저확신"] == {"못찾음": 1}


def test_blocking_field_tally_supports_the_threshold_decision(tmp_path):
    store = _store(tmp_path, [_row(probe_id="a"), _row(probe_id="b")])

    store.save("a", {"kosis_table_found": "못찾음",
                     "blocking_fields": ["indicator 표현", "period 값 없음"]})
    store.save("b", {"kosis_table_found": "못찾음",
                     "blocking_fields": ["indicator 표현"]})

    assert store.progress()["blocking_fields"]["indicator 표현"] == 2


def test_saves_survive_a_restart(tmp_path):
    store = _store(tmp_path)
    store.save(store.rows[0]["probe_id"], {
        "kosis_table_found": "애매", "probe_note": "비슷한 표가 둘",
    })

    reopened = ProbeStore(store.working_path)

    assert reopened.rows[0]["kosis_table_found"] == "애매"
    assert reopened.rows[0]["probe_note"] == "비슷한 표가 둘"
