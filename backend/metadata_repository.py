"""Read-only PostgreSQL repository for the KOSIS metadata database."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator, Mapping

from backend.errors import BackendError

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


METADATA_DATABASE_URL_ENV = "KOSIS_METADATA_DATABASE_URL"
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
METADATA_TABLE = "statistics_table"
RELEASE_ATTESTATION_SQL = "SELECT 1 FROM statistics_table WHERE snapshot_id = %s LIMIT 1"
METADATA_COLUMNS = (
    "snapshot_id",
    "table_key",
    "org_id",
    "tbl_id",
    "stat_id",
    "title_raw",
    "title_norm",
    "org_name_raw",
    "org_name_norm",
    "status",
    "send_de",
    "source_row_sha256",
    "extra_json",
)


class MetadataRepository:
    """Release-pinned reader for ``kosis_metadata.statistics_table``."""

    def __init__(self, connection_factory: Callable[..., Any] | None = None) -> None:
        self._connection_factory = connection_factory

    def _connect(self) -> Any:
        dsn = os.getenv(METADATA_DATABASE_URL_ENV, "").strip()
        if not dsn:
            raise BackendError("METADATA_CONFIGURATION_PENDING", "KOSIS metadata PostgreSQL 연결 설정이 없습니다.", status_code=503)
        factory = self._connection_factory or (psycopg.connect if psycopg is not None else None)
        if factory is None or dict_row is None:
            raise BackendError("METADATA_DRIVER_UNAVAILABLE", "KOSIS metadata PostgreSQL 드라이버를 사용할 수 없습니다.", status_code=503)
        try:
            return factory(dsn, row_factory=dict_row, options=READ_ONLY_OPTIONS, connect_timeout=5)
        except Exception as exc:
            raise BackendError("METADATA_UNAVAILABLE", "KOSIS metadata PostgreSQL에 연결할 수 없습니다.", status_code=503) from exc

    @contextmanager
    def _session(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            yield connection
        except BackendError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            raise BackendError("METADATA_UNAVAILABLE", "KOSIS metadata 조회에 실패했습니다.", status_code=503) from exc
        finally:
            try:
                connection.close()
            except Exception:
                pass

    @staticmethod
    def _release(release_id: str) -> str:
        release = str(release_id or "").strip()
        if not release:
            raise BackendError("KOSIS_RELEASE_MISMATCH", "KOSIS release가 지정되지 않았습니다.", status_code=503)
        return release

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if not 1 <= int(limit) <= 100 or int(offset) < 0:
            raise BackendError("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", status_code=422)
        return int(limit), int(offset)

    @staticmethod
    def _columns() -> str:
        return ", ".join(METADATA_COLUMNS)

    @staticmethod
    def _row(row: Mapping[str, Any], release_id: str) -> dict[str, Any]:
        snapshot_id = str(row.get("snapshot_id") or "")
        table_key = str(row.get("table_key") or "")
        if snapshot_id != release_id or not table_key:
            raise BackendError("CROSS_STORE_RELEASE_MISMATCH", "KOSIS metadata release 계약이 일치하지 않습니다.", status_code=503)
        return {column: row.get(column) for column in METADATA_COLUMNS}

    @staticmethod
    def _attest_release(connection: Any, release_id: str) -> None:
        """Prove the configured release exists before any public read query."""

        result = connection.execute(RELEASE_ATTESTATION_SQL, (release_id,))
        if result.fetchone() is None:
            raise BackendError(
                "KOSIS_RELEASE_MISMATCH",
                "KOSIS metadata release가 존재하지 않습니다.",
                status_code=503,
            )

    def browse_tables(self, release_id: str, *, limit: int = 20, offset: int = 0, organization: str = "") -> dict[str, Any]:
        release = self._release(release_id)
        page_limit, page_offset = self._page(limit, offset)
        org = str(organization or "").strip()
        columns = self._columns()
        where = "snapshot_id = %s AND (%s = '' OR org_id = %s OR org_name_raw = %s)"
        page_sql = f"SELECT {columns} FROM {METADATA_TABLE} WHERE {where} ORDER BY title_norm ASC, table_key ASC LIMIT %s OFFSET %s"
        count_sql = f"SELECT COUNT(*) AS count FROM {METADATA_TABLE} WHERE {where}"
        facet_sql = f"SELECT org_id, org_name_raw, COUNT(*) AS count FROM {METADATA_TABLE} WHERE snapshot_id = %s GROUP BY org_id, org_name_raw ORDER BY org_name_raw ASC, org_id ASC"
        with self._session() as connection:
            self._attest_release(connection, release)
            rows = connection.execute(page_sql, (release, org, org, org, page_limit, page_offset)).fetchall()
            total_row = connection.execute(count_sql, (release, org, org, org)).fetchone()
            facet_rows = connection.execute(facet_sql, (release,)).fetchall()
        items = [self._row(row, release) for row in rows]
        organizations = [
            {"id": str(row.get("org_id") or ""), "name": str(row.get("org_name_raw") or row.get("org_id") or ""), "count": int(row.get("count") or 0)}
            for row in facet_rows
            if row.get("org_id") or row.get("org_name_raw")
        ]
        return {
            "items": items,
            "total": int((total_row or {}).get("count") or 0),
            "total_relation": "eq",
            "organizations": organizations,
            "organizations_relation": "eq",
            "limit": page_limit,
            "offset": page_offset,
        }

    def hydrate_tables(self, release_id: str, ordered_table_keys: Iterable[str]) -> list[dict[str, Any]]:
        release = self._release(release_id)
        keys = [str(key).strip() for key in ordered_table_keys]
        if len(keys) > 1000 or any(not key for key in keys) or len(keys) != len(set(keys)):
            raise BackendError("CROSS_STORE_RELEASE_MISMATCH", "검색 결과의 table_key 계약이 일치하지 않습니다.", status_code=503)
        placeholders = ", ".join(["%s"] * len(keys))
        with self._session() as connection:
            self._attest_release(connection, release)
            if not keys:
                return []
            sql = f"SELECT {self._columns()} FROM {METADATA_TABLE} WHERE snapshot_id = %s AND table_key IN ({placeholders})"
            rows = connection.execute(sql, (release, *keys)).fetchall()
        by_key: dict[str, dict[str, Any]] = {}
        for row in rows:
            projected = self._row(row, release)
            key = projected["table_key"]
            if key in by_key:
                raise BackendError("CROSS_STORE_RELEASE_MISMATCH", "KOSIS metadata table_key가 중복됩니다.", status_code=503)
            by_key[key] = projected
        if set(by_key) != set(keys):
            raise BackendError("CROSS_STORE_RELEASE_MISMATCH", "검색 결과와 KOSIS metadata의 table_key가 일치하지 않습니다.", status_code=503)
        return [by_key[key] for key in keys]

    def get_table(self, release_id: str, table_key: str) -> dict[str, Any] | None:
        release = self._release(release_id)
        key = str(table_key or "").strip()
        sql = f"SELECT {self._columns()} FROM {METADATA_TABLE} WHERE snapshot_id = %s AND table_key = %s"
        with self._session() as connection:
            self._attest_release(connection, release)
            if not key:
                return None
            row = connection.execute(sql, (release, key)).fetchone()
        return None if row is None else self._row(row, release)


def repository_from_env() -> MetadataRepository:
    return MetadataRepository()
