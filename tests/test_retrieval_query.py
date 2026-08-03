from src.develop.retrieval_query import (
    ITEM,
    PRIMARY,
    SENTENCE,
    attach_query_variants,
    build_query_variants,
)


def _fields(**overrides):
    fields = {
        "indicator": "휘발유 평균 판매가",
        "item": ["휘발유"],
        "population": ["가구"],
        "period": "이달 첫 주",
    }
    fields.update(overrides)
    return fields


def test_the_indicator_alone_is_the_primary_query():
    """Appending item and population lowered recall 0.333 → 0.303."""
    variants = build_query_variants(_fields(), "휘발유값이 1639.8원이다.")

    assert variants[0] == {"role": PRIMARY, "query": "휘발유 평균 판매가"}


def test_population_never_enters_a_query():
    variants = build_query_variants(_fields(), "")

    assert all("가구" not in variant["query"] for variant in variants)


def test_an_item_already_inside_the_indicator_is_not_repeated():
    variants = build_query_variants(_fields(), "")

    assert [variant["role"] for variant in variants] == [PRIMARY]


def test_an_item_the_indicator_dropped_becomes_its_own_query():
    variants = build_query_variants(
        _fields(indicator="평균 판매가", item=["경유"]), "",
    )

    assert {"role": ITEM, "query": "경유"} in variants


def test_the_sentence_is_a_second_query():
    """It recovered three targets the indicator alone missed."""
    variants = build_query_variants(_fields(), "국제 유가가 내렸다.")

    assert variants[-1] == {"role": SENTENCE, "query": "국제 유가가 내렸다."}


def test_a_sentence_identical_to_the_indicator_is_not_duplicated():
    variants = build_query_variants(
        _fields(item=[]), "휘발유 평균 판매가",
    )

    assert len(variants) == 1


def test_whitespace_is_normalised_so_variants_dedupe():
    variants = build_query_variants(
        _fields(item=[]), "휘발유  평균   판매가",
    )

    assert len(variants) == 1


def test_an_empty_indicator_does_not_produce_an_empty_query():
    variants = build_query_variants(_fields(indicator="", item=[]), "문장")

    assert all(variant["query"] for variant in variants)
    assert [variant["role"] for variant in variants] == [SENTENCE]


def test_attach_leaves_the_fields_alone():
    rows = attach_query_variants([
        {"retrieval_fields": _fields(), "sentence_text": "문장이다."},
    ])

    assert rows[0]["retrieval_fields"] == _fields()
    assert rows[0]["retrieval_queries"][0]["role"] == PRIMARY
