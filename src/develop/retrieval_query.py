"""Turn the structured fields into the queries the retrieval stage runs.

팀 인계: 주장 구조화와 KOSIS 통계표 검색을 잇는 경계 모듈이다. 지표·품목·원문
문장 질의를 역할별로 분리해 출력한다.

This is the boundary artefact.  Everything upstream decides *what* a value
measures; this decides *what strings the retriever is asked to match*, and the
retriever team consumes it.  Keeping it in its own module means the query
contract can change without touching field composition.

Two findings from the 2026-08-03 adjudication (33 targets whose correct KOSIS
table is known) shape the contract.

* **The primary query is the indicator alone.**  Appending ``item`` and
  ``population`` — which the candidate generator was doing — *lowered* recall
  from 0.333 to 0.303.  Those fields name the axes a table is broken down by,
  and mixing them into the query pulls the match toward tables titled after the
  axis rather than the statistic.
* **The source sentence is worth a second query.**  Adding it recovered three
  targets the indicator alone missed (0.333 → 0.394) — cases like
  ``외국인의 국내 주식 투자 자금 순유출`` where the sentence carries vocabulary the
  indicator dropped.  A third and fourth variant added nothing.

Both numbers come from a 33-row dev sample, so the *direction* is supported and
the magnitude is not.  No variant is weighted or scored here: ranking across
variants belongs to the retriever.
"""

from __future__ import annotations

from typing import Any

PRIMARY = "indicator"
SENTENCE = "sentence"
ITEM = "item"


def build_query_variants(
    retrieval_fields: dict[str, Any],
    sentence_text: object = "",
) -> list[dict[str, str]]:
    """Return the queries for one value, most specific first.

    Order is the contract: a consumer that runs only the first query gets the
    single best-performing form, and each later variant is additive rather
    than a replacement.
    """
    variants: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(role: str, text: object) -> None:
        query = " ".join(str(text or "").split())
        if not query or query in seen:
            return
        seen.add(query)
        variants.append({"role": role, "query": query})

    add(PRIMARY, retrieval_fields.get("indicator"))
    # Only when it says something the indicator does not; an item that is a
    # substring of the indicator would just repeat the primary query.
    item = " ".join(retrieval_fields.get("item") or [])
    if item and item not in str(retrieval_fields.get("indicator") or ""):
        add(ITEM, item)
    add(SENTENCE, sentence_text)
    return variants


def attach_query_variants(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``retrieval_queries`` to composed rows, leaving fields untouched."""
    return [
        {
            **row,
            "retrieval_queries": build_query_variants(
                row.get("retrieval_fields") or {}, row.get("sentence_text"),
            ),
        }
        for row in rows
    ]
