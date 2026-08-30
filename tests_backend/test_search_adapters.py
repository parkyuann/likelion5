from __future__ import annotations

import hashlib
from typing import Any

import pytest

from backend import metadata_repository, search_adapter, table_catalog_service
from backend.errors import BackendError


RELEASE = "release-1"
INDEX = "kosis_standard-v1_release-1"
COLLECTION = "dense-release-1"


def _metadata_row(key: str, *, org: str = "org-1", title: str = "Title") -> dict[str, Any]:
    return {
        "snapshot_id": RELEASE, "table_key": key, "org_id": org, "tbl_id": key.split(":")[-1],
        "stat_id": "stat-1", "title_raw": title, "title_norm": title.casefold(),
        "org_name_raw": "Organization", "org_name_norm": "organization", "status": "active",
        "send_de": "20260827", "source_row_sha256": "a" * 64, "extra_json": {},
    }


def _hit(key: str, score: float, *, text: str = "2025 출생아") -> dict[str, Any]:
    return {
        "_id": f"doc-{key}", "_score": score,
        "_source": {
            "record_id": f"record-{key}", "snapshot_id": RELEASE, "table_key": key,
            "field": "TITLE", "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_id": f"source-{key}",
        },
    }


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeSearchHTTP:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.hits = hits

    def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, path, kwargs))
        if method == "GET":
            properties = {
                name: {"type": "text" if name == "text" else "keyword"}
                for name in search_adapter.REQUIRED_OPENSEARCH_FIELDS
            }
            return FakeResponse({INDEX: {"mappings": {"_source": {"enabled": True}, "properties": properties}}})
        if path.endswith("/_count"):
            return FakeResponse({"count": len(self.hits)})
        return FakeResponse({"hits": {"total": {"value": len(self.hits), "relation": "eq"}, "hits": self.hits}})


def test_metadata_repository_is_read_only_and_uses_canonical_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        def __init__(self, rows: list[dict[str, Any]] | None = None, row: dict[str, Any] | None = None) -> None:
            self.rows, self.row = rows or [], row

        def fetchall(self) -> list[dict[str, Any]]:
            return self.rows

        def fetchone(self) -> dict[str, Any] | None:
            return self.row if self.row is not None else (self.rows[0] if self.rows else None)

    class Connection:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def execute(self, sql: str, params: tuple[Any, ...]) -> Result:
            self.sql.append(sql)
            assert sql.lstrip().upper().startswith("SELECT")
            if sql == metadata_repository.RELEASE_ATTESTATION_SQL:
                return Result(row={"?column?": 1})
            if "COUNT(*)" in sql:
                return Result(row={"count": 1})
            if "GROUP BY" in sql:
                return Result(rows=[{"org_id": "org-1", "org_name_raw": "Organization", "count": 1}])
            return Result(rows=[_metadata_row("org-1:t1")])

        def close(self) -> None:
            pass

    connection = Connection()
    captured: dict[str, Any] = {}

    def factory(*args: Any, **kwargs: Any) -> Connection:
        captured.update(kwargs)
        return connection

    monkeypatch.setenv("KOSIS_METADATA_DATABASE_URL", "postgresql://metadata")
    repo = metadata_repository.MetadataRepository(factory)
    result = repo.browse_tables(RELEASE, organization="org-1")
    hydrated = repo.hydrate_tables(RELEASE, ["org-1:t1"])
    fetched = repo.get_table(RELEASE, "org-1:t1")
    assert result["items"][0]["title_raw"] == "Title"
    assert hydrated[0]["table_key"] == "org-1:t1"
    assert fetched is not None and fetched["table_key"] == "org-1:t1"
    assert captured["options"] == metadata_repository.READ_ONLY_OPTIONS
    attestations = [sql for sql in connection.sql if sql == metadata_repository.RELEASE_ATTESTATION_SQL]
    table_selects = [sql for sql in connection.sql if "FROM statistics_table" in sql and sql != metadata_repository.RELEASE_ATTESTATION_SQL and "COUNT(*)" not in sql and "GROUP BY" not in sql]
    assert len(attestations) == 3
    assert len(table_selects) == 3
    assert all("org_name_raw" in sql and "title_raw" in sql and "category_path" not in sql and "kosis_url" not in sql and "tbl_name" not in sql for sql in table_selects)


def test_metadata_get_missing_returns_none() -> None:
    class Result:
        def __init__(self, row: dict[str, Any] | None = None) -> None:
            self.row = row

        def fetchone(self) -> dict[str, Any] | None:
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.attested = False

        def execute(self, sql: str, params: tuple[Any, ...]) -> Result:
            assert sql.lstrip().upper().startswith("SELECT")
            if sql == metadata_repository.RELEASE_ATTESTATION_SQL:
                self.attested = True
                return Result({"?column?": 1})
            assert self.attested
            return Result()

        def close(self) -> None:
            pass

    repo = metadata_repository.MetadataRepository(lambda *args, **kwargs: Connection())
    import os
    os.environ["KOSIS_METADATA_DATABASE_URL"] = "postgresql://metadata"
    assert repo.get_table(RELEASE, "missing") is None


