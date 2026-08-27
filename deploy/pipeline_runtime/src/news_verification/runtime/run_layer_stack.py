"""Run L3→L4→L5 over one split's L2 predictions and score against gold.

팀 인계: HCX 이후 단계를 결정론적으로 재현하는 실행 진입점이다. 고정된 기사와
L2 결과를 입력받아 routing 및 검색 질의를 다시 생성한다.

Gate B is measured once, so the measurement has to be reproducible from
artefacts rather than from a scratch script.  Everything after L2 is
deterministic, so this reruns to the same numbers given the same inputs.

No labelling and no model call happens here.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator, Callable

try:
    from .l3_role_assignment import assign_roles, assignment_summary
    from .l4_field_normalization import compose_all
    from .l5_routing import DEFAULT_THRESHOLD, evaluate_routing, route_all, routing_summary
    from .retrieval_query import attach_query_variants
except ImportError:  # pragma: no cover - direct script execution
    from l3_role_assignment import assign_roles, assignment_summary
    from l4_field_normalization import compose_all
    from l5_routing import DEFAULT_THRESHOLD, evaluate_routing, route_all, routing_summary
    from retrieval_query import attach_query_variants


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def run_stack(
    articles: list[dict[str, Any]],
    l2_rows: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    sentence_span_iterator: Callable[[str], Iterator[tuple[int, int, int, str]]] | None = None,
) -> list[dict[str, Any]]:
    """Assign roles, compose fields and route every value candidate."""
    layout_by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in l2_rows:
        layout_by_article[str(row.get("article_idx"))].append(row)

    published = {
        str(article.get("article_idx")): article.get("date")
        for article in articles
    }
    assignments: list[dict[str, Any]] = []
    for article in articles:
        article_idx = str(article.get("article_idx"))
        role_kwargs = ({"sentence_span_iterator": sentence_span_iterator}
                       if sentence_span_iterator is not None else {})
        for assignment in assign_roles(
            str(article.get("article_text") or ""),
            layout_by_article.get(article_idx, []),
            **role_kwargs,
        ):
            assignment["article_idx"] = article_idx
            assignments.append(assignment)
    routed = route_all(compose_all(assignments, published), threshold=threshold)
    # The queries are the handoff artefact, so they are produced here rather
    # than by whoever consumes the file.
    return attach_query_variants(routed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=Path, required=True)
    parser.add_argument("--l2", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    articles = read_jsonl(args.articles)
    routed = run_stack(
        articles, read_jsonl(args.l2), threshold=args.threshold,
    )
    write_jsonl(args.output, routed)

    summary = {
        "articles": len(articles),
        "threshold": args.threshold,
        "assignment": assignment_summary(routed),
        "routing": routing_summary(routed),
    }
    if args.summary:
        args.summary.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=1))

    if args.gold:
        evaluation = evaluate_routing(read_jsonl(args.gold), routed)
        if args.evaluation:
            args.evaluation.write_text(
                json.dumps(evaluation, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        print(json.dumps(evaluation, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()


