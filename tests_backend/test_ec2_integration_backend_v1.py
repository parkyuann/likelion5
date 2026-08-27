from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import develop_verify_service, query_encoder, search_adapter
from backend.errors import BackendError
from backend.runtime_gate import require_pipeline_runtime


RELEASE = "release-1"
COLLECTION = "dense-release-1"
CSRF = {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-origin"}


def _authority() -> dict[str, bool]:
    return {
        "candidate_generation_only": True,
        "dimension_value_evidence_authority": False,
        "dimension_binding_authority": False,
        "dimension_completeness_authority": False,
        "binding_assignment_authority": False,
    }


def _point(index: int, score: float) -> dict[str, Any]:
    key = f"org:{index:03d}"
    return {
        "id": f"point-{index:03d}",
        "score": score,
        "payload": {
            "record_id": f"record-{index:03d}",
            "snapshot_id": RELEASE,
            "table_key": key,
            "field": "TITLE",
            "source_id": f"source-{index:03d}",
            "text_sha256": "a" * 64,
            "authority": _authority(),
        },
    }


def test_qdrant_unnamed_grouped_top101_omits_using_and_uses_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSearchParams:
        def __init__(self, *, exact: bool) -> None:
            self.exact = exact

    class FakeModels:
        SearchParams = FakeSearchParams

        class FieldCondition:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class MatchValue:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class MatchAny:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class Filter:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

    monkeypatch.setattr(search_adapter, "qdrant_models", FakeModels)
    captured: dict[str, Any] = {}

    class Client:
        def get_collections(self, **kwargs: Any) -> dict[str, Any]:
            return {"collections": [{"name": COLLECTION}]}

        def get_collection(self, **kwargs: Any) -> dict[str, Any]:
            return {"status": "green", "config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}}}}

        def count(self, **kwargs: Any) -> dict[str, Any]:
            return {"count": 101}

        def query_points_groups(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"groups": [{"id": point["payload"]["table_key"], "hits": [point]} for point in (_point(i, 1.0 - i / 1000) for i in range(101))]}

    config = search_adapter.QdrantConfig("http://qdrant", RELEASE, COLLECTION, 1024, "b" * 64)
    result = search_adapter.QdrantDenseAdapter(config, client=Client()).search_grouped_by_table([0.0] * 1024)

    assert len(result["candidates"]) == 100
    assert result["total_relation"] == "gte"
    assert captured["limit"] == 101
    assert captured["group_by"] == "table_key"
    assert captured["group_size"] == 1
    assert "using" not in captured
    assert captured["search_params"].exact is True
    assert captured["query_filter"] is not None


def test_qdrant_named_vector_is_rejected_and_env_name_must_be_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BackendError) as direct:
        search_adapter.QdrantConfig("http://qdrant", RELEASE, COLLECTION, 1024, "b" * 64, "dense")
    assert direct.value.code == "QDRANT_VECTOR_CONFIGURATION_PENDING"

    monkeypatch.setenv("QDRANT_URL", "http://qdrant")
    monkeypatch.setenv("KOSIS_RELEASE_ID", RELEASE)
    monkeypatch.setenv("QDRANT_COLLECTION", COLLECTION)
    monkeypatch.setenv("QDRANT_VECTOR_SIZE", "1024")
    monkeypatch.setenv("QDRANT_RECEIPT_SHA256", "b" * 64)
    monkeypatch.setenv("QDRANT_VECTOR_NAME", "dense")
    with pytest.raises(BackendError) as configured:
        search_adapter.QdrantConfig.from_env()
    assert configured.value.code == "QDRANT_CONFIGURATION_PENDING"


def test_query_encoder_requires_every_explicit_runtime_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "BGE_QUERY_ENCODER_ENABLED": "true",
        "BGE_QUERY_ENCODER_URL": "http://bge-query-encoder:8101",
        "BGE_QUERY_ENCODER_CONTRACT": query_encoder.ENCODER_CONTRACT,
        "BGE_QUERY_ENCODER_MODEL_ID": query_encoder.ENCODER_MODEL_ID,
        "BGE_QUERY_ENCODER_MODEL_REVISION": query_encoder.ENCODER_MODEL_REVISION,
        "BGE_QUERY_ENCODER_VECTOR_SIZE": "1024",
        "BGE_QUERY_ENCODER_MODEL_RECEIPT_SHA256": "b" * 64,
        "BGE_QUERY_ENCODER_TIMEOUT_SECONDS": "3.0",
        "BGE_QUERY_ENCODER_TOKEN_FILE": "/run/secrets/bge_query_encoder_token",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert query_encoder.QueryEncoderConfig.from_env().vector_size == 1024

    for missing in values:
        with monkeypatch.context() as scoped:
            for name, value in values.items():
                scoped.setenv(name, value)
            scoped.delenv(missing, raising=False)
            with pytest.raises(BackendError) as caught:
                query_encoder.QueryEncoderConfig.from_env()
            assert caught.value.code == "QUERY_ENCODER_CONFIGURATION_PENDING"


def test_pipeline_runtime_flag_is_exact_true(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("", "True", "1", "true "):
        monkeypatch.setenv("PIPELINE_RUNTIME_ENABLED", value)
        with pytest.raises(BackendError) as caught:
            require_pipeline_runtime()
        assert caught.value.code == "PIPELINE_RUNTIME_PENDING"

    monkeypatch.setenv("PIPELINE_RUNTIME_ENABLED", "true")
    require_pipeline_runtime()


def test_analyze_only_explicit_article_calls_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[str, str, str]] = []

    def fake_verify(text: str, title: str = "", date: str = "") -> dict[str, Any]:
        called.append((text, title, date))
        return {"type": "article", "status": "structured_only"}

    monkeypatch.setenv("PIPELINE_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5173")
    monkeypatch.setattr(develop_verify_service, "verify_article_develop", fake_verify)
    client = TestClient(app_module.app)

    article = client.post("/api/v1/analyze", headers=CSRF, json={"text": "기사", "input_type": "article"})
    assert article.status_code == 200
    assert called == [("기사", "", "")]

    query = client.post("/api/v1/analyze", headers=CSRF, json={"text": "2025년 출생아", "input_type": "query"})
    assert query.status_code == 503
    assert query.json()["code"] == "PIPELINE_NATURAL_QUERY_PENDING"

    url = client.post("/api/v1/analyze", headers=CSRF, json={"text": "https://example.com/a", "input_type": "auto"})
    assert url.status_code == 503
    assert url.json()["code"] == "PIPELINE_URL_PENDING"

    image = client.post("/api/v1/analyze/image", headers=CSRF, files={"file": ("a.png", b"x", "image/png")})
    assert image.status_code == 503
    assert image.json()["code"] == "PIPELINE_IMAGE_PENDING"


def test_verify_develop_invokes_service_after_runtime_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[str, str, str]] = []

    def fake_verify(text: str, title: str = "", date: str = "") -> dict[str, Any]:
        called.append((text, title, date))
        return {"type": "article", "status": "structured_only"}

    monkeypatch.setenv("PIPELINE_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5173")
    monkeypatch.setattr(develop_verify_service, "verify_article_develop", fake_verify)
    response = TestClient(app_module.app).post(
        "/api/v1/verify/develop",
        headers=CSRF,
        json={"text": "기사", "title": "제목", "date": "2026-08-27"},
    )
    assert response.status_code == 200
    assert called == [("기사", "제목", "2026-08-27")]


def test_live_stage_false_ignores_service_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stages: list[str] = []

    class FakeTraceError(Exception):
        pass

    def fake_run_trace(*, articles_path: Path, output_root: Path, stage: str, config_path: Path | None = None) -> None:
        stages.append(stage)
        output_root.mkdir(parents=True, exist_ok=True)
        if stage == "l1":
            (output_root / "01_value_candidates.jsonl").write_text('{"kind":"value_unit"}\n', encoding="utf-8")
            (output_root / "01_sentences.jsonl").write_text(
                '{"sentence_id":"s1","char_start":0,"char_end":3}\n', encoding="utf-8"
            )

    monkeypatch.setenv("PIPELINE_LIVE_STAGE_ENABLED", "false")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant")
    monkeypatch.setenv("BGE_QUERY_ENCODER_URL", "http://bge-query-encoder:8101")
    monkeypatch.setenv("BGE_RERANKER_URL", "http://reranker:8102")
    monkeypatch.setattr(develop_verify_service, "_load_trace_runner", lambda: (fake_run_trace, FakeTraceError))

    result = develop_verify_service.verify_article_develop("기사")
    assert stages == ["l1", "l2", "layers"]
    assert result["status"] == "structured_only"
    assert result["live"] is False
