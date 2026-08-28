"""Persistent, provenance-checked cache for official KOSIS table profiles.

The cache stores versioned TBL/ITM/PRD raw responses alongside the derived
profile.  A current pointer may advance, but prior versions are never deleted.
Reads recompute every hash and fail closed on corruption or staleness.  This
module never calls KOSIS or the cell API by itself; lazy refresh requires an
explicit injected metadata fetcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from src.news_verification.runtime.r4c1_live_metadata import ENDPOINTS


CONTRACT_VERSION = "r4c1-persistent-profile-cache-v1"
SCHEMA_VERSION = 1


class ProfileCacheError(ValueError):
    """Raised when cache provenance, freshness, or schema is invalid."""


@dataclass(frozen=True)
class CacheBundle:
    profile: dict[str, Any]
    raw_by_endpoint: dict[str, bytes]
    source_manifest_sha256: str


@dataclass(frozen=True)
class CacheLookup:
    status: str
    profile: dict[str, Any] | None
    profile_sha256: str | None
    age_seconds: float | None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _profile_sha(profile: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        _canonical_bytes(
            {
                key: value
                for key, value in profile.items()
                if key not in {"retrieved_at", "profile_sha256"}
            }
        )
    )


def _version_sha(profile: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        _canonical_bytes(
            {
                "profile_sha256": profile.get("profile_sha256"),
                "retrieved_at": profile.get("retrieved_at"),
                "response_sha256": profile.get("response_sha256"),
            }
        )
    )


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProfileCacheError(f"invalid retrieved_at: {value!r}") from error
    if parsed.tzinfo is None:
        raise ProfileCacheError("retrieved_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_bundle(bundle: CacheBundle) -> tuple[str, str, str, bytes]:
    profile = bundle.profile
    table_key = str(profile.get("table_key") or "")
    if not table_key or table_key.count(":") != 1:
        raise ProfileCacheError(f"invalid profile table_key: {table_key!r}")
    if profile.get("source") != "KOSIS_METADATA_API":
        raise ProfileCacheError("cache accepts only KOSIS_METADATA_API profiles")
    expected_profile_sha = str(profile.get("profile_sha256") or "")
    actual_profile_sha = _profile_sha(profile)
    if expected_profile_sha != actual_profile_sha:
        raise ProfileCacheError(f"profile SHA mismatch: {table_key}")
    if set(bundle.raw_by_endpoint) != set(ENDPOINTS):
        raise ProfileCacheError(f"raw endpoint set mismatch: {table_key}")
    response_sha = profile.get("response_sha256")
    if not isinstance(response_sha, Mapping) or set(response_sha) != set(ENDPOINTS):
        raise ProfileCacheError(f"profile response SHA set mismatch: {table_key}")
    for endpoint in ENDPOINTS:
        if _sha256_bytes(bundle.raw_by_endpoint[endpoint]) != response_sha[endpoint]:
            raise ProfileCacheError(f"raw SHA mismatch: {table_key} {endpoint}")
    _parse_time(str(profile.get("retrieved_at") or ""))
    source_manifest_sha = str(bundle.source_manifest_sha256 or "")
    if len(source_manifest_sha) != 64:
        raise ProfileCacheError("source manifest SHA must be sha256")
    return table_key, expected_profile_sha, _version_sha(profile), _canonical_bytes(profile)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE cache_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE profile_versions (
            version_sha256 TEXT PRIMARY KEY,
            profile_sha256 TEXT NOT NULL,
            table_key TEXT NOT NULL,
            profile_json BLOB NOT NULL,
            retrieved_at TEXT NOT NULL,
            cached_at TEXT NOT NULL,
            source_manifest_sha256 TEXT NOT NULL
        );
        CREATE INDEX profile_versions_table_key
            ON profile_versions(table_key, retrieved_at);
        CREATE TABLE endpoint_versions (
            version_sha256 TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            raw_bytes BLOB NOT NULL,
            PRIMARY KEY(version_sha256, endpoint),
            FOREIGN KEY(version_sha256) REFERENCES profile_versions(version_sha256)
        );
        CREATE TABLE current_profiles (
            table_key TEXT PRIMARY KEY,
            version_sha256 TEXT NOT NULL,
            FOREIGN KEY(version_sha256) REFERENCES profile_versions(version_sha256)
        );
        """
    )
    connection.executemany(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
        [
            ("contract_version", CONTRACT_VERSION),
            ("schema_version", str(SCHEMA_VERSION)),
            ("cell_api_calls", "0"),
        ],
    )


class PersistentProfileCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        meta = dict(connection.execute("SELECT key, value FROM cache_meta"))
        if meta.get("contract_version") != CONTRACT_VERSION:
            connection.close()
            raise ProfileCacheError("cache contract version mismatch")
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            connection.close()
            raise ProfileCacheError("cache schema version mismatch")
        return connection

    def put(self, bundle: CacheBundle, *, cached_at: datetime | None = None) -> str:
        table_key, profile_sha, version_sha, profile_json = _validate_bundle(bundle)
        timestamp = (cached_at or _utc_now()).astimezone(timezone.utc).isoformat()
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """INSERT OR IGNORE INTO profile_versions
                       (version_sha256, profile_sha256, table_key, profile_json, retrieved_at, cached_at, source_manifest_sha256)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        version_sha,
                        profile_sha,
                        table_key,
                        profile_json,
                        str(bundle.profile["retrieved_at"]),
                        timestamp,
                        bundle.source_manifest_sha256,
                    ),
                )
                for endpoint in ENDPOINTS:
                    raw = bundle.raw_by_endpoint[endpoint]
                    connection.execute(
                        """INSERT OR IGNORE INTO endpoint_versions
                           (version_sha256, endpoint, raw_sha256, raw_bytes)
                           VALUES (?, ?, ?, ?)""",
                        (version_sha, endpoint, _sha256_bytes(raw), raw),
                    )
                connection.execute(
                    """INSERT INTO current_profiles(table_key, version_sha256)
                       VALUES (?, ?)
                       ON CONFLICT(table_key) DO UPDATE SET version_sha256=excluded.version_sha256""",
                    (table_key, version_sha),
                )
        finally:
            connection.close()
        return profile_sha

    def lookup(
        self,
        table_key: str,
        *,
        max_age_seconds: float,
        now: datetime | None = None,
    ) -> CacheLookup:
        if max_age_seconds < 0:
            raise ProfileCacheError("max_age_seconds must be non-negative")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT p.* FROM current_profiles c
                   JOIN profile_versions p ON p.version_sha256=c.version_sha256
                   WHERE c.table_key=?""",
                (table_key,),
            ).fetchone()
            if row is None:
                return CacheLookup("MISS", None, None, None)
            profile_bytes = bytes(row["profile_json"])
            profile = json.loads(profile_bytes.decode("utf-8"))
            if not isinstance(profile, dict):
                raise ProfileCacheError(f"cached profile is not an object: {table_key}")
            profile_sha = str(row["profile_sha256"])
            if _profile_sha(profile) != profile_sha or profile.get("profile_sha256") != profile_sha:
                raise ProfileCacheError(f"cached profile SHA mismatch: {table_key}")
            endpoint_rows = connection.execute(
                "SELECT endpoint, raw_sha256, raw_bytes FROM endpoint_versions WHERE version_sha256=?",
                (row["version_sha256"],),
            ).fetchall()
            if {row["endpoint"] for row in endpoint_rows} != set(ENDPOINTS):
                raise ProfileCacheError(f"cached endpoint set mismatch: {table_key}")
            response_sha = profile.get("response_sha256") or {}
            for endpoint_row in endpoint_rows:
                raw = bytes(endpoint_row["raw_bytes"])
                raw_sha = _sha256_bytes(raw)
                endpoint = str(endpoint_row["endpoint"])
                if raw_sha != endpoint_row["raw_sha256"] or raw_sha != response_sha.get(endpoint):
                    raise ProfileCacheError(f"cached raw SHA mismatch: {table_key} {endpoint}")
            current = (now or _utc_now()).astimezone(timezone.utc)
            age = max(0.0, (current - _parse_time(str(row["retrieved_at"]))).total_seconds())
            status = "FRESH" if age <= max_age_seconds else "STALE"
            return CacheLookup(status, profile, profile_sha, age)
        finally:
            connection.close()

    def get_or_fetch(
        self,
        table_key: str,
        *,
        max_age_seconds: float,
        fetcher: Callable[[str], CacheBundle],
        now: datetime | None = None,
    ) -> tuple[CacheLookup, int]:
        current = self.lookup(table_key, max_age_seconds=max_age_seconds, now=now)
        if current.status == "FRESH":
            return current, 0
        bundle = fetcher(table_key)
        if str(bundle.profile.get("table_key") or "") != table_key:
            raise ProfileCacheError("fetcher returned a different table_key")
        self.put(bundle, cached_at=now)
        refreshed = self.lookup(table_key, max_age_seconds=max_age_seconds, now=now)
        if refreshed.status != "FRESH":
            raise ProfileCacheError("refreshed profile is not fresh")
        return refreshed, len(ENDPOINTS)


