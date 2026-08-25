"""Evaluate the gold-blind oracle-table runner against frozen plans afterward."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from src.develop.evaluate_r4c1_v2_checkpoint import (
    DEFAULT_PATHS,
    _independent_canonical_gold_plan,
    _jsonl,
    frame_to_profile,
    join_target_rows,
    validate_query_plan_inventory,
)
from src.develop.r4c1_claim_core_v2 import build_claim_core_v2
from src.develop.r4c1_article_context import (
    DEFAULT_ARTICLE_SOURCE,
    with_article_date_context,
)
from src.develop.run_r4c1_oracle_table_v1 import resolve_oracle_tables


CONTRACT_VERSION = "r4c1-oracle-table-only-evaluator-v1"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIVE_PROFILES = (
    ROOT / "data/develop/r4c1_oracle_table_live_metadata_20260819c/profiles.jsonl"
)


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def evaluate_oracle_table_v1(
    *,
    routed_path: str | Path = DEFAULT_PATHS["routed_frozen"],
    frame_path: str | Path = DEFAULT_PATHS["r4_frame"],
    frozen_plan_path: str | Path = DEFAULT_PATHS["r4_gold"],
    live_profiles_path: str | Path = DEFAULT_LIVE_PROFILES,
    article_source_path: str | Path | None = DEFAULT_ARTICLE_SOURCE,
) -> dict[str, Any]:
    routed_rows = with_article_date_context(_jsonl(routed_path), article_source_path)
    frame_rows = _jsonl(frame_path)
    frozen_rows = _jsonl(frozen_plan_path)
    joined = join_target_rows(routed_rows, frame_rows, frozen_rows)
    table_map = [
        {"target_id": str(frame["target_id"]), "table_key": str(frame["table_key"])}
        for _, frame, _ in joined
    ]
    live_profiles = _jsonl(live_profiles_path)
    predictions, runtime_report = resolve_oracle_tables(
        [routed for routed, _, _ in joined], table_map, live_profiles
    )
    predicted = {row["target_id"]: row for row in predictions}
    profile_by_table = {str(row.get("table_key")): row for row in live_profiles}

    exact = 0
    unsafe = 0
    expected_buildable = 0
    inventory_valid = 0
    query_ready = 0
    details: dict[str, dict[str, Any]] = {}
    hold_reasons: Counter[str] = Counter()
    for routed, frame, frozen in joined:
        target_id = str(frame["target_id"])
        prediction = predicted[target_id]
        resolution = prediction["resolution"]
        outcome = str(resolution.get("outcome") or "")
        frozen_status = str(frozen.get("resolution_status") or "")
        expected = (
            _independent_canonical_gold_plan(frozen, frame)
            if frozen_status == "QUERY_READY"
            else None
        )
        expected_buildable += int(expected is not None)
        is_exact = expected is not None and resolution.get("query_plan") == expected
        exact += int(is_exact)
        unsafe += int(outcome == "QUERY_READY" and frozen_status != "QUERY_READY")
        query_ready += int(outcome == "QUERY_READY")
        if outcome != "QUERY_READY":
            hold_reasons[str(resolution.get("hold_reason") or "UNKNOWN")] += 1
        inventory_errors: list[str] = []
        if outcome == "QUERY_READY" and resolution.get("query_plan"):
            profile = profile_by_table.get(str(frame["table_key"]))
            if profile is None:
                inventory_errors = ["PROFILE_UNAVAILABLE"]
            else:
                inventory_errors = validate_query_plan_inventory(
                    resolution["query_plan"], profile, build_claim_core_v2(routed)
                )
            inventory_valid += int(not inventory_errors)
        details[target_id] = {
            "table_key": frame["table_key"],
            "frozen_status": frozen_status,
            "prediction_outcome": outcome,
            "prediction_reason": resolution.get("hold_reason"),
            "full_query_plan_exact": is_exact,
            "inventory_errors": inventory_errors,
            "metadata_source": prediction["metadata_source"],
            "metadata_profile_sha256": prediction["metadata_profile_sha256"],
        }

    gold_qr = sum(str(frozen.get("resolution_status") or "") == "QUERY_READY" for _, _, frozen in joined)
    return {
        "contract_version": CONTRACT_VERSION,
        "oracle_scope": "table_only",
        "metadata_source": "KOSIS_METADATA_API",
        "targets": len(joined),
        "gold_query_ready_denominator": gold_qr,
        "full_query_plan_exact": {"numerator": exact, "denominator": gold_qr},
        "unsafe_ready": {"numerator": unsafe, "denominator": len(joined)},
        "query_ready": {"numerator": query_ready, "denominator": len(joined)},
        "independent_expected_buildable": {
            "numerator": expected_buildable,
            "denominator": gold_qr,
        },
        "ready_inventory_valid": {"numerator": inventory_valid, "denominator": query_ready},
        "hold_reason_distribution": dict(sorted(hold_reasons.items())),
        "runtime": runtime_report,
        "cell_api_calls": 0,
        "input_sha256": {
            "routed": _sha256_file(routed_path),
            "table_map_source_frame": _sha256_file(frame_path),
            "frozen_plan_evaluator_only": _sha256_file(frozen_plan_path),
            "live_profiles": _sha256_file(live_profiles_path),
            **(
                {"article_source": _sha256_file(article_source_path)}
                if article_source_path is not None
                else {}
            ),
        },
        "details": details,
    }


def main() -> None:
    print(json.dumps(evaluate_oracle_table_v1(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["evaluate_oracle_table_v1"]
