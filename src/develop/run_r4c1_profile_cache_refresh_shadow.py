"""Explicit stale/miss refresh gate for the R4-C1 persistent profile cache.

The default mode is a read-only audit.  Metadata calls and cache mutation are
possible only when ``execute=True`` and a fetcher is injected.  Cell data is
never requested here.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from src.develop.evaluate_r4c1_v2_checkpoint import _jsonl
from src.develop.r4c1_live_metadata import collect_live_profiles, make_default_api
from src.develop.r4c1_profile_cache import (
    CacheBundle,
    CacheLookup,
    PersistentProfileCache,
    _read_snapshot_bundles,
)
from src.develop.run_r4c1_search_cache_shadow import (
    DEFAULT_CACHE,
    DEFAULT_SEARCH_FINAL,
    _candidate_table_keys,
)


CONTRACT_VERSION = "r4c1-profile-cache-refresh-shadow-v1"


class RefreshShadowError(ValueError):
    """Raised when explicit refresh inputs or postconditions are invalid."""


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(table_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", table_key)


def make_snapshot_fetcher(
    api: Any,
    snapshot_root: str | Path,
    *,
    delay_seconds: float = 0.2,
) -> Callable[[str], CacheBundle]:
    """Create immutable one-table metadata snapshots for refreshed versions."""

    root = Path(snapshot_root)

    def fetch(table_key: str) -> CacheBundle:
        destination = root / _safe_name(table_key)
        report = collect_live_profiles(
            [table_key],
            destination,
            api,
            delay_seconds=delay_seconds,
        )
        if report.get("profile_success_count") != 1:
            raise RefreshShadowError(f"metadata refresh did not build one profile: {table_key}")
        bundles, _ = _read_snapshot_bundles(destination)
        if len(bundles) != 1 or bundles[0].profile.get("table_key") != table_key:
            raise RefreshShadowError(f"metadata refresh identity mismatch: {table_key}")
        return bundles[0]

    return fetch


def refresh_cache_shadow(
    cache: Any,
    table_keys: Iterable[str],
    *,
    max_age_seconds: float,
    execute: bool = False,
    fetcher: Callable[[str], CacheBundle] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Audit freshness and optionally promote explicit metadata refreshes."""

    keys = sorted({str(value) for value in table_keys if str(value)})
    if not keys:
        raise RefreshShadowError("at least one table_key is required")
    before: dict[str, CacheLookup] = {}
    for table_key in keys:
        before[table_key] = cache.lookup(
            table_key,
            max_age_seconds=max_age_seconds,
            now=now,
        )
    needs_refresh = [key for key in keys if before[key].status in {"STALE", "MISS"}]
    unexpected = sorted(
        {lookup.status for lookup in before.values()} - {"FRESH", "STALE", "MISS"}
    )
    if unexpected:
        raise RefreshShadowError(f"unexpected cache status: {unexpected}")
    if execute and needs_refresh and fetcher is None:
        raise RefreshShadowError("execute refresh requires an injected metadata fetcher")

    metadata_calls = 0
    refreshed: list[str] = []
    if execute:
        for table_key in needs_refresh:
            lookup, calls = cache.get_or_fetch(
                table_key,
                max_age_seconds=max_age_seconds,
                fetcher=fetcher,
                now=now,
            )
            if lookup.status != "FRESH" or lookup.profile is None:
                raise RefreshShadowError(f"refresh did not produce FRESH profile: {table_key}")
            metadata_calls += int(calls)
            refreshed.append(table_key)

    after = {
        table_key: cache.lookup(
            table_key,
            max_age_seconds=max_age_seconds,
            now=now,
        )
        for table_key in keys
    }
    before_counts = Counter(lookup.status for lookup in before.values())
    after_counts = Counter(lookup.status for lookup in after.values())
    if not execute and metadata_calls != 0:
        raise RefreshShadowError("audit-only mode cannot call metadata API")
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": "EXECUTE_METADATA_REFRESH" if execute else "AUDIT_ONLY",
        "table_count": len(keys),
        "max_age_seconds": max_age_seconds,
        "before_status_distribution": dict(sorted(before_counts.items())),
        "after_status_distribution": dict(sorted(after_counts.items())),
        "refresh_required_count": len(needs_refresh),
        "refresh_required_table_keys": needs_refresh,
        "refreshed_count": len(refreshed),
        "refreshed_table_keys": refreshed,
        "planned_metadata_api_calls": len(needs_refresh) * 3,
        "metadata_api_calls": metadata_calls,
        "cell_api_calls": 0,
        "profile_sha256_before": {
            key: before[key].profile_sha256 for key in keys if before[key].profile_sha256
        },
        "profile_sha256_after": {
            key: after[key].profile_sha256 for key in keys if after[key].profile_sha256
        },
    }


def write_refresh_shadow_report(
    *,
    search_final_path: str | Path,
    cache_path: str | Path,
    output_root: str | Path,
    max_age_seconds: float,
    execute: bool = False,
    fetcher: Callable[[str], CacheBundle] | None = None,
) -> dict[str, Any]:
    output_root = Path(output_root)
    report_path = output_root / "refresh_report.json"
    if report_path.exists():
        raise FileExistsError(f"refusing to overwrite refresh report: {report_path}")
    final_rows = _jsonl(search_final_path)
    table_keys = _candidate_table_keys(final_rows)
    cache_sha_before = _sha256_file(cache_path)
    report = refresh_cache_shadow(
        PersistentProfileCache(cache_path),
        table_keys,
        max_age_seconds=max_age_seconds,
        execute=execute,
        fetcher=fetcher,
    )
    report["input_sha256"] = {
        "search_final": _sha256_file(search_final_path),
        "profile_cache_before": cache_sha_before,
    }
    report["profile_cache_after_sha256"] = _sha256_file(cache_path)
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-final", type=Path, default=DEFAULT_SEARCH_FINAL)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=float, default=604800)
    parser.add_argument("--execute-metadata-api", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    fetcher = None
    if args.execute_metadata_api:
        api = make_default_api(timeout=args.timeout, retries=0)
        fetcher = make_snapshot_fetcher(
            api,
            args.output / "metadata_snapshots",
            delay_seconds=args.delay_seconds,
        )
    report = write_refresh_shadow_report(
        search_final_path=args.search_final,
        cache_path=args.cache,
        output_root=args.output,
        max_age_seconds=args.max_age_seconds,
        execute=args.execute_metadata_api,
        fetcher=fetcher,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RefreshShadowError",
    "make_snapshot_fetcher",
    "refresh_cache_shadow",
    "write_refresh_shadow_report",
]