def _read_snapshot_bundles(snapshot_root: Path) -> tuple[list[CacheBundle], dict[str, Any]]:
    manifest_path = snapshot_root / "manifest.json"
    profiles_path = snapshot_root / "profiles.jsonl"
    checkpoint_path = snapshot_root / "raw_responses/checkpoint.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source") != "KOSIS_METADATA_API":
        raise ProfileCacheError("snapshot source must be KOSIS_METADATA_API")
    if manifest.get("profiles_sha256") != _sha256_file(profiles_path):
        raise ProfileCacheError("snapshot profiles SHA mismatch")
    checkpoint: dict[tuple[str, str], dict[str, Any]] = {}
    for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        identity = (str(row.get("table_key") or ""), str(row.get("endpoint") or ""))
        if not all(identity) or identity in checkpoint or row.get("status") != "OK":
            raise ProfileCacheError(f"invalid snapshot checkpoint identity: {identity}")
        checkpoint[identity] = row
    manifest_sha = _sha256_file(manifest_path)
    bundles: list[CacheBundle] = []
    for line in profiles_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        profile = json.loads(line)
        table_key = str(profile.get("table_key") or "")
        raw_by_endpoint: dict[str, bytes] = {}
        for endpoint in ENDPOINTS:
            row = checkpoint.get((table_key, endpoint))
            if row is None:
                raise ProfileCacheError(f"snapshot endpoint missing: {table_key} {endpoint}")
            raw_path = snapshot_root / row["raw_path"]
            raw = raw_path.read_bytes()
            if _sha256_bytes(raw) != row.get("raw_sha256"):
                raise ProfileCacheError(f"snapshot raw SHA mismatch: {table_key} {endpoint}")
            raw_by_endpoint[endpoint] = raw
        bundle = CacheBundle(profile, raw_by_endpoint, manifest_sha)
        _validate_bundle(bundle)
        bundles.append(bundle)
    if len(bundles) != manifest.get("profile_success_count"):
        raise ProfileCacheError("snapshot profile count mismatch")
    return bundles, manifest


def seed_profile_cache(
    snapshot_root: str | Path,
    database_path: str | Path,
    output_manifest_path: str | Path,
    *,
    cached_at: datetime | None = None,
) -> dict[str, Any]:
    snapshot_root = Path(snapshot_root)
    database_path = Path(database_path)
    output_manifest_path = Path(output_manifest_path)
    temporary = database_path.with_suffix(database_path.suffix + ".tmp")
    if database_path.exists() or temporary.exists() or output_manifest_path.exists():
        raise FileExistsError("refusing to overwrite persistent cache output")
    bundles, source_manifest = _read_snapshot_bundles(snapshot_root)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        _create_schema(connection)
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, database_path)
    cache = PersistentProfileCache(database_path)
    for bundle in bundles:
        cache.put(bundle, cached_at=cached_at)
    connection = cache._connect()
    try:
        profile_versions = connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0]
        endpoint_versions = connection.execute("SELECT COUNT(*) FROM endpoint_versions").fetchone()[0]
        current_profiles = connection.execute("SELECT COUNT(*) FROM current_profiles").fetchone()[0]
    finally:
        connection.close()
    report = {
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "source": "KOSIS_METADATA_API",
        "source_snapshot_manifest_sha256": _sha256_file(snapshot_root / "manifest.json"),
        "source_profiles_sha256": source_manifest["profiles_sha256"],
        "profile_versions": profile_versions,
        "endpoint_versions": endpoint_versions,
        "current_profiles": current_profiles,
        "database_sha256": _sha256_file(database_path),
        "cell_api_calls": 0,
    }
    output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = output_manifest_path.with_suffix(output_manifest_path.suffix + ".tmp")
    temporary_manifest.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_manifest, output_manifest_path)
    return report


__all__ = [
    "CONTRACT_VERSION",
    "CacheBundle",
    "CacheLookup",
    "PersistentProfileCache",
    "ProfileCacheError",
    "seed_profile_cache",
]




