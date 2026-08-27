"""Pinned query-encoder service for the v6 BGE-M3-ko Dense collection.

The service deliberately has no CPU or alternate-model fallback.  Its output
may create candidate membership only; it is not binding evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from typing import Any, Mapping, Sequence
from urllib import request


MODEL_ID = "dragonkue/BGE-m3-ko"
MODEL_REVISION = "7074d66aa46562342193ca4feb3d89bf9dad71b4"
SERVICE_CONTRACT = "bge-m3-ko-query-encoder-service-v1"
VECTOR_DIMENSION = 1024


@dataclass(frozen=True)
class EncoderSettings:
    dtype: str = "float32"
    batch_size: int = 16
    max_length: int = 1024
    device: str = "cuda"
    normalize_embeddings: bool = True


def validate_vector(vector: Sequence[Any]) -> list[float]:
    values = [float(value) for value in vector]
    if len(values) != VECTOR_DIMENSION or not all(math.isfinite(value) for value in values):
        raise RuntimeError("QUERY_ENCODER_INVALID_VECTOR")
    norm = math.sqrt(sum(value * value for value in values))
    if not 0.999 <= norm <= 1.001:
        raise RuntimeError("QUERY_ENCODER_VECTOR_NOT_NORMALIZED")
    return values


class HttpQueryEncoderClient:
    """Strict client which accepts only the pinned corpus-compatible encoder."""

    def __init__(self, endpoint: str, *, timeout_seconds: float = 120.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def encode(self, text: str) -> list[float]:
        payload = json.dumps({"texts": [text]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            self.endpoint + "/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("QUERY_ENCODER_UNAVAILABLE") from exc
        if (
            body.get("contract") != SERVICE_CONTRACT
            or body.get("model_revision") != MODEL_REVISION
            or len(body.get("vectors") or []) != 1
        ):
            raise RuntimeError("QUERY_ENCODER_CONTRACT_MISMATCH")
        return validate_vector(body["vectors"][0])


def create_app(settings: EncoderSettings | None = None):
    """Create the pinned CUDA app; fail closed if CUDA is unavailable."""
    from fastapi import Body, FastAPI, HTTPException
    import torch
    from sentence_transformers import SentenceTransformer

    settings = settings or EncoderSettings()
    if settings.device != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("QUERY_ENCODER_CUDA_UNAVAILABLE")
    torch.use_deterministic_algorithms(True)
    local_snapshot = str(os.getenv("LOCAL_ENCODER_SNAPSHOT") or "").strip()
    model = SentenceTransformer(
        local_snapshot or MODEL_ID,
        **({} if local_snapshot else {"revision": MODEL_REVISION}),
        device=settings.device,
        trust_remote_code=False,
    )
    model.max_seq_length = settings.max_length
    app = FastAPI(title=SERVICE_CONTRACT)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "READY",
            "contract": SERVICE_CONTRACT,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "vector_dimension": VECTOR_DIMENSION,
            "authority": "CANDIDATE_GENERATION_ONLY",
            "settings": settings.__dict__,
            "cuda": torch.version.cuda,
            "torch": torch.__version__,
        }

    @app.post("/embed")
    def embed(body: dict[str, Any] = Body(...)) -> Mapping[str, Any]:
        texts = body.get("texts")
        if (
            not isinstance(texts, list)
            or not texts
            or len(texts) > 32
            or any(not isinstance(text, str) or not text.strip() for text in texts)
        ):
            raise HTTPException(status_code=400, detail="1..32 non-empty texts required")
        matrix = model.encode(
            texts,
            batch_size=settings.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=settings.normalize_embeddings,
        )
        vectors = [validate_vector(row.tolist()) for row in matrix]
        return {
            "contract": SERVICE_CONTRACT,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "vector_dimension": VECTOR_DIMENSION,
            "authority": "CANDIDATE_GENERATION_ONLY",
            "vectors": vectors,
        }

    return app


__all__ = [
    "EncoderSettings", "HttpQueryEncoderClient", "MODEL_ID", "MODEL_REVISION",
    "SERVICE_CONTRACT", "VECTOR_DIMENSION", "create_app", "validate_vector",
]