@pytest.mark.parametrize("method", ["browse", "hydrate", "get"])
def test_metadata_wrong_release_zero_fails_closed(method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        def fetchone(self) -> None:
            return None

    class Connection:
        def execute(self, sql: str, params: tuple[Any, ...]) -> Result:
            assert sql == metadata_repository.RELEASE_ATTESTATION_SQL
            return Result()

        def close(self) -> None:
            pass

    monkeypatch.setenv("KOSIS_METADATA_DATABASE_URL", "postgresql://metadata")
    repo = metadata_repository.MetadataRepository(lambda *args, **kwargs: Connection())
    with pytest.raises(BackendError) as caught:
        if method == "browse":
            repo.browse_tables("wrong-release")
        elif method == "hydrate":
            repo.hydrate_tables("wrong-release", ["org:t1"])
        else:
            repo.get_table("wrong-release", "org:t1")
    assert caught.value.code == "KOSIS_RELEASE_MISMATCH"
    assert caught.value.status_code == 503


def test_opensearch_mapping_and_query_contract_has_no_literal_analyzer() -> None:
    client = FakeSearchHTTP([_hit("org:t2", 1.0), _hit("org:t1", 1.0), _hit("org:t3", 2.0)])
    config = search_adapter.OpenSearchConfig("http://opensearch", RELEASE, "standard-v1", INDEX)
    adapter = search_adapter.OpenSearchBM25Adapter(config, client=client)
    result = adapter.search("2025", limit=3)
    assert [item["table_key"] for item in result["candidates"]] == ["org:t3", "org:t1", "org:t2"]
    body = client.calls[-1][2]["json"]
    assert "analyzer" not in body["query"]["bool"]["must"][0]["match"]["text"]
    assert body["collapse"] == {"field": "table_key"}
    assert body["sort"] == [{"_score": {"order": "desc"}}, {"table_key": {"order": "asc"}}]
    count_calls = [call for call in client.calls if call[1].endswith("/_count")]
    assert len(count_calls) == 1
    assert count_calls[0][0] == "POST"
    assert count_calls[0][2]["json"] == {"query": {"term": {"snapshot_id": RELEASE}}}


def test_opensearch_wrong_release_zero_fails_closed() -> None:
    class ZeroCount(FakeSearchHTTP):
        def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
            self.calls.append((method, path, kwargs))
            if method == "GET":
                properties = {
                    name: {"type": "text" if name == "text" else "keyword"}
                    for name in search_adapter.REQUIRED_OPENSEARCH_FIELDS
                }
                return FakeResponse({INDEX: {"mappings": {"_source": {"enabled": True}, "properties": properties}}})
            if path.endswith("/_count"):
                return FakeResponse({"count": 0})
            raise AssertionError("search must not run after a failed count attestation")

    client = ZeroCount([])
    config = search_adapter.OpenSearchConfig("http://opensearch", RELEASE, "standard-v1", INDEX)
    with pytest.raises(BackendError) as caught:
        search_adapter.OpenSearchBM25Adapter(config, client=client).search("x", limit=1)
    assert caught.value.code == "KOSIS_RELEASE_MISMATCH"
    assert caught.value.status_code == 503


@pytest.mark.parametrize("mutator", ["hash", "release"])
def test_opensearch_hit_contract_fails_closed(mutator: str) -> None:
    hit = _hit("org:t1", 1.0)
    if mutator == "hash":
        hit["_source"]["text_sha256"] = "0" * 64
    else:
        hit["_source"]["snapshot_id"] = "other-release"
    config = search_adapter.OpenSearchConfig("http://opensearch", RELEASE, "standard-v1", INDEX)
    with pytest.raises(BackendError) as caught:
        search_adapter.OpenSearchBM25Adapter(config, client=FakeSearchHTTP([hit])).search("x", limit=1)
    assert caught.value.code in {"SEARCH_SOURCE_CONTRACT_MISMATCH", "KOSIS_RELEASE_MISMATCH"}


def test_opensearch_window_and_legacy_env_are_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [_hit(f"org:{index}", 1.0, text=str(index)) for index in range(100)]
    config = search_adapter.OpenSearchConfig("http://opensearch", RELEASE, "standard-v1", INDEX)
    result = search_adapter.OpenSearchBM25Adapter(config, client=FakeSearchHTTP(hits)).search("x", limit=1, offset=99)
    assert result["total_relation"] == "gte"
    assert result["candidates"][0]["table_key"] == "org:99"
    monkeypatch.setenv("OPENSEARCH_URL", "http://opensearch")
    monkeypatch.setenv("KOSIS_RELEASE_ID", RELEASE)
    monkeypatch.setenv("KOSIS_OPENSEARCH_INDEX", INDEX)
    monkeypatch.delenv("KOSIS_BM25_INDEX", raising=False)
    with pytest.raises(BackendError):
        search_adapter.OpenSearchConfig.from_env()


def test_empty_query_uses_metadata_only_and_hydrates_nonempty_in_candidate_order(monkeypatch: pytest.MonkeyPatch) -> None:
    class Metadata:
        def __init__(self) -> None:
            self.browse_calls = 0
            self.hydrate_calls: list[list[str]] = []

        def browse_tables(self, release_id: str, **kwargs: Any) -> dict[str, Any]:
            self.browse_calls += 1
            return {"items": [_metadata_row("org:t1")], "total": 1, "organizations": [{"id": "org-1", "name": "Organization", "count": 1}]}

        def hydrate_tables(self, release_id: str, keys: list[str]) -> list[dict[str, Any]]:
            self.hydrate_calls.append(keys)
            return [_metadata_row(key, title=key) for key in keys]

        def get_table(self, release_id: str, key: str) -> dict[str, Any] | None:
            return None

    class BM25:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            return {"window": [
                {"table_key": "org:t2", "release_id": RELEASE, "source": "opensearch_bm25", "score": 2.0, "evidence": {"record_id": "r2", "source_id": "s2", "field": "TITLE", "text_sha256": "a" * 64, "index": INDEX}},
                {"table_key": "org:t1", "release_id": RELEASE, "source": "opensearch_bm25", "score": 1.0, "evidence": {"record_id": "r1", "source_id": "s1", "field": "TITLE", "text_sha256": "b" * 64, "index": INDEX}},
            ], "total_relation": "eq"}

    class Dense:
        def search_grouped_by_table(self, vector: list[float], **kwargs: Any) -> dict[str, Any]:
            assert len(vector) == 1024
            return {"window": [], "total_relation": "eq"}

    class Encoder:
        def encode(self, query: str) -> tuple[list[float], dict[str, Any]]:
            return [0.0] * 1024, {"model_revision": "test-revision", "vector_dimension": 1024}

    metadata = Metadata()
    bm25 = BM25()
    monkeypatch.setenv("KOSIS_RELEASE_ID", RELEASE)
    table_catalog_service.configure_adapters(metadata=metadata, bm25=bm25, dense=Dense(), encoder=Encoder())
    monkeypatch.setenv("KOSIS_HYBRID_PATH_TOP_K", "100")
    monkeypatch.setenv("KOSIS_HYBRID_FUSION_TOP_K", "100")
    monkeypatch.setenv("KOSIS_HYBRID_RRF_K", "60")
    monkeypatch.setenv("BGE_RERANKER_ENABLED", "false")
    empty = table_catalog_service.search_tables("  ")
    assert empty["items"][0]["org_name"] == "Organization"
    assert metadata.browse_calls == 1 and bm25.calls == 0
    nonempty = table_catalog_service.search_tables("query", limit=2)
    assert [item["table_key"] for item in nonempty["items"]] == ["org:t2", "org:t1"]
    assert metadata.hydrate_calls == [["org:t2", "org:t1"]]
    table_catalog_service.configure_adapters(metadata=None, bm25=None)


def test_empty_nfkc_query_allows_large_metadata_offset_but_bm25_window_fails_before_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    class Metadata:
        def __init__(self) -> None:
            self.browse_args: list[tuple[int, int]] = []

        def browse_tables(self, release_id: str, **kwargs: Any) -> dict[str, Any]:
            self.browse_args.append((kwargs["limit"], kwargs["offset"]))
            return {"items": [], "total": 1001, "organizations": []}

        def hydrate_tables(self, release_id: str, keys: list[str]) -> list[dict[str, Any]]:
            raise AssertionError("metadata hydrate must not run for a rejected BM25 window")

    class BM25:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError("BM25 must not run for a rejected window")

    metadata = Metadata()
    bm25 = BM25()
    monkeypatch.setenv("KOSIS_RELEASE_ID", RELEASE)
    table_catalog_service.configure_adapters(metadata=metadata, bm25=bm25)
    empty = table_catalog_service.search_tables("\u3000\u2003", limit=1, offset=1000)
    assert empty["offset"] == 1000
    assert metadata.browse_args == [(1, 1000)]

    with pytest.raises(BackendError) as caught:
        table_catalog_service.search_tables("query", limit=1, offset=1000)
    assert caught.value.code == "SEARCH_WINDOW_EXCEEDED"
    assert caught.value.status_code == 422
    assert bm25.calls == 0
    assert metadata.browse_args == [(1, 1000)]
    table_catalog_service.configure_adapters(metadata=None, bm25=None)


def test_qdrant_named_vector_and_authority_contract() -> None:
    class Client:
        def get_collections(self, **kwargs: Any) -> dict[str, Any]:
            return {"collections": [{"name": COLLECTION}]}

        def get_collection(self, **kwargs: Any) -> dict[str, Any]:
            return {"status": "green", "config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}}}}

        def count(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["exact"] is True
            assert kwargs["count_filter"] is not None
            assert "snapshot_id" in repr(kwargs["count_filter"])
            assert RELEASE in repr(kwargs["count_filter"])
            return {"count": 1}

        def query_points(self, **kwargs: Any) -> dict[str, Any]:
            assert "using" not in kwargs
            assert kwargs["query_filter"] is not None
            assert "snapshot_id" in repr(kwargs["query_filter"])
            assert RELEASE in repr(kwargs["query_filter"])
            return {"points": [{"id": "p1", "score": 0.9, "payload": {
                "record_id": "r1", "snapshot_id": RELEASE, "table_key": "org:t1", "field": "ITEM", "source_id": "s1", "text_sha256": "a" * 64,
                "authority": {"candidate_generation_only": True, "dimension_value_evidence_authority": False, "dimension_binding_authority": False, "dimension_completeness_authority": False, "binding_assignment_authority": False},
            }}]}

    config = search_adapter.QdrantConfig("http://qdrant", RELEASE, COLLECTION, 1024, "b" * 64)
    result = search_adapter.QdrantDenseAdapter(config, client=Client()).search_by_vector([0.0] * 1024, fields=["ITEM"], limit=1)
    assert result[0]["table_key"] == "org:t1"
    assert result[0]["evidence"]["receipt_sha256"] == "b" * 64


def test_qdrant_rejects_invalid_vector_and_authority() -> None:
    class Client:
        def get_collections(self, **kwargs: Any) -> dict[str, Any]:
            return {"collections": [{"name": COLLECTION}]}

        def get_collection(self, **kwargs: Any) -> dict[str, Any]:
            return {"status": "green", "config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}}}}

        def count(self, **kwargs: Any) -> dict[str, Any]:
            return {"count": 1}

        def query_points(self, **kwargs: Any) -> dict[str, Any]:
            return {"points": [{"id": "p1", "score": 0.9, "payload": {"record_id": "r1", "snapshot_id": RELEASE, "table_key": "org:t1", "field": "ITEM", "source_id": "s1", "text_sha256": "a" * 64, "authority": "CANDIDATE_GENERATION_ONLY"}}]}

    config = search_adapter.QdrantConfig("http://qdrant", RELEASE, COLLECTION, 1024, "b" * 64)
    adapter = search_adapter.QdrantDenseAdapter(config, client=Client())
    with pytest.raises(BackendError) as invalid_vector:
        adapter.search_by_vector([float("nan")] * 1024)
    assert invalid_vector.value.code == "QDRANT_VECTOR_INVALID"
    with pytest.raises(BackendError) as invalid_authority:
        adapter.search_by_vector([0.0] * 1024)
    assert invalid_authority.value.code == "QDRANT_PAYLOAD_CONTRACT_MISMATCH"


def test_qdrant_alias_is_rejected_before_collection_lookup() -> None:
    class Client:
        def get_collections(self, **kwargs: Any) -> dict[str, Any]:
            return {"collections": [{"name": "actual-collection"}]}

        def get_collection(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("alias must be rejected from inventory before get_collection")

    config = search_adapter.QdrantConfig("http://qdrant", RELEASE, "collection-alias", 1024, "b" * 64)
    with pytest.raises(BackendError) as caught:
        search_adapter.QdrantDenseAdapter(config, client=Client()).search_by_vector([0.0] * 1024)
    assert caught.value.code == "QDRANT_COLLECTION_NOT_CONCRETE"


def test_qdrant_wrong_release_zero_fails_closed() -> None:
    class Client:
        def get_collections(self, **kwargs: Any) -> dict[str, Any]:
            return {"collections": [{"name": COLLECTION}]}

        def get_collection(self, **kwargs: Any) -> dict[str, Any]:
            return {"status": "green", "config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}}}}

        def count(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["count_filter"] is not None
            assert kwargs["exact"] is True
            return {"count": 0}

        def query_points(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("query must not run after a failed count attestation")

    config = search_adapter.QdrantConfig("http://qdrant", RELEASE, COLLECTION, 1024, "b" * 64)
    with pytest.raises(BackendError) as caught:
        search_adapter.QdrantDenseAdapter(config, client=Client()).search_by_vector([0.0] * 1024)
    assert caught.value.code == "KOSIS_RELEASE_MISMATCH"
    assert caught.value.status_code == 503
