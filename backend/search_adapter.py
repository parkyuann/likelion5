"""Release-pinned, read-only KOSIS search adapters.

BM25 is exposed through an OpenSearch text channel. Dense search is an internal
Qdrant vector interface only; neither adapter performs indexing, mutation,
verdicting, or cell-value lookup.
"""

from __future__ import annotations

import hashlib
import math
import os
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import quote

import httpx

from backend.errors import BackendError

try:  # qdrant-client is a runtime dependency; tests can inject a fake client.
    from qdrant_client import QdrantClient
    from qdrant_client import models as qdrant_models
except ImportError:  # pragma: no cover - dependency preflight covers this path
    QdrantClient = None  # type: ignore[assignment,misc]
    qdrant_models = None  # type: ignore[assignment]


ALLOWED_FIELDS = frozenset({"TITLE", "CATEGORY", "ITEM", "DIMENSION_AXIS"})
REQUIRED_OPENSEARCH_FIELDS = frozenset(
    {"record_id", "snapshot_id", "table_key", "field", "text", "text_sha256", "source_id"}
)
REQUIRED_QDRANT_FIELDS = REQUIRED_OPENSEARCH_FIELDS - {"text"}
CANDIDATE_AUTHORITY = "CANDIDATE_GENERATION_ONLY"
SEARCH_CHANNEL_TOP_K = 100
SEARCH_WINDOW_MAX = 1000
DENSE_BOUNDARY_CLOSED = "CLOSED"
DENSE_BOUNDARY_DROPPED_UNCLOSED_CUTOFF_TIE = "DROPPED_UNCLOSED_CUTOFF_TIE"


class SearchAdapter(Protocol):
    def search(self, query: str, *, limit: int, offset: int = 0) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OpenSearchConfig:
    url: str
    release_id: str
    analyzer: str
    index: str

    def __post_init__(self) -> None:
        if self.analyzer != "standard-v1":
            raise BackendError("OPENSEARCH_ANALYZER_UNSUPPORTED", "현재 runtime은 standard-v1 analyzer만 사용합니다.", status_code=503)
        if not self.index or any(value in self.index for value in (",", "*", "?", " ")):
            raise BackendError("OPENSEARCH_INDEX_NOT_CONCRETE", "OpenSearch index는 concrete 이름이어야 합니다.", status_code=503)
        if "standard-v1" not in self.index:
            raise BackendError("OPENSEARCH_INDEX_CONFIG_MISMATCH", "OpenSearch index와 analyzer label이 일치하지 않습니다.", status_code=503)

    @classmethod
    def from_env(cls) -> "OpenSearchConfig":
        url = os.getenv("OPENSEARCH_URL", "").strip().rstrip("/")
        release_id = os.getenv("KOSIS_RELEASE_ID", "").strip()
        analyzer = os.getenv("KOSIS_BM25_ANALYZER", "standard-v1").strip()
        index = os.getenv("KOSIS_BM25_INDEX", "").strip()
        if not url or not release_id or not index:
            raise BackendError("OPENSEARCH_CONFIGURATION_PENDING", "OpenSearch release 연결 설정이 없습니다.", status_code=503)
        return cls(url=url, release_id=release_id, analyzer=analyzer, index=index)


def normalize_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(query or "")).split())


def _fail(code: str, message: str, status_code: int = 503) -> BackendError:
    return BackendError(code, message, status_code=status_code)


def _mapping_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _qdrant_filter(release_id: str, fields: Iterable[str] | None = None) -> Any:
    """Build the SDK filter, retaining a serializable form for injected test clients."""

    selected_fields = sorted(set(fields or ()))
    if qdrant_models is not None:
        must = [qdrant_models.FieldCondition(
            key="snapshot_id",
            match=qdrant_models.MatchValue(value=release_id),
        )]
        if selected_fields:
            must.append(qdrant_models.FieldCondition(
                key="field",
                match=qdrant_models.MatchAny(any=selected_fields),
            ))
        return qdrant_models.Filter(must=must)
    must = [{"key": "snapshot_id", "match": {"value": release_id}}]
    if selected_fields:
        must.append({"key": "field", "match": {"any": selected_fields}})
    return {"must": must}


