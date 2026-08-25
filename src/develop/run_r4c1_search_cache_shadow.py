"""Gold-blind R4-C1 search candidate + persistent profile cache runner."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.develop.evaluate_r4c1_kosis_search_shadow import resolve_search_shadow
from src.develop.evaluate_r4c1_v2_checkpoint import DEFAULT_PATHS, _jsonl
from src.develop.r4c1_article_context import DEFAULT_ARTICLE_SOURCE, with_article_date_context
from src.develop.r4c1_profile_cache import PersistentProfileCache


CONTRACT_VERSION = "r4c1-search-cache-shadow-runner-v1"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEARCH_FINAL = (
    ROOT / "data/develop/r4c1_kosis_search_shadow_20260819c/retrieval_final_output.jsonl"
)
DEFAULT_CACHE = ROOT / "data/develop/r4c1_profile_cache_20260819/r4c1_profiles.sqlite3"


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _candidate_table_keys(final_rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            str(candidate.get("table_key") or "")
            for row in final_rows
            for candidate in (
                row.get("retrieval", {}).get("candidates", [])
                if isinstance(row.get("retrieval"), Mapping)
                else []
            )
            if isinstance(candidate, Mapping) and candidate.get("table_key")
        }
    )


def run_cache_shadow(
    routed_rows: Iterable[Mapping[str, Any]],
    final_rows: Iterable[Mapping[str, Any]],
    cache: Any,
    *,
    max_age_seconds: float,
) -> dict[str, Any]:
    final = [dict(row) for row in final_rows]
    keys = _candidate_table_keys(final)
    cache_counter: Counter[str] = Counter()
    profiles: list[dict[str, Any]] = []
    profile_shas: dict[str, str] = {}
    for table_key in keys:
        lookup = cache.lookup(table_key, max_age_seconds=max_age_seconds)
        cache_counter[lookup.status] += 1
        if lookup.status == "FRESH" and lookup.profile is not None:
            profiles.append(lookup.profile)
            profile_shas[table_key] = str(lookup.profile_sha256 or "")
    runtime = resolve_search_shadow(routed_rows, final, profiles)
    output_rows = [
        {
            "target_id": target_id,
            "resolution": detail,
            "candidate_source": "KOSIS_INTEGRATED_SEARCH_API",
            "profile_source": "R4C1_PERSISTENT_PROFILE_CACHE",
        }
        for target_id, detail in sorted(runtime["details"].items())
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "targets": runtime["targets"],
        "candidates": runtime["candidates"],
        "distribution": runtime["distribution"],
        "cache": {
            "unique_table_keys": len(keys),
            "status_distribution": dict(sorted(cache_counter.items())),
            "profile_sha256_by_table": profile_shas,
            "max_age_seconds": max_age_seconds,
        },
        "profile_available": runtime["profile_available"],
        "profile_unavailable": runtime["profile_unavailable"],
        "metadata_api_calls": 0,
        "cell_api_calls": 0,
        "forbidden_runtime_inputs_accessed": [],
        "output_rows": output_rows,
    }


def write_cache_shadow_run(
    *,
    routed_path: str | Path,
    search_final_path: str | Path,
    cache_path: str | Path,
    output_root: str | Path,
    max_age_seconds: float,
    article_source_path: str | Path | None = DEFAULT_ARTICLE_SOURCE,
) -> dict[str, Any]:
    output_root = Path(output_root)
    report_path = output_root / "run_report.json"
    rows_path = output_root / "r4c1_shadow_output.jsonl"
    if report_path.exists() or rows_path.exists():
        raise FileExistsError(f"refusing to overwrite shadow output: {output_root}")
    routed = with_article_date_context(_jsonl(routed_path), article_source_path)
    final = _jsonl(search_final_path)
    report = run_cache_shadow(
        routed,
        final,
        PersistentProfileCache(cache_path),
        max_age_seconds=max_age_seconds,
    )
    rows = report.pop("output_rows")
    report["input_sha256"] = {
        "routed": _sha256_file(routed_path),
        "search_final": _sha256_file(search_final_path),
        "profile_cache": _sha256_file(cache_path),
        **(
            {"article_source": _sha256_file(article_source_path)}
            if article_source_path is not None
            else {}
        ),
    }
    output_root.mkdir(parents=True, exist_ok=True)
    rows_bytes = b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for row in rows
    )
    temporary_rows = rows_path.with_suffix(rows_path.suffix + ".tmp")
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary_rows.write_bytes(rows_bytes)
    report["output_sha256"] = hashlib.sha256(rows_bytes).hexdigest()
    temporary_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_rows, rows_path)
    os.replace(temporary_report, report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routed", type=Path, default=DEFAULT_PATHS["routed_live"])
    parser.add_argument("--search-final", type=Path, default=DEFAULT_SEARCH_FINAL)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=float, default=604800)
    parser.add_argument("--article-source", type=Path, default=DEFAULT_ARTICLE_SOURCE)
    args = parser.parse_args(argv)
    report = write_cache_shadow_run(
        routed_path=args.routed,
        search_final_path=args.search_final,
        cache_path=args.cache,
        output_root=args.output,
        max_age_seconds=args.max_age_seconds,
        article_source_path=args.article_source,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_cache_shadow", "write_cache_shadow_run"]
