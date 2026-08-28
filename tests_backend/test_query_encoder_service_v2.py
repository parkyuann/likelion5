from __future__ import annotations

import json
import threading
from urllib import request
from urllib.error import HTTPError

import pytest

from backend.query_encoder_service import (
    FIXED_PROBE_TEXT,
    MODEL_ID,
    MODEL_REVISION,
    REQUEST_CONTRACT,
    RESPONSE_CONTRACT,
    VECTOR_DIMENSION,
    EncoderHTTPServer,
    QueryEncoderService,
    validate_vector,
)


RECEIPT_SHA = "a" * 64
TOKEN = "test-only-internal-token"


def unit_vector() -> list[float]:
    return [1.0] + [0.0] * (VECTOR_DIMENSION - 1)


class FakeTokenizer:
    def __call__(self, text: str, **_kwargs):
        return {"input_ids": list(range(max(1, len(text.split()))))}


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, **_kwargs):
        self.calls.append(list(texts))
        return [unit_vector()]


def make_service() -> QueryEncoderService:
    return QueryEncoderService(
        FakeModel(),
        FakeTokenizer(),
        token=TOKEN,
        receipt_sha256=RECEIPT_SHA,
        cuda_version="12.8",
    )


def run_server(service: QueryEncoderService):
    service.run_fixed_probe()
    server = EncoderHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def call(server, *, token=TOKEN, body=None):
    payload = json.dumps(body or {
        "contract": REQUEST_CONTRACT,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "normalize_embeddings": True,
        "texts": ["정규화된 query"],
    }).encode("utf-8")
    req = request.Request(
        f"http://127.0.0.1:{server.server_port}/v2/query-embeddings",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Service-Token": token,
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_validate_vector_requires_1024_finite_normalized_values():
    assert validate_vector(unit_vector()) == unit_vector()
    with pytest.raises(RuntimeError, match="INVALID_VECTOR"):
        validate_vector([1.0])
    with pytest.raises(RuntimeError, match="INVALID_VECTOR"):
        validate_vector([float("nan")] + [0.0] * (VECTOR_DIMENSION - 1))
    with pytest.raises(RuntimeError, match="NOT_NORMALIZED"):
        validate_vector([0.5] + [0.0] * (VECTOR_DIMENSION - 1))


def test_health_and_embedding_expose_only_pinned_v2_contract():
    service = make_service()
    server, thread = run_server(service)
    try:
        with request.urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        assert health == {
            "status": "READY",
            "contract": "bge-m3-ko-query-encoder-service-v2",
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_receipt_sha256": RECEIPT_SHA,
            "vector_dimension": VECTOR_DIMENSION,
            "dtype": "float32",
            "normalize_embeddings": True,
            "authority": "CANDIDATE_GENERATION_ONLY",
            "device": "cuda",
            "cuda": "12.8",
            "max_length": 1024,
        }
        status, body = call(server)
        assert status == 200
        assert body["contract"] == RESPONSE_CONTRACT
        assert body["model_revision"] == MODEL_REVISION
        assert body["vector_dimension"] == VECTOR_DIMENSION
        assert body["truncated"] is False
        assert len(body["items"]) == 1
        assert body["items"][0]["index"] == 0
        assert len(body["items"][0]["vector"]) == VECTOR_DIMENSION
        # A second, independent connection must not be starved by the first
        # HTTP/1.1 request on the deliberately single-worker service.
        with request.urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
            assert response.headers["Connection"].casefold() == "close"
            assert json.loads(response.read().decode("utf-8"))["status"] == "READY"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_wrong_secret_and_non_normalized_or_wrong_pin_are_rejected():
    server, thread = run_server(make_service())
    try:
        status, body = call(server, token="wrong")
        assert (status, body) == (401, {"error": "UNAUTHORIZED"})
        bad_body = {
            "contract": REQUEST_CONTRACT,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "normalize_embeddings": False,
            "texts": ["정규화된 query"],
        }
        status, body = call(server, body=bad_body)
        assert status == 422
        assert body["error"] == "QUERY_ENCODER_REQUEST_PIN_MISMATCH"
        bad_text = dict(bad_body, normalize_embeddings=True, texts=[" 정규화된 query "])
        status, body = call(server, body=bad_text)
        assert status == 422
        assert body["error"] == "QUERY_ENCODER_QUERY_MUST_BE_NORMALIZED"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_fixed_probe_uses_one_text_per_inference_call():
    model = FakeModel()
    service = QueryEncoderService(
        model,
        FakeTokenizer(),
        token=TOKEN,
        receipt_sha256=RECEIPT_SHA,
        cuda_version="12.8",
    )
    service.run_fixed_probe()
    assert model.calls == [[FIXED_PROBE_TEXT], [FIXED_PROBE_TEXT]]
    assert service.ready is True
