"""Evaluate the official KOSIS-search R4-C1 shadow without cell access."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.develop.evaluate_r4c1_oracle_table_v1 import evaluate_oracle_table_v1
from src.develop.evaluate_r4c1_v2_checkpoint import (
    DEFAULT_PATHS,
    _independent_canonical_gold_plan,
    _jsonl,
    _routed_index,
    validate_query_plan_inventory,
)
from src.develop.r4c1_article_context import DEFAULT_ARTICLE_SOURCE, with_article_date_context
from src.develop.r4c1_claim_core_v2 import build_claim_core_v2
from src.develop.r4c1_projection_v2 import project_candidate_v2, validate_target_v2


CONTRACT_VERSION = "r4c1-kosis-search-shadow-evaluator-v1"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEARCH_FINAL = (
    ROOT / "data/develop/r4c1_kosis_search_shadow_20260819c/retrieval_final_output.jsonl"
)
DEFAULT_SEARCH_PROFILES = (
    ROOT / "data/develop/r4c1_kosis_search_live_metadata_20260819/profiles.jsonl"
)


class SearchShadowEvaluationError(ValueError):
    """Raised when immutable shadow inputs cannot be joined exactly."""


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _unique_index(
    rows: Iterable[Mapping[str, Any]], key: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(key) or "")
        if not identity or identity in result:
            raise SearchShadowEvaluationError(f"missing or duplicate {key}: {identity!r}")
        result[identity] = dict(row)
    return result


def resolve_search_shadow(
    routed_rows: Iterable[Mapping[str, Any]],
    final_rows: Iterable[Mapping[str, Any]],
    profile_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Seal runtime outcomes before any frozen expected artifact is read."""

    routed = _routed_index(routed_rows)
    profiles = _unique_index(profile_rows, "table_key")
    final = _unique_index(final_rows, "target_id")
    distribution: Counter[str] = Counter()
    details: dict[str, dict[str, Any]] = {}
    candidate_count = 0
    profile_available = 0
    for target_id, row in sorted(final.items()):
        if target_id not in routed:
            raise SearchShadowEvaluationError(f"target missing from routed: {target_id}")
        retrieval = row.get("retrieval")
        candidates = (
            list(retrieval.get("candidates") or [])
            if isinstance(retrieval, Mapping)
            else []
        )
        candidate_count += len(candidates)
        core = build_claim_core_v2(routed[target_id])
        projections = []
        candidate_keys: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            table_key = str(candidate.get("table_key") or "")
            candidate_keys.append(table_key)
            profile = profiles.get(table_key)
            profile_available += int(profile is not None)
            projections.append(project_candidate_v2(core, profile))
        if projections:
            resolution = validate_target_v2(projections)
            outcome = resolution.outcome
            reason = resolution.hold_reason
            query_plan = resolution.query_plan
            chosen_table = resolution.chosen_table_key
        else:
            outcome = "HOLD"
            reason = "NO_CANDIDATES"
            query_plan = None
            chosen_table = None
        distribution[outcome if outcome == "QUERY_READY" else str(reason)] += 1
        inventory_errors: list[str] = []
        if query_plan is not None and chosen_table in profiles:
            inventory_errors = validate_query_plan_inventory(
                query_plan, profiles[chosen_table], core
            )
        details[target_id] = {
            "outcome": outcome,
            "hold_reason": reason,
            "candidate_count": len(candidate_keys),
            "candidate_table_keys": candidate_keys,
            "chosen_table_key": chosen_table,
            "query_plan": query_plan,
            "inventory_errors": inventory_errors,
        }
    return {
        "targets": len(final),
        "candidates": candidate_count,
        "profile_available": profile_available,
        "profile_unavailable": candidate_count - profile_available,
        "distribution": dict(sorted(distribution.items())),
        "details": details,
    }