class OpenSearchBM25Adapter:
    """OpenSearch adapter limited to one release-pinned BM25 candidate channel."""

    def __init__(self, config: OpenSearchConfig | None = None, *, client: Any | None = None, timeout: float = 5.0) -> None:
        self.config = config or OpenSearchConfig.from_env()
        self._client = client or httpx.Client(base_url=self.config.url, timeout=timeout)
        self._preflighted = False

    @property
    def client(self) -> Any:
        return self._client

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            elif int(getattr(response, "status_code", 200)) >= 400:
                raise RuntimeError("HTTP response failed")
            return response
        except Exception as exc:
            raise _fail("OPENSEARCH_UNAVAILABLE", "OpenSearch에 연결할 수 없습니다.") from exc

    def preflight(self) -> None:
        if self._preflighted:
            return
        encoded_index = quote(self.config.index, safe='-_:.')
        response = self._request("GET", f"/{encoded_index}")
        try:
            payload = response.json()
        except Exception as exc:
            raise _fail("OPENSEARCH_MAPPING_INVALID", "OpenSearch mapping 응답이 올바르지 않습니다.") from exc
        if not isinstance(payload, Mapping) or set(payload) != {self.config.index}:
            raise _fail("OPENSEARCH_INDEX_NOT_CONCRETE", "OpenSearch concrete index가 일치하지 않습니다.")
        index_payload = payload.get(self.config.index)
        mappings = _mapping_value(index_payload, "mappings", {})
        source_mapping = _mapping_value(mappings, "_source", {})
        if not isinstance(mappings, Mapping) or (isinstance(source_mapping, Mapping) and source_mapping.get("enabled") is False):
            raise _fail("OPENSEARCH_MAPPING_INVALID", "OpenSearch _source 계약이 없습니다.")
        properties = _mapping_value(mappings, "properties", {})
        if not isinstance(properties, Mapping):
            raise _fail("OPENSEARCH_MAPPING_INVALID", "OpenSearch properties 계약이 없습니다.")
        for name in REQUIRED_OPENSEARCH_FIELDS - {"text"}:
            field_mapping = properties.get(name)
            if not isinstance(field_mapping, Mapping) or field_mapping.get("type") != "keyword":
                raise _fail("OPENSEARCH_MAPPING_INVALID", "OpenSearch 필드 타입 계약이 일치하지 않습니다.")
        text_mapping = properties.get("text")
        if not isinstance(text_mapping, Mapping) or text_mapping.get("type") != "text":
            raise _fail("OPENSEARCH_MAPPING_INVALID", "OpenSearch text 필드 타입 계약이 일치하지 않습니다.")
        count_response = self._request(
            "POST",
            f"/{encoded_index}/_count",
            json={"query": {"term": {"snapshot_id": self.config.release_id}}},
        )
        try:
            count_payload = count_response.json()
            count = count_payload["count"]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise _fail("OPENSEARCH_MAPPING_INVALID", "OpenSearch release count 응답이 올바르지 않습니다.") from exc
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise _fail("KOSIS_RELEASE_MISMATCH", "OpenSearch configured release가 존재하지 않습니다.")
        self._preflighted = True

    def _body(self, query: str, size: int, *, fields: Iterable[str] | None = None) -> dict[str, Any]:
        selected_fields = sorted(set(fields or ALLOWED_FIELDS))
        if not selected_fields or not set(selected_fields).issubset(ALLOWED_FIELDS):
            raise _fail("OPENSEARCH_FIELD_INVALID", "OpenSearch 검색 field가 허용되지 않습니다.", 422)
        return {
            "size": size,
            "track_total_hits": True,
            "_source": sorted(REQUIRED_OPENSEARCH_FIELDS),
            "query": {
                "bool": {
                    "must": [{"match": {"text": {"query": query}}}],
                    "filter": [
                        {"term": {"snapshot_id": self.config.release_id}},
                        {"terms": {"field": selected_fields}},
                    ],
                }
            },
            "collapse": {"field": "table_key"},
            "sort": [{"_score": {"order": "desc"}}, {"table_key": {"order": "asc"}}],
        }

    def _candidate(self, hit: Mapping[str, Any]) -> dict[str, Any]:
        source = hit.get("_source")
        if not isinstance(source, Mapping) or not REQUIRED_OPENSEARCH_FIELDS.issubset(source):
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch candidate source가 없습니다.")
        values = {key: source.get(key) for key in REQUIRED_OPENSEARCH_FIELDS}
        if any(not isinstance(values[key], str) or not values[key].strip() for key in REQUIRED_OPENSEARCH_FIELDS):
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch candidate 필드가 비어 있습니다.")
        if str(values["snapshot_id"]) != self.config.release_id:
            raise _fail("KOSIS_RELEASE_MISMATCH", "OpenSearch candidate release가 일치하지 않습니다.")
        if str(values["field"]) not in ALLOWED_FIELDS:
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch candidate field가 허용되지 않습니다.")
        if hashlib.sha256(str(values["text"]).encode("utf-8")).hexdigest() != str(values["text_sha256"]).casefold():
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch text hash가 일치하지 않습니다.")
        score = hit.get("_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch score가 올바르지 않습니다.")
        document_id = hit.get("_id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch document id가 없습니다.")
        return {
            "table_key": str(values["table_key"]),
            "release_id": self.config.release_id,
            "source": "opensearch_bm25",
            "score": float(score),
            "evidence": {
                "channel": "bm25", "analyzer": self.config.analyzer, "index": self.config.index,
                "document_id": document_id, "record_id": str(values["record_id"]),
                "field": str(values["field"]), "source_id": str(values["source_id"]),
                "text": str(values["text"]), "text_sha256": str(values["text_sha256"]),
            },
        }

    def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        fields: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Return only release-pinned, deduplicated BM25 candidate envelopes."""
        try:
            page_limit, page_offset = int(limit), int(offset)
        except (TypeError, ValueError) as exc:
            raise _fail("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", 422) from exc
        if not 1 <= page_limit <= 100 or page_offset < 0 or page_offset + page_limit > SEARCH_WINDOW_MAX:
            raise _fail("SEARCH_WINDOW_EXCEEDED", "검색 결과 범위는 1,000개 이내여야 합니다.", 422)
        normalized = normalize_query(query)
        if not normalized:
            raise _fail("EMPTY_SEARCH_QUERY", "검색어가 비어 있습니다.", 422)
        self.preflight()
        response = self._request(
            "POST",
            f"/{quote(self.config.index, safe='-_:.')}/_search",
            json=self._body(normalized, SEARCH_CHANNEL_TOP_K, fields=fields),
        )
        try:
            payload = response.json()
            hits_payload = payload["hits"]
            raw_hits = hits_payload["hits"]
        except Exception as exc:
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch 검색 응답이 올바르지 않습니다.") from exc
        if not isinstance(raw_hits, list) or len(raw_hits) > SEARCH_CHANNEL_TOP_K:
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch candidate window가 올바르지 않습니다.")
        if any(not isinstance(hit, Mapping) for hit in raw_hits):
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch candidate 형식이 올바르지 않습니다.")
        candidates = [self._candidate(hit) for hit in raw_hits]
        if len({item["table_key"] for item in candidates}) != len(candidates):
            raise _fail("SEARCH_SOURCE_CONTRACT_MISMATCH", "OpenSearch collapse 계약이 지켜지지 않았습니다.")
        candidates.sort(key=lambda item: (-float(item["score"]), item["table_key"]))
        relation = _mapping_value(hits_payload.get("total"), "relation", "eq")
        relation = "gte" if relation == "gte" or len(candidates) >= SEARCH_CHANNEL_TOP_K else "eq"
        return {"candidates": candidates[page_offset : page_offset + page_limit], "window": candidates, "total": len(candidates), "total_relation": relation, "limit": page_limit, "offset": page_offset}


@dataclass(frozen=True)
class QdrantConfig:
    url: str
    release_id: str
    collection: str
    vector_size: int
    receipt_sha256: str
    vector_name: str = ""

    def __post_init__(self) -> None:
        if self.vector_name != "":
            raise BackendError(
                "QDRANT_VECTOR_CONFIGURATION_PENDING",
                "현재 EC2 Qdrant는 unnamed vector만 지원합니다.",
                status_code=503,
            )

    @classmethod
    def from_env(cls) -> "QdrantConfig":
        url = os.getenv("QDRANT_URL", "").strip()
        release_id = os.getenv("KOSIS_RELEASE_ID", "").strip()
        collection = os.getenv("QDRANT_COLLECTION", "").strip()
        raw_size = os.getenv("QDRANT_VECTOR_SIZE", "1024").strip()
        receipt = os.getenv("QDRANT_RECEIPT_SHA256", "").strip()
        vector_name = os.getenv("QDRANT_VECTOR_NAME", "")
        try:
            size = int(raw_size)
        except ValueError:
            size = -1
        if not url or not release_id or not collection or vector_name != "" or size != 1024 or len(receipt) != 64 or any(char not in "0123456789abcdefABCDEF" for char in receipt):
            raise BackendError("QDRANT_CONFIGURATION_PENDING", "Qdrant dense read 연결 설정이 올바르지 않습니다.", status_code=503)
        return cls(url=url, release_id=release_id, collection=collection, vector_size=size, receipt_sha256=receipt.lower(), vector_name="")


def _status_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw).split(".")[-1].casefold()


def _vector_params(info: Any) -> Any:
    config = _mapping_value(info, "config")
    params = _mapping_value(config, "params")
    return _mapping_value(params, "vectors")


def _unnamed_vector_params(info: Any) -> Any:
    """Return the single unnamed vector configuration; reject named vectors."""

    vectors = _vector_params(info)
    if isinstance(vectors, Mapping):
        if "dense" in vectors:
            return None
        if "size" in vectors and "distance" in vectors:
            return vectors
    if vectors is not None and _mapping_value(vectors, "size") is not None:
        return vectors
    return None


class QdrantDenseAdapter:
    """Internal precomputed-vector reader; it has no public text endpoint."""

    def __init__(self, config: QdrantConfig | None = None, *, client: Any | None = None, timeout: float = 5.0) -> None:
        self.config = config or QdrantConfig.from_env()
        self._client = client
        if self._client is None:
            if QdrantClient is None:
                raise _fail("QDRANT_DRIVER_UNAVAILABLE", "Qdrant 드라이버를 사용할 수 없습니다.")
            self._client = QdrantClient(url=self.config.url, timeout=timeout)
        self._preflighted = False

    def preflight(self) -> None:
        if self._preflighted:
            return
        try:
            inventory = self._client.get_collections()
            collections = _mapping_value(inventory, "collections")
            if not isinstance(collections, Sequence) or isinstance(collections, (str, bytes)):
                raise _fail("QDRANT_COLLECTION_CONTRACT_MISMATCH", "Qdrant collection inventory가 올바르지 않습니다.")
            collection_names = {
                str(_mapping_value(item, "name"))
                for item in collections
                if _mapping_value(item, "name") is not None
            }
            if self.config.collection not in collection_names:
                raise _fail("QDRANT_COLLECTION_NOT_CONCRETE", "Qdrant 설정값이 실제 collection 이름이 아닙니다.")
            info = self._client.get_collection(collection_name=self.config.collection)
            count_filter = _qdrant_filter(self.config.release_id)
            count_method = getattr(self._client, "count", None)
            if count_method is None:
                raise _fail("QDRANT_COUNT_UNAVAILABLE", "Qdrant release count API를 사용할 수 없습니다.")
            try:
                count_result = count_method(
                    collection_name=self.config.collection,
                    count_filter=count_filter,
                    exact=True,
                )
            except TypeError:
                count_result = count_method(
                    collection_name=self.config.collection,
                    count_filter=count_filter,
                )
        except Exception as exc:
            if isinstance(exc, BackendError):
                raise
            raise _fail("QDRANT_UNAVAILABLE", "Qdrant collection에 연결할 수 없습니다.") from exc
        if _status_name(_mapping_value(info, "status")) != "green":
            raise _fail("QDRANT_COLLECTION_CONTRACT_MISMATCH", "Qdrant collection 상태가 green이 아닙니다.")
        unnamed = _unnamed_vector_params(info)
        try:
            size = int(_mapping_value(unnamed, "size", -1))
        except (TypeError, ValueError):
            size = -1
        if unnamed is None or size != self.config.vector_size or _status_name(_mapping_value(unnamed, "distance")) != "cosine":
            raise _fail("QDRANT_COLLECTION_CONTRACT_MISMATCH", "Qdrant dense vector 계약이 일치하지 않습니다.")
        count = _mapping_value(count_result, "count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise _fail("KOSIS_RELEASE_MISMATCH", "Qdrant configured release가 존재하지 않습니다.")
        self._preflighted = True

    @staticmethod
    def _authority(payload: Mapping[str, Any]) -> bool:
        authority = payload.get("authority")
        if not isinstance(authority, Mapping) or authority.get("candidate_generation_only") is not True:
            return False
        return all(authority.get(name) is False for name in (
            "dimension_value_evidence_authority",
            "dimension_binding_authority",
            "dimension_completeness_authority",
            "binding_assignment_authority",
        ))

    def _point(self, point: Any) -> dict[str, Any]:
        payload = _mapping_value(point, "payload", {})
        if not isinstance(payload, Mapping) or not REQUIRED_QDRANT_FIELDS.issubset(payload):
            raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant payload 필드가 부족합니다.")
        if any(not isinstance(payload.get(key), str) or not str(payload.get(key)).strip() for key in REQUIRED_QDRANT_FIELDS):
            raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant payload 필드가 비어 있습니다.")
        if str(payload["snapshot_id"]) != self.config.release_id:
            raise _fail("KOSIS_RELEASE_MISMATCH", "Qdrant candidate release가 일치하지 않습니다.")
        if str(payload["field"]) not in ALLOWED_FIELDS or not self._authority(payload):
            raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant candidate authority 계약이 일치하지 않습니다.")
        score = _mapping_value(point, "score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant score가 올바르지 않습니다.")
        point_id = _mapping_value(point, "id")
        if point_id is None or not str(point_id).strip():
            raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant point id가 없습니다.")
        return {
            "table_key": str(payload["table_key"]), "release_id": self.config.release_id,
            "source": "qdrant_dense", "score": float(score),
            "evidence": {
                "channel": "dense", "collection": self.config.collection,
                "receipt_sha256": self.config.receipt_sha256, "point_id": str(point_id),
                "record_id": str(payload["record_id"]), "field": str(payload["field"]),
                "source_id": str(payload["source_id"]), "text_sha256": str(payload["text_sha256"]),
            },
        }

    def search_by_vector(self, vector: Sequence[float], *, fields: Iterable[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if len(vector) != self.config.vector_size or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in vector):
            raise _fail("QDRANT_VECTOR_INVALID", "dense query vector는 1,024개의 유한한 수여야 합니다.", 422)
        if not 1 <= int(limit) <= 100:
            raise _fail("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", 422)
        selected_fields = sorted(set(fields or ALLOWED_FIELDS))
        if not selected_fields or not set(selected_fields).issubset(ALLOWED_FIELDS):
            raise _fail("QDRANT_FIELD_INVALID", "Qdrant 검색 field가 허용되지 않습니다.", 422)
        self.preflight()
        query_filter = _qdrant_filter(self.config.release_id, selected_fields)
        try:
            query_points = getattr(self._client, "query_points", None)
            if query_points is None:
                raise _fail("QDRANT_QUERY_API_UNAVAILABLE", "unnamed Qdrant query API를 사용할 수 없습니다.")
            result = query_points(collection_name=self.config.collection, query=[float(value) for value in vector], query_filter=query_filter, limit=int(limit), with_payload=True, with_vectors=False)
            points = _mapping_value(result, "points", result)
        except BackendError:
            raise
        except Exception as exc:
            raise _fail("QDRANT_UNAVAILABLE", "Qdrant dense 검색에 실패했습니다.") from exc
        if not isinstance(points, list):
            raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant 검색 응답이 올바르지 않습니다.")
        return [self._point(point) for point in points]

    def search_grouped_by_table(
        self,
        vector: Sequence[float],
        *,
        fields: Iterable[str] | None = None,
        limit: int = SEARCH_CHANNEL_TOP_K,
    ) -> dict[str, Any]:
        """Read one deterministic representative per table from the existing collection.

        The extra 101st group is intentionally requested so a tied cutoff is never
        resolved arbitrarily.  There is no ungrouped or legacy fallback.
        """

        if len(vector) != self.config.vector_size or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise _fail("QDRANT_VECTOR_INVALID", "dense query vector는 1,024개의 유한한 수여야 합니다.", 422)
        try:
            page_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise _fail("INVALID_SEARCH_WINDOW", "검색 범위가 올바르지 않습니다.", 422) from exc
        if page_limit != SEARCH_CHANNEL_TOP_K:
            raise _fail("INVALID_SEARCH_WINDOW", "dense 채널은 고유 table Top-100만 허용합니다.", 422)
        selected_fields = sorted(set(fields or ALLOWED_FIELDS))
        if not selected_fields or not set(selected_fields).issubset(ALLOWED_FIELDS):
            raise _fail("QDRANT_FIELD_INVALID", "Qdrant 검색 field가 허용되지 않습니다.", 422)
        self.preflight()
        query_filter = _qdrant_filter(self.config.release_id, selected_fields)
        query_points_groups = getattr(self._client, "query_points_groups", None)
        if query_points_groups is None or qdrant_models is None:
            raise _fail("QDRANT_GROUP_QUERY_UNAVAILABLE", "Qdrant grouped query API를 사용할 수 없습니다.")
        grouped: list[tuple[str, str, dict[str, Any]]] = []
        requested_window = SEARCH_CHANNEL_TOP_K + 1
        expansion_windows: list[int] = []
        while True:
            try:
                result = query_points_groups(
                    collection_name=self.config.collection,
                    query=[float(value) for value in vector],
                    query_filter=query_filter,
                    group_by="table_key",
                    group_size=1,
                    limit=requested_window,
                    search_params=qdrant_models.SearchParams(exact=True),
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:
                raise _fail("QDRANT_UNAVAILABLE", "Qdrant grouped dense 검색에 실패했습니다.") from exc
            groups = _mapping_value(result, "groups")
            if not isinstance(groups, list) or len(groups) > requested_window:
                raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant grouped 검색 응답이 올바르지 않습니다.")
            grouped = []
            seen_group_keys: set[str] = set()
            for group in groups:
                group_id = _mapping_value(group, "id")
                hits = _mapping_value(group, "hits")
                if group_id is None or not str(group_id).strip() or not isinstance(hits, list) or len(hits) != 1:
                    raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant table group 계약이 일치하지 않습니다.")
                candidate = self._point(hits[0])
                table_key = candidate["table_key"]
                if str(group_id) != table_key:
                    raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant group key와 payload table_key가 다릅니다.")
                if table_key in seen_group_keys:
                    raise _fail("QDRANT_PAYLOAD_CONTRACT_MISMATCH", "Qdrant table group이 중복됩니다.")
                seen_group_keys.add(table_key)
                point_id = str(candidate["evidence"]["point_id"])
                grouped.append((table_key, point_id, candidate))
            grouped.sort(key=lambda item: (-float(item[2]["score"]), item[0], item[1]))
            boundary_closed = len(grouped) <= SEARCH_CHANNEL_TOP_K
            cutoff_score: float | None = None
            if len(grouped) > SEARCH_CHANNEL_TOP_K:
                cutoff_score = float(grouped[SEARCH_CHANNEL_TOP_K - 1][2]["score"])
                boundary_closed = float(grouped[SEARCH_CHANNEL_TOP_K][2]["score"]) < cutoff_score
            if boundary_closed:
                break
            if requested_window >= SEARCH_WINDOW_MAX:
                # A collection can expose more than SEARCH_CHANNEL_TOP_K groups
                # with the same score at the cutoff.  There is no safe way to
                # choose 100 representatives from that set without an explicit
                # tie-break contract, so keep only the strictly-better groups.
                # This is a bounded channel degradation; payload/vector/preflight
                # failures above remain hard errors.
                if cutoff_score is None:
                    raise _fail("DENSE_BOUNDARY_TIE_UNRESOLVED", "dense Top-100 경계 동률을 결정할 수 없습니다.")
                observed_tied_count = sum(
                    1 for _, _, item in grouped if float(item["score"]) == cutoff_score
                )
                candidates = [
                    item for _, _, item in grouped
                    if float(item["score"]) > cutoff_score
                ]
                boundary_status = DENSE_BOUNDARY_DROPPED_UNCLOSED_CUTOFF_TIE
                boundary_audit = {
                    "boundary_status": boundary_status,
                    "cutoff_score": cutoff_score,
                    "observed_tied_count": observed_tied_count,
                    "requested_window": requested_window,
                    "expansions": list(expansion_windows),
                }
                break
            requested_window = min(SEARCH_WINDOW_MAX, max(requested_window * 2, SEARCH_CHANNEL_TOP_K * 2 + 2))
            expansion_windows.append(requested_window)
        else:  # pragma: no cover - the loop exits through a boundary decision
            raise _fail("DENSE_BOUNDARY_TIE_UNRESOLVED", "dense Top-100 경계 동률을 결정할 수 없습니다.")
        if boundary_closed:
            candidates = [item[2] for item in grouped[:SEARCH_CHANNEL_TOP_K]]
            cutoff_score = float(grouped[SEARCH_CHANNEL_TOP_K - 1][2]["score"]) if len(grouped) >= SEARCH_CHANNEL_TOP_K else None
            boundary_audit = {
                "boundary_status": DENSE_BOUNDARY_CLOSED,
                "cutoff_score": cutoff_score,
                "observed_tied_count": (
                    sum(1 for _, _, item in grouped if float(item["score"]) == cutoff_score)
                    if cutoff_score is not None else 0
                ),
                "requested_window": requested_window,
                "expansions": list(expansion_windows),
            }
        for rank, candidate in enumerate(candidates, start=1):
            candidate["evidence"] = {
                **candidate["evidence"],
                "rank": rank,
                "group_window": requested_window,
                "group_window_expansions": list(expansion_windows),
                "boundary_status": boundary_audit["boundary_status"],
            }
        relation = "gte" if len(grouped) == requested_window else "eq"
        return {
            "candidates": candidates,
            "window": candidates,
            "total": len(candidates),
            "total_relation": relation,
            "limit": SEARCH_CHANNEL_TOP_K,
            "offset": 0,
            "audit": boundary_audit,
        }
