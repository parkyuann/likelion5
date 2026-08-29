from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.news_verification.runtime.operational_retrieval_v2 import (
    QuerySpec,
    RrfCandidate,
    build_query_register,
    rerank_top50,
)
from src.news_verification.runtime.v6_search_channels import (
    ItemOfficialKosisSearchChannel,
    bounded_item_suffixes,
)


def test_item_official_is_an_explicit_operational_opt_in():
    register = build_query_register({
        "indicator": "고용률",
        "item": ["취업자", "고용률"],
        "sentence": "고용률은 60%였다.",
        "_include_item_official": True,
    })
    item = next(query for query in register if query.query_id == "item")
    assert item.channels == ("bm25", "dense", "item_official")
    assert item.fields_by_channel["item_official"] == ("ITEM",)

    baseline = build_query_register({"indicator": "고용률", "item": "고용률"})
    baseline_item = next(query for query in baseline if query.query_id == "item")
    assert "item_official" not in baseline_item.channels


def test_bounded_item_suffixes_match_the_registered_rule():
    assert bounded_item_suffixes("a b c d e") == ("b c d e", "c d e", "d e")
    assert bounded_item_suffixes("a b") == ()


def test_item_official_folds_full_and_suffix_results_without_binding_authority():
    channel = ItemOfficialKosisSearchChannel("secret")
    calls: list[str] = []

    def fake_official(query: QuerySpec, fields: Any, top_k: int):
        del fields, top_k
        calls.append(query.text)
        if len(calls) == 1:
            return [{"table_key": "org:full", "score": 1.0}]
        return [
            {"table_key": "org:suffix", "score": 1.0},
            {"table_key": "org:full", "score": 0.5},
        ]

    channel._official = fake_official  # type: ignore[method-assign]
    query = QuerySpec(
        "item", "item", "a b c d", ("item_official",), {"item_official": ("ITEM",)}
    )
    rows = list(channel(query, ("ITEM",), 20))
    assert calls == ["a b c d", "b c d", "c d"]
    assert [row["table_key"] for row in rows] == ["org:full", "org:suffix"]
    assert all(row["field"] == "ITEM" for row in rows)
    assert all(row["source"] == "kosis_official_item" for row in rows)


def test_reranker_scope_is_capped_at_attested_service_limit():
    candidates = tuple(RrfCandidate(f"org:{index}", 1.0, index, ()) for index in range(60))
    passages = tuple({"candidate_id": candidate.table_key, "text": candidate.table_key} for candidate in candidates)
    seen: list[int] = []

    class FakeReranker:
        def rerank(self, query: str, rows: list[dict[str, str]]):
            assert query == "고용률"
            seen.append(len(rows))
            return [
                {"candidate_id": row["candidate_id"], "raw_logit": float(index), "sigmoid_score": 0.5}
                for index, row in enumerate(rows)
            ]

    result = rerank_top50("고용률", candidates, passages, FakeReranker())
    assert seen == [50]
    assert len(result) == 50
    assert result[0].table_key == "org:49"
