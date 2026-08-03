from src.develop.build_stratum_candidates import (
    _surface_hits,
    classify_org_role,
    org_role_summary,
)

INDEX = [("고용노동부", "고용노동부"), ("노동부", "고용노동부"),
         ("통계청", "국가데이터처"), ("국세청", "국세청")]


def test_foreign_marker_suppresses_korean_alias():
    """`미 노동부` is the US department, not 고용노동부."""
    text = "미국의 2월 비농업 일자리가 15만1000개 늘었다고 미 노동부가 7일 밝혔다."

    hits = _surface_hits(text, INDEX)

    assert hits == []


def test_domestic_mention_still_matches_when_foreign_one_exists():
    text = (
        "미 노동부는 고용지표를 발표했다. "
        "노동부는 국내 고용동향을 함께 공표했다."
    )

    surfaces = {hit["surface"] for hit in _surface_hits(text, INDEX)}

    assert "노동부" in surfaces


def test_plain_domestic_mention_matches():
    text = "16일 통계청은 '2025년 6월 고용 동향'에서 취업자를 발표했다."

    surfaces = {hit["surface"] for hit in _surface_hits(text, INDEX)}

    assert "통계청" in surfaces


def test_org_name_embedded_in_longer_word_is_not_a_mention():
    """`신한은행` contains `한은` but is not 한국은행."""
    index = [("한은", "한국은행"), ("한국은행", "한국은행")]
    text = "신한은행이 쏠트래블 체크카드 200만장을 발급했다고 밝혔다."

    assert _surface_hits(text, index) == []


def test_standalone_alias_still_matches():
    index = [("한은", "한국은행")]
    text = "한은은 기준금리를 동결했다."

    assert [hit["surface"] for hit in _surface_hits(text, index)] == ["한은"]


def test_foreign_country_before_ministry_is_suppressed():
    index = [("국방부", "국방부")]
    text = "KAI는 필리핀 국방부와 FA-50 수출 계약을 체결했다."

    assert _surface_hits(text, index) == []


def test_classify_org_role_separates_source_from_actor():
    assert classify_org_role(
        "통계청에 따르면 지난달 취업자는 2909만명이다."
    ) == "통계출처"
    assert classify_org_role(
        "국세청은 업체 46곳을 대상으로 세무조사에 나선다고 밝혔다."
    ) == "행위주체"
    assert classify_org_role("국세청 관계자가 참석했다.") == "불명"


def test_org_role_summary_counts_source_shaped_sentences():
    text = (
        "통계청에 따르면 취업자는 2909만명이다. "
        "국세청은 세무조사에 착수했다."
    )
    hits = [{"surface": "통계청", "canonical_org": "국가데이터처"},
            {"surface": "국세청", "canonical_org": "국세청"}]

    summary = org_role_summary(text, hits)

    assert summary["통계출처"] == 1
    assert summary["행위주체"] == 1
    assert summary["source_shaped"] is True


def test_org_role_summary_flags_actor_only_article():
    text = "국세청은 업체 46곳을 대상으로 세무조사에 나선다고 밝혔다."
    hits = [{"surface": "국세청", "canonical_org": "국세청"}]

    summary = org_role_summary(text, hits)

    assert summary["source_shaped"] is False
    assert summary["행위주체"] == 1
