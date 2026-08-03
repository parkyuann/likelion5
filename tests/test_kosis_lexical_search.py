import json

from src.develop.kosis_lexical_search import LexicalIndex, bigrams, build_index

TABLES = [
    {"table_key": "101:A", "tbl_name": "장래인구추계 노년부양비",
     "category_paths": ["인구", "장래인구추계"]},
    {"table_key": "101:B", "tbl_name": "행정구역별 주민등록세대수",
     "category_paths": ["인구", "주민등록인구현황"]},
    {"table_key": "133:C", "tbl_name": "주유소 제품별 평균 판매가격",
     "category_paths": ["물가", "석유류"]},
    {"table_key": "101:D", "tbl_name": "소비자물가지수",
     "category_paths": ["물가", "소비자물가조사"]},
]


def _index():
    return LexicalIndex([dict(row) for row in TABLES])


def test_bigrams_ignore_spacing_so_korean_names_match():
    assert bigrams("노년 부양비") == bigrams("노년부양비")


def test_a_short_string_still_yields_something():
    assert bigrams("가") == ["가"]
    assert bigrams("") == []


def test_the_named_table_ranks_first():
    hits = _index().search("노년 부양비")

    assert hits[0]["table_key"] == "101:A"


def test_a_verbatim_indicator_beats_accumulated_overlap():
    """Without the substring bonus, long names outscore the exact table."""
    hits = _index().search("주유소 제품별 평균 판매가격")

    assert hits[0]["table_key"] == "133:C"


def test_an_unrelated_query_does_not_return_the_whole_index():
    hits = _index().search("어획량")

    assert all(hit["table_key"] != "133:C" for hit in hits[:1])


def test_an_empty_query_returns_nothing():
    assert _index().search("") == []


def test_extra_terms_contribute_to_the_score():
    plain = _index().search("지수", k=4)
    boosted = _index().search("지수", k=4, extra_terms=["소비자물가"])

    assert boosted[0]["table_key"] == "101:D"
    assert [hit["table_key"] for hit in boosted] != [] and plain is not None


def test_k_limits_the_result_size():
    assert len(_index().search("인구", k=2)) <= 2


def test_build_index_keeps_only_the_search_fields(tmp_path):
    registry = tmp_path / "registry.jsonl"
    registry.write_text(
        json.dumps({
            "table_key": "101:A", "tbl_name": "노년부양비", "org_id": "101",
            "tbl_id": "A", "category_paths": [["인구", "장래인구추계"]],
            "profile_present": True, "doc_meta_text": "버려짐",
            "source_metadata": [{"heavy": "x" * 100}],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "index.jsonl"

    manifest = build_index(registry, out)

    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert manifest["tables"] == 1
    assert row["category_paths"] == ["인구", "장래인구추계"]
    assert "source_metadata" not in row


def test_build_index_skips_rows_without_a_name(tmp_path):
    registry = tmp_path / "registry.jsonl"
    registry.write_text(
        json.dumps({"table_key": "101:A", "tbl_name": ""}) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "index.jsonl"

    assert build_index(registry, out)["tables"] == 0


def _dup_index():
    return LexicalIndex([
        {"table_key": "101:A1", "tbl_name": "노년부양비 및 노령화지수",
         "category_paths": ["인구"]},
        {"table_key": "101:A2", "tbl_name": "노년부양비 및 노령화지수",
         "category_paths": ["인구"]},
        {"table_key": "101:A3", "tbl_name": "노년부양비 및 노령화지수",
         "category_paths": ["인구"]},
        {"table_key": "101:C", "tbl_name": "장래인구추계 노년 부양 지표",
         "category_paths": ["인구"]},
    ])


def test_duplicate_names_do_not_fill_the_candidate_list():
    """The registry repeats a statistic per body and vintage."""
    hits = _dup_index().search("노년 부양비", k=3)

    assert len({hit["tbl_name"] for hit in hits}) == len(hits)
    assert len(hits) == 2


def test_the_collapsed_duplicates_are_counted_not_hidden():
    hits = _dup_index().search("노년 부양비", k=3)

    assert hits[0]["duplicate_tables"] == 3


def test_collapsing_can_be_switched_off():
    hits = _dup_index().search("노년 부양비", k=3, collapse_duplicate_names=False)

    assert len({hit["tbl_name"] for hit in hits}) < len(hits)
