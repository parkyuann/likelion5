"""Release-bound read-only adapters for the EC2 canonical data plane.

This module is the bridge between the backend adapters and the operational
``SearchChannel`` contract.  It deliberately contains no indexing, upsert,
metadata refresh, cache write, or cell-value lookup path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

from backend.errors import BackendError
from backend.metadata_repository import READ_ONLY_OPTIONS
from backend.query_encoder import BGEQueryEncoderClient
from backend.search_adapter import (
    ALLOWED_FIELDS,
    OpenSearchBM25Adapter,
    QdrantDenseAdapter,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - deployment dependency preflight owns this path
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


CONTRACT_VERSION = "release-bound-live-adapters-v1"
RELEASE_RUNTIME_ENV = "KOSIS_RELEASE_DATA_RUNTIME_ENABLED"
RERANKER_ENABLED_ENV = "BGE_RERANKER_ENABLED"
RRF_ORDER_RERANKER_DISABLED = "RRF_ORDER_RERANKER_DISABLED"
RRF_ONLY_ORDERING = "RRF_ONLY"
VECTOR_SIZE = 1024
PROFILE_TABLES = (
    "statistics_table",
    "table_item",
    "dimension_axis",
    "dimension_value",
    "period_coverage",
)


def release_runtime_enabled() -> bool:
    return os.getenv(RELEASE_RUNTIME_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}


def _fail(code: str, message: str, status_code: int = 503) -> BackendError:
    return BackendError(code, message, status_code=status_code)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _strict_send_de(value: Any) -> str:
    raw = _text(value)
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise _fail(
            "METADATA_PROFILE_CONTRACT_MISMATCH",
            "canonical send_de가 ISO 날짜가 아닙니다.",
        ) from None
    if parsed.isoformat() != raw:
        raise _fail(
            "METADATA_PROFILE_CONTRACT_MISMATCH",
            "canonical send_de 형식이 YYYY-MM-DD가 아닙니다.",
        )
    return parsed.isoformat()


def _assert_snapshot(row: Mapping[str, Any], release_id: str) -> None:
    if _text(row.get("snapshot_id")) != release_id:
        raise _fail("KOSIS_RELEASE_MISMATCH", "PostgreSQL metadata release가 일치하지 않습니다.")


class CanonicalMetadataProfileProvider:
    """Build the existing profile shape from canonical PostgreSQL tables."""

    def __init__(self, *, dsn: str | None = None, release_id: str | None = None) -> None:
        self.dsn = (dsn if dsn is not None else os.getenv("KOSIS_METADATA_DATABASE_URL", "")).strip()
        self.release_id = (release_id if release_id is not None else os.getenv("KOSIS_RELEASE_ID", "")).strip()
        self.lookups = 0
        self.release_attested = False
        self.profile_sha256: dict[str, str] = {}

    def _connect(self) -> Any:
        if not self.dsn or not self.release_id:
            raise _fail("METADATA_CONFIGURATION_PENDING", "KOSIS metadata PostgreSQL 연결 설정이 없습니다.")
        if psycopg is None or dict_row is None:
            raise _fail("METADATA_DRIVER_UNAVAILABLE", "KOSIS metadata PostgreSQL 드라이버를 사용할 수 없습니다.")
        try:
            return psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                options=READ_ONLY_OPTIONS,
                connect_timeout=5,
            )
        except Exception as exc:
            raise _fail("METADATA_UNAVAILABLE", "KOSIS metadata PostgreSQL에 연결할 수 없습니다.") from exc

    def _release_attestation(self, connection: Any) -> None:
        row = connection.execute(
            "SELECT 1 FROM statistics_table WHERE snapshot_id = %s LIMIT 1",
            (self.release_id,),
        ).fetchone()
        if row is None:
            raise _fail("KOSIS_RELEASE_MISMATCH", "KOSIS metadata release가 존재하지 않습니다.")
        self.release_attested = True

    @staticmethod
    def _table_key(value: Any) -> str:
        table_key = _text(value)
        if not table_key or table_key.count(":") != 1:
            raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical table_key 형식이 올바르지 않습니다.")
        return table_key

    def preflight(self) -> None:
        connection = self._connect()
        try:
            self._release_attestation(connection)
        except BackendError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def _profile(self, table_key: str, connection: Any) -> dict[str, Any] | None:
        stats = connection.execute(
            """
            SELECT snapshot_id, table_key, org_id, tbl_id, stat_id,
                   title_raw, title_norm, org_name_raw, org_name_norm,
                   status, send_de, source_row_sha256, extra_json
            FROM statistics_table
            WHERE snapshot_id = %s AND table_key = %s
            """,
            (self.release_id, table_key),
        ).fetchall()
        if not stats:
            return None
        if len(stats) != 1:
            raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical statistics_table identity가 중복됩니다.")
        table = stats[0]
        _assert_snapshot(table, self.release_id)
        if self._table_key(table.get("table_key")) != table_key:
            raise _fail("CROSS_STORE_RELEASE_MISMATCH", "canonical table_key가 요청과 다릅니다.")

        items = connection.execute(
            """
            SELECT snapshot_id, table_key, itm_id, itm_name_raw, itm_name_eng,
                   unit_id, unit_nm_raw, unit_nm_eng, item_order
            FROM table_item
            WHERE snapshot_id = %s AND table_key = %s
            ORDER BY item_order NULLS LAST, itm_id ASC
            """,
            (self.release_id, table_key),
        ).fetchall()
        axes = connection.execute(
            """
            SELECT snapshot_id, table_key, obj_id, obj_name_raw,
                   obj_name_eng, source_obj_id_sn, param_obj_level, value_count
            FROM dimension_axis
            WHERE snapshot_id = %s AND table_key = %s
            ORDER BY param_obj_level ASC, obj_id ASC
            """,
            (self.release_id, table_key),
        ).fetchall()
        values = connection.execute(
            """
            SELECT snapshot_id, table_key, obj_id, value_id,
                   value_name_raw, value_name_eng, parent_value_id, value_order,
                   unit_id, unit_nm_raw, unit_nm_eng
            FROM dimension_value
            WHERE snapshot_id = %s AND table_key = %s
            ORDER BY obj_id ASC, value_order NULLS LAST, value_id ASC
            """,
            (self.release_id, table_key),
        ).fetchall()
        periods = connection.execute(
            """
            SELECT snapshot_id, table_key, prd_se, start_period, end_period
            FROM period_coverage
            WHERE snapshot_id = %s AND table_key = %s
            ORDER BY prd_se ASC, start_period ASC, end_period ASC
            """,
            (self.release_id, table_key),
        ).fetchall()

        for row in (*items, *axes, *values, *periods):
            _assert_snapshot(row, self.release_id)
            if self._table_key(row.get("table_key")) != table_key:
                raise _fail("CROSS_STORE_RELEASE_MISMATCH", "canonical metadata table_key가 일치하지 않습니다.")
        if not items or not axes or not values or not periods:
            raise _fail("METADATA_PROFILE_INCOMPLETE", "canonical metadata profile inventory가 불완전합니다.")

        item_profile: list[dict[str, Any]] = []
        item_ids: set[str] = set()
        for row in items:
            item_id = _text(row.get("itm_id"))
            if not item_id or item_id in item_ids or not _text(row.get("itm_name_raw")):
                raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical ITEM identity가 올바르지 않습니다.")
            item_ids.add(item_id)
            item_profile.append({
                "itm_id": item_id,
                "itm_nm": _text(row.get("itm_name_raw")),
                "itm_nm_eng": _text(row.get("itm_name_eng")),
                "unit_id": _text(row.get("unit_id")),
                "unit_nm": _text(row.get("unit_nm_raw")),
                "unit_nm_eng": _text(row.get("unit_nm_eng")),
            })

        values_by_axis: dict[str, list[dict[str, Any]]] = {}
        value_ids: set[tuple[str, str]] = set()
        for row in values:
            axis_id = _text(row.get("obj_id"))
            value_id = _text(row.get("value_id"))
            identity = (axis_id, value_id)
            if not axis_id or not value_id or identity in value_ids or not _text(row.get("value_name_raw")):
                raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical dimension value identity가 올바르지 않습니다.")
            value_ids.add(identity)
            values_by_axis.setdefault(axis_id, []).append({
                "value_id": value_id,
                "value_name": _text(row.get("value_name_raw")),
                "value_name_eng": _text(row.get("value_name_eng")),
                "parent_value_id": _text(row.get("parent_value_id")),
                "unit_id": _text(row.get("unit_id")),
                "unit_nm": _text(row.get("unit_nm_raw")),
                "unit_nm_eng": _text(row.get("unit_nm_eng")),
            })

        dimensions: list[dict[str, Any]] = []
        source_axis_orders: set[int] = set()
        param_axis_orders: set[int] = set()
        for row in axes:
            axis_id = _text(row.get("obj_id"))
            axis_name = _text(row.get("obj_name_raw"))
            source_axis_order = row.get("source_obj_id_sn")
            param_axis_order = row.get("param_obj_level")
            try:
                source_axis_order_int = int(source_axis_order)
                param_axis_order_int = int(param_axis_order)
            except (TypeError, ValueError) as exc:
                raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical dimension order가 없습니다.") from exc
            if (
                not axis_id
                or not axis_name
                or source_axis_order_int < 1
                or source_axis_order_int in source_axis_orders
                or not 1 <= param_axis_order_int <= 8
                or param_axis_order_int in param_axis_orders
            ):
                raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical dimension axis identity가 올바르지 않습니다.")
            source_axis_orders.add(source_axis_order_int)
            param_axis_orders.add(param_axis_order_int)
            axis_values = values_by_axis.get(axis_id, [])
            expected_count = row.get("value_count")
            if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count != len(axis_values):
                raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical dimension value_count가 일치하지 않습니다.")
            dimensions.append({
                "obj_id": axis_id,
                "obj_nm": axis_name,
                "obj_nm_eng": _text(row.get("obj_name_eng")),
                "obj_id_sn": source_axis_order_int,
                "obj_order": param_axis_order_int,
                "values": axis_values,
            })
        dimensions.sort(key=lambda row: (int(row["obj_order"]), row["obj_id"]))
        if [row["obj_order"] for row in dimensions] != list(range(1, len(dimensions) + 1)):
            raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical param_obj_level이 연속적이지 않습니다.")

        period_profile: list[dict[str, str]] = []
        period_ids: set[tuple[str, str, str]] = set()
        for row in periods:
            period = (_text(row.get("prd_se")), _text(row.get("start_period")), _text(row.get("end_period")))
            if not all(period) or period in period_ids:
                raise _fail("METADATA_PROFILE_CONTRACT_MISMATCH", "canonical period identity가 올바르지 않습니다.")
            period_ids.add(period)
            period_profile.append({"PRD_SE": period[0], "STRT_PRD_DE": period[1], "END_PRD_DE": period[2]})

        org_id = _text(table.get("org_id"))
        tbl_id = _text(table.get("tbl_id"))
        if table_key != f"{org_id}:{tbl_id}":
            raise _fail("CROSS_STORE_RELEASE_MISMATCH", "statistics_table table_key 구성요소가 일치하지 않습니다.")
        profile: dict[str, Any] = {
            "table_key": table_key,
            "release_id": self.release_id,
            "snapshot_id": self.release_id,
            "org_id": org_id,
            "tbl_id": tbl_id,
            "stat_id": _text(table.get("stat_id")),
            "send_de": _strict_send_de(table.get("send_de")),
            "tbl_name": _text(table.get("title_raw")),
            "org_name": _text(table.get("org_name_raw")),
            "items": item_profile,
            "dimensions": dimensions,
            "periods": period_profile,
            "units": sorted({
                unit
                for unit in (
                    [item["unit_nm"] for item in item_profile]
                    + [value["unit_nm"] for dimension in dimensions for value in dimension["values"]]
                )
                if unit
            }),
            "source": "KOSIS_METADATA_POSTGRESQL",
            "source_tables": list(PROFILE_TABLES),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "metadata_readiness": {
                "cell_selector_ready": True,
                "item_count": len(item_profile),
                "dimension_count": len(dimensions),
                "dimension_value_count": sum(len(row["values"]) for row in dimensions),
                "period_count": len(period_profile),
                "items_with_unit": sum(bool(item["unit_nm"]) for item in item_profile),
                "dimension_values_with_unit": sum(
                    bool(value["unit_nm"])
                    for dimension in dimensions
                    for value in dimension["values"]
                ),
            },
        }
        profile["profile_sha256"] = _canonical_sha({key: value for key, value in profile.items() if key not in {"retrieved_at", "profile_sha256"}})
        return profile

    def __call__(self, table_key: str) -> Mapping[str, Any] | None:
        key = self._table_key(table_key)
        self.lookups += 1
        connection = self._connect()
        try:
            self._release_attestation(connection)
            profile = self._profile(key, connection)
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
            raise _fail("METADATA_PROFILE_READ_FAILED", "canonical metadata profile 조회에 실패했습니다.") from exc
        finally:
            connection.close()
        if profile is not None:
            self.profile_sha256[key] = str(profile["profile_sha256"])
        return profile

    def prefetch(self, table_keys: Iterable[str]) -> dict[str, Mapping[str, Any] | None]:
        return {key: self(key) for key in sorted({str(value) for value in table_keys if str(value)})}

    @property
    def metadata_api_calls(self) -> int:
        return 0

    def audit(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION,
            "source": "KOSIS_METADATA_POSTGRESQL",
            "release_id": self.release_id,
            "tables": list(PROFILE_TABLES),
            "read_only": True,
            "lookups": self.lookups,
            "profiles": dict(sorted(self.profile_sha256.items())),
        }


class ReleaseBoundBM25Channel:
    def __init__(self, adapter: OpenSearchBM25Adapter) -> None:
        self.adapter = adapter

    def __call__(self, query: Any, fields: Sequence[str], top_k: int) -> Iterable[Mapping[str, Any]]:
        result = self.adapter.search(query.text, limit=min(max(1, int(top_k)), 100), fields=fields)
        for candidate in result.get("candidates") or []:
            evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), Mapping) else {}
            yield {
                "record_id": _text(evidence.get("record_id")),
                "table_key": _text(candidate.get("table_key")),
                "field": _text(evidence.get("field")),
                "score": candidate.get("score"),
                "release_id": _text(candidate.get("release_id")),
                "snapshot_id": _text(candidate.get("release_id")),
                "source": "opensearch_bm25",
                "evidence": dict(evidence),
            }


class ReleaseBoundDenseChannel:
    def __init__(self, adapter: QdrantDenseAdapter, encoder: Callable[[str], Sequence[float] | tuple[Sequence[float], Mapping[str, Any]]]) -> None:
        self.adapter = adapter
        self.encoder = encoder
        # A runtime is instantiated for one operational request.  Preserve every
        # dense audit for that request rather than retaining a lossy last value.
        self._audit_lock = Lock()
        self._audit_events: list[dict[str, Any]] = []

    @staticmethod
    def _encoded(value: Any) -> tuple[Sequence[float], Mapping[str, Any]]:
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], Mapping):
            return value[0], value[1]
        return value, {}

    def __call__(self, query: Any, fields: Sequence[str], top_k: int) -> Iterable[Mapping[str, Any]]:
        query_text = str(query.text)
        query_sha256 = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
        vector, encoder_evidence = self._encoded(self.encoder(query_text))
        result = self.adapter.search_grouped_by_table(vector, fields=fields, limit=100)
        raw_audit = result.get("audit") if isinstance(result, Mapping) else None
        audit = {
            key: raw_audit[key]
            for key in (
                "boundary_status", "cutoff_score", "observed_tied_count",
                "requested_window", "expansions",
            )
            if isinstance(raw_audit, Mapping) and key in raw_audit
        }
        with self._audit_lock:
            self._audit_events.append({"query_sha256": query_sha256, **audit})
        candidates: list[dict[str, Any]] = []
        for candidate in result.get("candidates") or []:
            evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), Mapping) else {}
            candidates.append({
                "record_id": _text(evidence.get("record_id")),
                "table_key": _text(candidate.get("table_key")),
                "field": _text(evidence.get("field")),
                "score": candidate.get("score"),
                "release_id": _text(candidate.get("release_id")),
                "snapshot_id": _text(candidate.get("release_id")),
                "source": "qdrant_dense",
                "evidence": {**dict(evidence), "encoder": dict(encoder_evidence)},
            })
        return candidates

    def audit_cursor(self) -> int:
        with self._audit_lock:
            return len(self._audit_events)

    def audits_since(self, cursor: int) -> list[dict[str, Any]]:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise _fail("DENSE_AUDIT_CURSOR_INVALID", "dense audit cursor가 올바르지 않습니다.")
        with self._audit_lock:
            if cursor > len(self._audit_events):
                raise _fail("DENSE_AUDIT_CURSOR_INVALID", "dense audit cursor가 현재 범위를 벗어났습니다.")
            return [dict(event) for event in self._audit_events[cursor:]]


@dataclass
class ReleaseBoundRuntime:
    metadata: CanonicalMetadataProfileProvider
    bm25_adapter: OpenSearchBM25Adapter
    dense_adapter: QdrantDenseAdapter
    encoder: BGEQueryEncoderClient

    @property
    def release_id(self) -> str:
        return self.metadata.release_id

    @property
    def binding_sha256(self) -> str:
        payload = {
            "release_id": self.release_id,
            "analyzer": self.bm25_adapter.config.analyzer,
            "index": self.bm25_adapter.config.index,
            "collection": self.dense_adapter.config.collection,
            "qdrant_receipt_sha256": self.dense_adapter.config.receipt_sha256,
            "encoder_model_revision": self.encoder.config.model_revision,
            "encoder_vector_size": self.encoder.config.vector_size,
        }
        return _canonical_sha(payload)

    def preflight(self) -> dict[str, Any]:
        if os.getenv(RERANKER_ENABLED_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}:
            raise _fail("RERANKER_RUNTIME_PENDING", "release-bound runtime은 현재 reranker 비활성만 지원합니다.")
        self.metadata.preflight()
        self.bm25_adapter.preflight()
        self.dense_adapter.preflight()
        self.encoder.preflight()
        return {
            "status": "READY",
            "contract_version": CONTRACT_VERSION,
            "release_id": self.release_id,
            "release_binding_sha256": self.binding_sha256,
            "service_urls": {
                "query_encoder": self.encoder.config.url,
                "qdrant": self.dense_adapter.config.url,
                "reranker": "",
            },
            "gpu_receipts": {
                "query_encoder": {
                    "model_id": self.encoder.config.model_id,
                    "model_revision": self.encoder.config.model_revision,
                    "model_receipt_sha256": self.encoder.config.model_receipt_sha256,
                    "vector_dimension": self.encoder.config.vector_size,
                    "normalized": True,
                },
                "reranker": {"enabled": False, "ranking_mode": RRF_ORDER_RERANKER_DISABLED},
            },
            "opensearch": {
                "index": self.bm25_adapter.config.index,
                "analyzer": self.bm25_adapter.config.analyzer,
            },
            "qdrant": {
                "collection": self.dense_adapter.config.collection,
                "vector_size": self.dense_adapter.config.vector_size,
                "receipt_sha256": self.dense_adapter.config.receipt_sha256,
            },
            "profile": self.metadata.audit(),
            "ranking_mode": RRF_ONLY_ORDERING,
            "read_only": True,
        }

    def channels(self, encoder: Callable[[str], Any]) -> dict[str, Any]:
        return {
            "bm25": ReleaseBoundBM25Channel(self.bm25_adapter),
            "dense": ReleaseBoundDenseChannel(self.dense_adapter, encoder),
        }


def build_release_bound_runtime() -> ReleaseBoundRuntime:
    metadata = CanonicalMetadataProfileProvider()
    return ReleaseBoundRuntime(
        metadata=metadata,
        bm25_adapter=OpenSearchBM25Adapter(),
        dense_adapter=QdrantDenseAdapter(),
        encoder=BGEQueryEncoderClient(),
    )


__all__ = [
    "CONTRACT_VERSION",
    "RRF_ONLY_ORDERING",
    "RRF_ORDER_RERANKER_DISABLED",
    "CanonicalMetadataProfileProvider",
    "ReleaseBoundBM25Channel",
    "ReleaseBoundDenseChannel",
    "ReleaseBoundRuntime",
    "build_release_bound_runtime",
    "release_runtime_enabled",
]
