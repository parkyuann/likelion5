"""Runtime profile refresh provider seam.

The live adapter depends on this narrow provider seam instead of importing
the experimental profile-cache refresh runner.  The shadow runner re-exports
the same factory for compatibility.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable

from src.news_verification.runtime.r4c1_live_metadata import collect_live_profiles
from src.news_verification.runtime.r4c1_profile_cache import CacheBundle, _read_snapshot_bundles


class ProfileProviderError(ValueError):
    pass


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
        report = collect_live_profiles([table_key], destination, api, delay_seconds=delay_seconds)
        if report.get("profile_success_count") != 1:
            raise ProfileProviderError(f"metadata refresh did not build one profile: {table_key}")
        bundles, _ = _read_snapshot_bundles(destination)
        if len(bundles) != 1 or bundles[0].profile.get("table_key") != table_key:
            raise ProfileProviderError(f"metadata refresh identity mismatch: {table_key}")
        return bundles[0]

    return fetch


__all__ = ["ProfileProviderError", "make_snapshot_fetcher"]


