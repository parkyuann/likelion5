"""Concrete live I/O adapters for operational pipeline v2."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from src.news_verification.runtime.r4c1_live_metadata import collect_live_profiles, make_default_api
from src.news_verification.runtime.r4c1_profile_cache import PersistentProfileCache, _read_snapshot_bundles
from src.news_verification.runtime.services.profile_provider import make_snapshot_fetcher


class LiveAdapterError(ValueError):
    pass


class MonotonicTimingRecorder:
    """Request-local timing recorder with a deliberately small public surface."""

    CONTRACT_VERSION = "pipeline-timing-v1"

    def __init__(self) -> None:
        self._started = time.monotonic_ns()
        self._spans: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _ms(start_ns: int, end_ns: int) -> int:
        return max(0, int(round((end_ns - start_ns) / 1_000_000)))

    def observe(self, name: str, started_ns: int, *, calls: int = 1, **counts: int) -> int:
        duration = self._ms(started_ns, time.monotonic_ns())
        with self._lock:
            record = self._spans.setdefault(str(name), {"sum_ms": 0, "calls": 0})
            record["sum_ms"] += duration
            record["calls"] += max(0, int(calls))
            for key, value in counts.items():
                record[key] = record.get(key, 0) + max(0, int(value))
        return duration

    def span(self, name: str, *, calls: int = 1):
        recorder = self
        started = time.monotonic_ns()

        class _Span:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                recorder.observe(name, started, calls=calls)
                return False

        return _Span()

    def snapshot(self, *, total_started_ns: int | None = None) -> dict[str, Any]:
        with self._lock:
            stages = {name: dict(values) for name, values in sorted(self._spans.items())}
        started = self._started if total_started_ns is None else total_started_ns
        return {
            "contract_version": self.CONTRACT_VERSION,
            "total_wall_ms": self._ms(started, time.monotonic_ns()),
            "stages": stages,
        }


def _normalized_cache_text(value: Any) -> str:
    # Must match backend.query_encoder.normalize_encoder_query exactly.
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


class RequestScopedProfileProvider:
    """Positive/negative profile memoization for exactly one live request."""

    def __init__(self, inner: Callable[[str], Mapping[str, Any] | None]) -> None:
        self.inner = inner
        self._cache: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        self._lock = threading.RLock()
        self.logical_lookups = 0
        self.physical_lookups = 0
        self.cache_hits = 0
        self.negative_hits = 0

    def _key(self, table_key: str) -> tuple[str, str]:
        release_id = str(getattr(self.inner, "release_id", "") or "")
        return release_id, str(table_key)

    def __call__(self, table_key: str) -> Mapping[str, Any] | None:
        key = self._key(table_key)
        with self._lock:
            self.logical_lookups += 1
            if key in self._cache:
                self.cache_hits += 1
                if self._cache[key] is None:
                    self.negative_hits += 1
                return deepcopy(self._cache[key]) if self._cache[key] is not None else None
        self.physical_lookups += 1
        try:
            result = self.inner(str(table_key))
        except Exception:
            raise
        snapshot = deepcopy(dict(result)) if isinstance(result, Mapping) else None
        if snapshot is not None:
            expected_release = str(getattr(self.inner, "release_id", "") or "")
            actual_release = str(snapshot.get("release_id") or "")
            actual_table = str(snapshot.get("table_key") or "")
            profile_sha = str(snapshot.get("profile_sha256") or "")
            if expected_release and actual_release != expected_release:
                raise LiveAdapterError("METADATA_PROFILE_CONTRACT_MISMATCH")
            if actual_table and actual_table != str(table_key):
                raise LiveAdapterError("METADATA_PROFILE_CONTRACT_MISMATCH")
            if profile_sha and not re.fullmatch(r"[0-9a-fA-F]{64}", profile_sha):
                raise LiveAdapterError("METADATA_PROFILE_CONTRACT_MISMATCH")
        with self._lock:
            self._cache[key] = snapshot
        return deepcopy(snapshot) if snapshot is not None else None

    def prefetch(self, table_keys: Iterable[str]) -> dict[str, Mapping[str, Any] | None]:
        keys = sorted({str(value) for value in table_keys if str(value)})
        return {key: self(key) for key in keys}

    def speculative(self, table_key: str, *, timeout_seconds: float) -> Mapping[str, Any] | None:
        native = getattr(self.inner, "speculative", None)
        if not callable(native) or timeout_seconds <= 0:
            raise LiveAdapterError("SPECULATIVE_NATIVE_TIMEOUT_UNSUPPORTED")
        key = self._key(table_key)
        with self._lock:
            self.logical_lookups += 1
            if key in self._cache:
                self.cache_hits += 1
                return deepcopy(self._cache[key]) if self._cache[key] is not None else None
        self.physical_lookups += 1
        result = native(str(table_key), timeout_seconds=timeout_seconds)
        snapshot = deepcopy(dict(result)) if isinstance(result, Mapping) else None
        with self._lock:
            self._cache[key] = snapshot
        return deepcopy(snapshot) if snapshot is not None else None

    @property
    def metadata_api_calls(self) -> int:
        return int(getattr(self.inner, "metadata_api_calls", 0) or 0)

    @property
    def lookups(self) -> int:
        return self.logical_lookups

    def audit(self) -> dict[str, Any]:
        inner_audit = getattr(self.inner, "audit", None)
        return {
            "contract": "request-scoped-profile-cache-v1",
            "logical_lookups": self.logical_lookups,
            "physical_lookups": self.physical_lookups,
            "cache_hits": self.cache_hits,
            "negative_hits": self.negative_hits,
            "inner": inner_audit() if callable(inner_audit) else {},
        }


class RequestScopedEncoder:
    """Request-local normalized query-vector cache; invalid vectors are never cached."""

    def __init__(self, inner: Callable[[str], Sequence[float]]) -> None:
        self.inner = inner
        self._cache: dict[tuple[str, str, int, str], tuple[float, ...]] = {}
        self._lock = threading.RLock()
        self.logical_calls = 0
        self.physical_calls = 0
        self.cache_hits = 0

    def __call__(self, text: str) -> Sequence[float]:
        normalized = _normalized_cache_text(text)
        query_sha = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        model_id = str(getattr(self.inner, "model_id", "") or "")
        revision = str(getattr(self.inner, "model_revision", "") or "")
        vector_size = int(getattr(self.inner, "vector_size", 1024) or 1024)
        key = (model_id, revision, vector_size, query_sha)
        with self._lock:
            self.logical_calls += 1
            cached = self._cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached
        self.physical_calls += 1
        value = self.inner(text)
        evidence: Mapping[str, Any] | None = None
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], Mapping):
            vector = value[0]
            evidence = value[1]
        else:
            vector = value
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)) or len(vector) != vector_size:
            raise LiveAdapterError("QUERY_ENCODER_CONTRACT_MISMATCH")
        result = tuple(float(item) for item in vector)
        if any(not math.isfinite(item) for item in result):
            raise LiveAdapterError("QUERY_ENCODER_CONTRACT_MISMATCH")
        if evidence is not None and evidence.get("normalized") is not True:
            raise LiveAdapterError("QUERY_ENCODER_CONTRACT_MISMATCH")
        norm = math.sqrt(sum(item * item for item in result))
        if not 0.999 <= norm <= 1.001:
            raise LiveAdapterError("QUERY_ENCODER_CONTRACT_MISMATCH")
        with self._lock:
            self._cache[key] = result
        return result

    def speculative(self, text: str, *, timeout_seconds: float) -> Sequence[float]:
        native = getattr(self.inner, "speculative", None)
        if not callable(native) or timeout_seconds <= 0:
            raise LiveAdapterError("SPECULATIVE_NATIVE_TIMEOUT_UNSUPPORTED")
        value = native(text, timeout_seconds=timeout_seconds)
        vector = value[0] if isinstance(value, tuple) and len(value) == 2 else value
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)) or len(vector) != 1024:
            raise LiveAdapterError("QUERY_ENCODER_CONTRACT_MISMATCH")
        return tuple(float(item) for item in vector)

    @property
    def calls(self) -> int:
        return self.physical_calls

    def audit(self) -> dict[str, Any]:
        return {
            "contract": "request-scoped-query-vector-cache-v1",
            "logical_calls": self.logical_calls,
            "physical_calls": self.physical_calls,
            "cache_hits": self.cache_hits,
        }


class RequestScopedRetrievalCache:
    """Immutable request-local retrieval result cache."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Any, Mapping[str, Any]]] = {}
        self._lock = threading.RLock()
        self.logical_calls = 0
        self.physical_calls = 0
        self.cache_hits = 0

    def get_or_call(self, identity: Any, callback: Callable[[], tuple[Sequence[Any], Mapping[str, Any]]]):
        key = identity if isinstance(identity, str) else json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        with self._lock:
            self.logical_calls += 1
            cached = self._cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return tuple(deepcopy(cached[0])), deepcopy(dict(cached[1]))
        value = callback()
        candidates, audit = value
        snapshot = (tuple(deepcopy(tuple(candidates))), deepcopy(dict(audit)))
        with self._lock:
            self.physical_calls += 1
            self._cache[key] = snapshot
        return tuple(deepcopy(snapshot[0])), deepcopy(snapshot[1])

    def audit(self) -> dict[str, int | str]:
        return {
            "contract": "request-scoped-retrieval-cache-v1",
            "logical_calls": self.logical_calls,
            "physical_calls": self.physical_calls,
            "cache_hits": self.cache_hits,
        }


