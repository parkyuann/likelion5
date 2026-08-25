"""Pinned GPU service and strict client for the Korean BGE v2-m3 reranker.

The model is loaded only by :func:`create_app`, allowing CPU development and
contract tests without downloading the 2.3 GB model snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib import request


MODEL_ID = "dragonkue/bge-reranker-v2-m3-ko"
MODEL_REVISION = "2aca5884ecac490192af9ebd86836d9073d826cd"
SERVICE_CONTRACT = "bge-reranker-v2-m3-ko-service-v1"
CUBLAS_WORKSPACE_DEFAULT = ":4096:8"


def configure_deterministic_cuda_environment() -> str:
    """Supply the cuBLAS prerequisite used by PyTorch deterministic mode."""
    return os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", CUBLAS_WORKSPACE_DEFAULT)


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_manifest(snapshot_root: Path) -> dict[str, Any]:
    """Hash every model file after the pinned revision is downloaded."""
    files = [path for path in sorted(snapshot_root.rglob("*")) if path.is_file()]
    return {
        "contract": SERVICE_CONTRACT,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "files": [
            {"path": path.relative_to(snapshot_root).as_posix(), "size": path.stat().st_size, "sha256": _sha_file(path)}
            for path in files
        ],
    }


@dataclass(frozen=True)
class ServiceSettings:
    dtype: str = "float32"
    max_length: int = 1024
    batch_size: int = 8
    device: str = "cuda"


class HttpRerankerClient:
    """No-fallback HTTP client for the internal GPU service."""

    def __init__(self, endpoint: str, *, timeout_seconds: float = 120.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _post(self, path: str, payload: Mapping[str, Any]) -> Any:
        req = request.Request(
            self.endpoint + path,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("RERANKER_UNAVAILABLE") from exc

    def rerank(self, query: str, passages: Sequence[Mapping[str, str]]) -> Sequence[Mapping[str, Any]]:
        response = self._post("/rerank", {"query": query, "candidates": list(passages)})
        if response.get("contract") != SERVICE_CONTRACT or response.get("model_revision") != MODEL_REVISION:
            raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
        return list(response.get("results") or [])


def create_app(settings: ServiceSettings | None = None):
    """Create the FastAPI GPU app, failing rather than silently using CPU."""
    configure_deterministic_cuda_environment()
    from fastapi import Body, FastAPI, HTTPException
    import torch
    from sentence_transformers import CrossEncoder

    settings = settings or ServiceSettings()
    if settings.device != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("RERANKER_CUDA_UNAVAILABLE")
    torch.use_deterministic_algorithms(True)
    local_snapshot = str(os.getenv("LOCAL_RERANKER_SNAPSHOT") or "").strip()
    model = CrossEncoder(
        local_snapshot or MODEL_ID,
        **({} if local_snapshot else {"revision": MODEL_REVISION}),
        device=settings.device,
        max_length=settings.max_length,
        trust_remote_code=False,
    )
    app = FastAPI(title=SERVICE_CONTRACT)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "READY",
            "contract": SERVICE_CONTRACT,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "settings": settings.__dict__,
            "cuda": torch.version.cuda,
            "torch": torch.__version__,
        }

    @app.post("/rerank")
    def rerank(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        query = str(body.get("query") or "").strip()
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not query or not candidates or len(candidates) > 100:
            raise HTTPException(status_code=400, detail="query and 1..100 candidates required")
        if any(
            not isinstance(candidate, Mapping)
            or not str(candidate.get("candidate_id") or "").strip()
            or not isinstance(candidate.get("text"), str)
            for candidate in candidates
        ):
            raise HTTPException(status_code=400, detail="each candidate requires candidate_id and text")
        if len({str(candidate["candidate_id"]) for candidate in candidates}) != len(candidates):
            raise HTTPException(status_code=400, detail="candidate IDs must be unique")
        pairs = [(query, str(candidate["text"])) for candidate in candidates]
        logits = model.predict(
            pairs,
            batch_size=settings.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        rows = [
            {
                "candidate_id": str(candidate["candidate_id"]),
                "raw_logit": float(logit),
                "sigmoid_score": 1.0 / (1.0 + math.exp(-float(logit))),
            }
            for candidate, logit in zip(candidates, logits, strict=True)
        ]
        rows.sort(key=lambda row: (-row["raw_logit"], row["candidate_id"]))
        return {
            "contract": SERVICE_CONTRACT,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "settings": settings.__dict__,
            "results": rows,
        }

    return app


__all__ = [
    "HttpRerankerClient", "MODEL_ID", "MODEL_REVISION", "SERVICE_CONTRACT",
    "ServiceSettings", "create_app", "snapshot_manifest",
]