def evaluate_search_shadow(
    *,
    routed_path: str | Path = DEFAULT_PATHS["routed_live"],
    search_final_path: str | Path = DEFAULT_SEARCH_FINAL,
    profiles_path: str | Path = DEFAULT_SEARCH_PROFILES,
    frame_path: str | Path = DEFAULT_PATHS["r4_frame"],
    frozen_path: str | Path = DEFAULT_PATHS["r4_gold"],
    article_source_path: str | Path | None = DEFAULT_ARTICLE_SOURCE,
) -> dict[str, Any]:
    routed_rows = with_article_date_context(_jsonl(routed_path), article_source_path)
    final_rows = _jsonl(search_final_path)
    profile_rows = _jsonl(profiles_path)
    runtime = resolve_search_shadow(routed_rows, final_rows, profile_rows)

    # Expected-side artifacts are joined only after runtime outcomes are sealed.
    frame = _unique_index(_jsonl(frame_path), "target_id")
    frozen = _unique_index(_jsonl(frozen_path), "target_id")
    if set(frame) != set(frozen):
        raise SearchShadowEvaluationError("frame/frozen target mismatch")
    details = runtime["details"]
    gold_qr_ids = sorted(
        target_id
        for target_id, row in frozen.items()
        if str(row.get("resolution_status") or "") == "QUERY_READY"
    )
    search_hits = 0
    exact = 0
    for target_id in gold_qr_ids:
        detail = details.get(target_id, {})
        search_hits += int(str(frame[target_id]["table_key"]) in detail.get("candidate_table_keys", []))
        expected = _independent_canonical_gold_plan(frozen[target_id], frame[target_id])
        exact += int(expected is not None and detail.get("query_plan") == expected)

    unsafe_ids = sorted(
        target_id
        for target_id, gold in frozen.items()
        if details.get(target_id, {}).get("outcome") == "QUERY_READY"
        and str(gold.get("resolution_status") or "") != "QUERY_READY"
    )
    ready_ids = sorted(
        target_id for target_id, row in details.items() if row["outcome"] == "QUERY_READY"
    )
    inventory_valid_ids = sorted(
        target_id for target_id in ready_ids if not details[target_id]["inventory_errors"]
    )
    unlabeled_ready_ids = sorted(set(ready_ids) - set(frozen))

    oracle = evaluate_oracle_table_v1()
    proven_ids = sorted(
        target_id
        for target_id, row in oracle["details"].items()
        if row.get("full_query_plan_exact") is True
    )
    proven_hits = sum(
        str(frame[target_id]["table_key"])
        in details.get(target_id, {}).get("candidate_table_keys", [])
        for target_id in proven_ids
    )

    return {
        "contract_version": CONTRACT_VERSION,
        "candidate_source": "KOSIS_INTEGRATED_SEARCH_API",
        "metadata_source": "KOSIS_METADATA_API",
        "runtime": runtime,
        "query_ready": {"numerator": len(ready_ids), "denominator": runtime["targets"]},
        "ready_target_ids": ready_ids,
        "ready_inventory_valid": {
            "numerator": len(inventory_valid_ids),
            "denominator": len(ready_ids),
        },
        "unsafe_ready": {"numerator": len(unsafe_ids), "denominator": len(frozen)},
        "unsafe_target_ids": unsafe_ids,
        "unlabeled_ready_target_ids": unlabeled_ready_ids,
        "gold_query_ready_search_recall": {
            "numerator": search_hits,
            "denominator": len(gold_qr_ids),
        },
        "oracle_proven_exact_search_recall": {
            "numerator": proven_hits,
            "denominator": len(proven_ids),
        },
        "full_query_plan_exact": {"numerator": exact, "denominator": len(gold_qr_ids)},
        "cell_api_calls": 0,
        "input_sha256": {
            "routed": _sha256_file(routed_path),
            "search_final": _sha256_file(search_final_path),
            "profiles": _sha256_file(profiles_path),
            "frame_evaluator_only": _sha256_file(frame_path),
            "frozen_evaluator_only": _sha256_file(frozen_path),
            **(
                {"article_source": _sha256_file(article_source_path)}
                if article_source_path is not None
                else {}
            ),
        },
    }


def main() -> None:
    print(json.dumps(evaluate_search_shadow(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["evaluate_search_shadow", "resolve_search_shadow"]