def safe_adapter_failure(error_code: str, exc: BaseException, **metadata: Any) -> dict[str, Any]:
    """Return bounded failure evidence without exception text or arguments."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", error_code):
        error_code = "LIVE_ADAPTER_EXCEPTION"
    return {"error_code": error_code, "error_type": type(exc).__name__, **metadata}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_live_articles(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LiveAdapterError("ARTICLE_INPUT_UTF8_REQUIRED") from exc
    try:
        if source.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            value = json.loads(text)
            rows = value if isinstance(value, list) else [value]
    except json.JSONDecodeError as exc:
        raise LiveAdapterError("ARTICLE_INPUT_INVALID_JSON") from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise LiveAdapterError("ARTICLE_INPUT_OBJECTS_REQUIRED")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        article_id = str(row.get("article_idx") or "").strip()
        title = str(row.get("title") or "").strip()
        body = str(row.get("article_text") or "").strip()
        published = str(row.get("date") or "").strip()
        if not article_id or article_id in seen:
            raise LiveAdapterError("ARTICLE_ID_MISSING_OR_DUPLICATE")
        if not title or not body or not published:
            raise LiveAdapterError(f"ARTICLE_REQUIRED_FIELD_MISSING:{article_id}")
        try:
            date.fromisoformat(published[:10])
        except ValueError as exc:
            raise LiveAdapterError(f"ARTICLE_DATE_INVALID:{article_id}") from exc
        seen.add(article_id)
        result.append({**row, "article_idx": article_id, "title": title, "article_text": body, "date": published})
    return result


class V6CatalogPassageStore:
    """Read only the RRF candidate tables from the v6 BM25 catalog DB."""

    allowed_fields = ("TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS")

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        if not self.database.is_file():
            raise FileNotFoundError(self.database)
        self.calls = 0
        self.rows_read = 0

    def records_for_tables(self, table_keys: Sequence[str]) -> list[dict[str, Any]]:
        keys = sorted({str(value) for value in table_keys if str(value)})
        if not keys:
            return []
        self.calls += 1
        rows: list[tuple[Any, ...]] = []
        connection = sqlite3.connect(f"file:{self.database.as_posix()}?mode=ro", uri=True)
        try:
            for offset in range(0, len(keys), 800):
                chunk = keys[offset:offset + 800]
                placeholders = ",".join("?" for _ in chunk)
                field_placeholders = ",".join("?" for _ in self.allowed_fields)
                rows.extend(connection.execute(
                    "SELECT record_id,table_key,field,text,text_sha256 FROM records "
                    f"WHERE table_key IN ({placeholders}) AND field IN ({field_placeholders}) "
                    "ORDER BY table_key,field,record_id",
                    (*chunk, *self.allowed_fields),
                ).fetchall())
        finally:
            connection.close()
        self.rows_read += len(rows)
        return [
            {"record_id": row[0], "table_key": row[1], "field": row[2], "text": row[3], "text_sha256": row[4]}
            for row in rows
        ]


class RunProfileProvider:
    """Fresh cache reader with explicit append-only live metadata refresh."""

    def __init__(
        self,
        source_cache: str | Path,
        run_cache: str | Path,
        snapshot_root: str | Path,
        *,
        max_age_seconds: float,
        timeout_seconds: float = 20.0,
        delay_seconds: float = 0.2,
    ) -> None:
        source = Path(source_cache)
        destination = Path(run_cache)
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite run cache: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self.source_sha256 = sha256_file(source)
        self.run_cache_path = destination
        self.cache = PersistentProfileCache(destination)
        self.max_age_seconds = max_age_seconds
        self.api = make_default_api(timeout=timeout_seconds, retries=0)
        self.fetcher = make_snapshot_fetcher(
            self.api,
            snapshot_root,
            delay_seconds=delay_seconds,
        )
        self.lookups = Counter()
        self._metadata_success_calls = 0
        self.profile_sha256: dict[str, str] = {}
        self.failures: dict[str, dict[str, Any]] = {}

    def __call__(self, table_key: str) -> Mapping[str, Any] | None:
        try:
            before = self.cache.lookup(table_key, max_age_seconds=self.max_age_seconds)
            self.lookups[before.status] += 1
            if before.status == "FRESH" and before.profile is not None:
                if before.profile_sha256:
                    self.profile_sha256[table_key] = before.profile_sha256
                return before.profile
            current, calls = self.cache.get_or_fetch(
                table_key,
                max_age_seconds=self.max_age_seconds,
                fetcher=self.fetcher,
            )
            self._metadata_success_calls += int(calls)
            if current.status != "FRESH" or current.profile is None:
                raise LiveAdapterError("PROFILE_REFRESH_NOT_FRESH")
            if current.profile_sha256:
                self.profile_sha256[table_key] = current.profile_sha256
            return current.profile
        except Exception as exc:  # fail one candidate closed; preserve the rest of Top-50
            self.failures[table_key] = safe_adapter_failure("RUN_PROFILE_LOOKUP_FAILED", exc, table_key=table_key)
            return None

    def audit(self) -> dict[str, Any]:
        return {
            "lookup_status": dict(sorted(self.lookups.items())),
            "metadata_api_calls": self.metadata_api_calls,
            "profiles": dict(sorted(self.profile_sha256.items())),
            "failures": dict(sorted(self.failures.items())),
            "source_cache_sha256": self.source_sha256,
            "run_cache_sha256": sha256_file(self.run_cache_path),
        }

    @property
    def metadata_api_calls(self) -> int:
        attempts = int(getattr(getattr(self.api, "session", None), "http_attempts", 0) or 0)
        return max(self._metadata_success_calls, attempts)


class OperationalProfileProvider:
    """Reusable cache isolated from the immutable seed and shared across runs.

    Profile and endpoint versions remain append-only in the cache schema.  The
    current pointer may advance to a newly fetched version; the seed database
    itself is never opened for writing.
    """

    def __init__(
        self,
        source_cache: str | Path,
        operational_cache: str | Path,
        snapshot_root: str | Path,
        *,
        max_age_seconds: float,
        timeout_seconds: float = 20.0,
        delay_seconds: float = 0.2,
        budget_ledger: Any | None = None,
        budget_run_id: str | None = None,
        budget_phase: str | None = None,
    ) -> None:
        source = Path(source_cache)
        destination = Path(operational_cache)
        if source.resolve() == destination.resolve():
            raise LiveAdapterError("OPERATIONAL_CACHE_MUST_NOT_BE_SEED")
        self.source_sha256 = sha256_file(source)
        self.operational_cache_path = destination
        self.snapshot_root = Path(snapshot_root)
        self.max_age_seconds = max_age_seconds
        self.delay_seconds = delay_seconds
        self.budget_ledger = budget_ledger
        self.budget_run_id = str(budget_run_id or "audit")
        self.budget_phase = str(budget_phase or "batch")
        self._lock = threading.RLock()
        self.initialized_from_seed = False
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + f".{uuid.uuid4().hex}.tmp")
            shutil.copy2(source, temporary)
            if sha256_file(temporary) != self.source_sha256:
                raise LiveAdapterError("OPERATIONAL_CACHE_SEED_COPY_SHA_MISMATCH")
            try:
                os.replace(temporary, destination)
            except BaseException:
                if temporary.exists():
                    temporary.unlink()
                raise
            self.initialized_from_seed = True
        self.cache_sha256_before = sha256_file(destination)
        self.cache = PersistentProfileCache(destination)
        # Opening the schema here rejects a wrong or corrupt pre-existing DB.
        connection = self.cache._connect()
        connection.close()
        self.api = make_default_api(timeout=timeout_seconds, retries=0)
        self.lookups = Counter()
        self._metadata_success_calls = 0
        self.profile_sha256: dict[str, str] = {}
        self.version_sha256: dict[str, str] = {}
        self.failures: dict[str, dict[str, Any]] = {}
        self.prefetch_table_keys: list[str] = []

    @staticmethod
    def _safe_name(table_key: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", table_key)

    def _fetch(self, table_key: str):
        version_root = (
            self.snapshot_root
            / self._safe_name(table_key)
            / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "_" + uuid.uuid4().hex)
        )
        def before_endpoint(key: str, endpoint: str):
            if self.budget_ledger is None:
                return None
            phase = self.budget_ledger.current_phase(self.budget_run_id) if hasattr(self.budget_ledger, "current_phase") else self.budget_phase
            return self.budget_ledger.reserve(self.budget_run_id, "metadata", target_id=f"{key}:{endpoint}", detail={"phase": phase, "table_key": key, "endpoint": endpoint})

        def after_endpoint(reservation: Any, error: BaseException | None) -> None:
            if reservation is None or self.budget_ledger is None:
                return
            if error is None:
                self.budget_ledger.complete(reservation)
            elif bool(getattr(error, "pre_send", False)):
                self.budget_ledger.release_before_send(reservation, error)
            else:
                self.budget_ledger.mark_unknown(reservation, detail={"exception": type(error).__name__})

        report = collect_live_profiles(
            [table_key],
            version_root,
            self.api,
            delay_seconds=self.delay_seconds,
            before_endpoint=before_endpoint if self.budget_ledger is not None else None,
            after_endpoint=after_endpoint if self.budget_ledger is not None else None,
        )
        if report.get("profile_success_count") != 1:
            raise LiveAdapterError(f"METADATA_REFRESH_PROFILE_FAILED:{table_key}")
        bundles, _ = _read_snapshot_bundles(version_root)
        if len(bundles) != 1 or str(bundles[0].profile.get("table_key") or "") != table_key:
            raise LiveAdapterError(f"METADATA_REFRESH_IDENTITY_MISMATCH:{table_key}")
        return bundles[0]

    def _record_version(self, table_key: str) -> None:
        connection = self.cache._connect()
        try:
            row = connection.execute(
                "SELECT version_sha256 FROM current_profiles WHERE table_key=?",
                (table_key,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            self.version_sha256[table_key] = str(row[0])

    def __call__(self, table_key: str) -> Mapping[str, Any] | None:
        with self._lock:
            try:
                before = self.cache.lookup(table_key, max_age_seconds=self.max_age_seconds)
                self.lookups[before.status] += 1
                if before.status == "FRESH" and before.profile is not None:
                    if before.profile_sha256:
                        self.profile_sha256[table_key] = before.profile_sha256
                    self._record_version(table_key)
                    return before.profile
                current, calls = self.cache.get_or_fetch(
                    table_key,
                    max_age_seconds=self.max_age_seconds,
                    fetcher=self._fetch,
                )
                self._metadata_success_calls += int(calls)
                if current.status != "FRESH" or current.profile is None:
                    raise LiveAdapterError("PROFILE_REFRESH_NOT_FRESH")
                if current.profile_sha256:
                    self.profile_sha256[table_key] = current.profile_sha256
                self._record_version(table_key)
                return current.profile
            except Exception as exc:
                self.failures[table_key] = safe_adapter_failure("OPERATIONAL_PROFILE_LOOKUP_FAILED", exc, table_key=table_key)
                return None

    def prefetch(self, table_keys: Iterable[str]) -> dict[str, Mapping[str, Any] | None]:
        keys = sorted({str(value) for value in table_keys if str(value)})
        self.prefetch_table_keys = sorted(set(self.prefetch_table_keys).union(keys))
        return {table_key: self(table_key) for table_key in keys}

    def audit(self) -> dict[str, Any]:
        return {
            "contract": "operational-profile-cache-v2",
            "lookup_status": dict(sorted(self.lookups.items())),
            "metadata_api_calls": self.metadata_api_calls,
            "profiles": dict(sorted(self.profile_sha256.items())),
            "versions": dict(sorted(self.version_sha256.items())),
            "failures": dict(sorted(self.failures.items())),
            "prefetch_unique_table_count": len(self.prefetch_table_keys),
            "source_cache_sha256": self.source_sha256,
            "initialized_from_seed": self.initialized_from_seed,
            "operational_cache_path": str(self.operational_cache_path.resolve()),
            "operational_cache_sha256_before": self.cache_sha256_before,
            "operational_cache_sha256_after": sha256_file(self.operational_cache_path),
        }

    @property
    def metadata_api_calls(self) -> int:
        attempts = int(getattr(getattr(self.api, "session", None), "http_attempts", 0) or 0)
        return max(self._metadata_success_calls, attempts)


class CountingAdapter:
    def __init__(self, inner: Callable[..., Any]) -> None:
        self.inner = inner
        self.calls = 0
        self.sum_ms = 0
        self._lock = threading.Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic_ns()
        try:
            return self.inner(*args, **kwargs)
        finally:
            with self._lock:
                self.calls += 1
                self.sum_ms += max(0, int(round((time.monotonic_ns() - started) / 1_000_000)))

    def speculative(self, *args: Any, timeout_seconds: float, **kwargs: Any) -> Any:
        native = getattr(self.inner, "speculative", None)
        if not callable(native) or timeout_seconds <= 0:
            raise LiveAdapterError("SPECULATIVE_NATIVE_TIMEOUT_UNSUPPORTED")
        started = time.monotonic_ns()
        try:
            return native(*args, timeout_seconds=timeout_seconds, **kwargs)
        finally:
            with self._lock:
                self.calls += 1
                self.sum_ms += max(0, int(round((time.monotonic_ns() - started) / 1_000_000)))

    def audit_cursor(self) -> int | None:
        method = getattr(self.inner, "audit_cursor", None)
        value = method() if callable(method) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def audits_since(self, cursor: int) -> list[dict[str, Any]]:
        method = getattr(self.inner, "audits_since", None)
        value = method(cursor) if callable(method) else []
        return [dict(event) for event in value if isinstance(event, Mapping)] if isinstance(value, Sequence) else []


class CountingEncoder:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls = 0
        self.sum_ms = 0
        self._lock = threading.Lock()

    def __call__(self, text: str) -> Sequence[float]:
        started = time.monotonic_ns()
        try:
            return self.client(text)
        finally:
            with self._lock:
                self.calls += 1
                self.sum_ms += max(0, int(round((time.monotonic_ns() - started) / 1_000_000)))


class CountingReranker:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls = 0

    def rerank(self, query: str, passages: Sequence[Mapping[str, str]]) -> Sequence[Mapping[str, Any]]:
        self.calls += 1
        return self.client(query, passages)


class CountingAnswerer:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls = 0
        self.sum_ms = 0
        self._lock = threading.Lock()

    def render(
        self,
        packet: Any,
        brief: Mapping[str, Any] | None,
        repair_code: str | None = None,
    ) -> Mapping[str, Any]:
        started = time.monotonic_ns()
        try:
            return self.client.render(packet, brief, repair_code)
        finally:
            with self._lock:
                self.calls += 1
                self.sum_ms += max(0, int(round((time.monotonic_ns() - started) / 1_000_000)))


class FailClosedCellFetcher:
    def __init__(self, inner: Callable[[dict[str, Any]], Any]) -> None:
        self.inner = inner
        self.calls = 0
        self.sum_ms = 0
        self._lock = threading.Lock()

    def __call__(self, query: dict[str, Any]) -> Any:
        started = time.monotonic_ns()
        try:
            required = ("org_id", "tbl_id", "itm_id", "prd_se", "start_prd_de", "end_prd_de", "obj_levels")
            if any(not query.get(key) for key in required) or not isinstance(query.get("obj_levels"), dict):
                return {"err": "CELL_QUERY_INCOMPLETE"}
            try:
                return self.inner(query)
            except Exception as exc:
                return {"err": "CELL_API_EXCEPTION", **safe_adapter_failure("CELL_API_EXCEPTION", exc)}
        finally:
            with self._lock:
                self.calls += 1
                self.sum_ms += max(0, int(round((time.monotonic_ns() - started) / 1_000_000)))


def write_live_outputs(output_root: str | Path, result: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    root = Path(output_root)
    final_names = (
        "article_summary.json", "final_answers.jsonl", "stage_ledger.jsonl", "run_report.json", "manifest.json",
    )
    if any((root / name).exists() for name in final_names):
        raise FileExistsError(f"refusing to overwrite live output files: {root}")
    root.mkdir(parents=True, exist_ok=True)

    def write_new(name: str, payload: bytes) -> None:
        path = root / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)

    answers = b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8") + b"\n"
        for row in result.get("answers") or []
    )
    ledger = b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8") + b"\n"
        for row in result.get("stage_ledger") or []
    )
    summaries = {
        "contract": "operational-article-summary-v1",
        "articles": list(result.get("article_summaries") or []),
    }
    summary_bytes = json.dumps(
        summaries, ensure_ascii=False, indent=2, sort_keys=True, default=str,
    ).encode("utf-8") + b"\n"
    report = {
        key: value
        for key, value in result.items()
        if key not in {"answers", "stage_ledger", "article_summaries"}
    }
    write_new("article_summary.json", summary_bytes)
    write_new("final_answers.jsonl", answers)
    write_new("stage_ledger.jsonl", ledger)
    write_new("run_report.json", json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n")
    final_manifest = {
        **dict(manifest),
        "outputs": {
            "article_summary.json": hashlib.sha256(summary_bytes).hexdigest(),
            "final_answers.jsonl": hashlib.sha256(answers).hexdigest(),
            "stage_ledger.jsonl": hashlib.sha256(ledger).hexdigest(),
        },
    }
    write_new("manifest.json", json.dumps(final_manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n")


__all__ = [
    "CountingAdapter", "CountingAnswerer", "CountingEncoder", "CountingReranker", "FailClosedCellFetcher", "LiveAdapterError",
    "MonotonicTimingRecorder", "RequestScopedProfileProvider", "RequestScopedEncoder", "RequestScopedRetrievalCache",
    "OperationalProfileProvider", "RunProfileProvider", "V6CatalogPassageStore", "load_live_articles", "sha256_file",
    "write_live_outputs",
]
