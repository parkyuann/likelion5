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
SERVICE_CONTRACT = "bge-reranker-v2-m3-ko-service-v2"
MAX_CANDIDATES = 50
IDENTITY_FIELDS = ("tokenizer_tree_manifest_sha256", "service_source_sha256", "image_digest", "driver")
TIMEOUT_ENV = "BGE_RERANKER_TIMEOUT_MS"
IMAGE_DIGEST_ENV = "BGE_RERANKER_IMAGE_DIGEST"
MODEL_MANIFEST_ENV = "BGE_RERANKER_MODEL_MANIFEST_SHA256"
SERVICE_SOURCE_ENV = "BGE_RERANKER_SERVICE_SOURCE_SHA256"
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


def canonical_json(value: Any) -> bytes:
    """The byte representation shared by service and client request hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def request_sha256(query: str, candidates: Sequence[Mapping[str, str]]) -> str:
    payload = {"contract": SERVICE_CONTRACT, "query": query, "candidates": [
        {"candidate_id": str(row["candidate_id"]), "text": str(row["text"])} for row in candidates
    ]}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def input_ids_sha256(candidates: Sequence[Mapping[str, str]]) -> str:
    ids = [str(row["candidate_id"]) for row in candidates]
    return hashlib.sha256(canonical_json(ids)).hexdigest()


def parse_timeout_ms(value: Any = None) -> int:
    """Require the immutable deployment timeout contract (250..30000 ms)."""
    raw = os.getenv(TIMEOUT_ENV) if value is None else value
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("BGE_RERANKER_TIMEOUT_MS_INVALID")
    text = str(raw).strip()
    if not text.isdecimal() or str(int(text)) != text:
        raise ValueError("BGE_RERANKER_TIMEOUT_MS_INVALID")
    parsed = int(text)
    if not 250 <= parsed <= 30000:
        raise ValueError("BGE_RERANKER_TIMEOUT_MS_INVALID")
    return parsed


def validate_endpoint(endpoint: str) -> str:
    """Only the internal HTTP DNS endpoint is accepted by the deployment client."""
    value = str(endpoint or "").rstrip("/")
    if value != "http://bge-reranker:8102":
        raise ValueError("RERANKER_ENDPOINT_INVALID")
    return value


def validate_rerank_response(
    response: Any, query: str, candidates: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    if not isinstance(response, Mapping):
        raise RuntimeError("RERANKER_INVALID_RESPONSE")
    if response.get("contract") != SERVICE_CONTRACT or response.get("model_id") != MODEL_ID or response.get("model_revision") != MODEL_REVISION:
        raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
    if any(not str(response.get(field) or "") for field in IDENTITY_FIELDS):
        raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
    if response.get("request_sha256") != request_sha256(query, candidates) or response.get("input_ids_sha256") != input_ids_sha256(candidates):
        raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
    rows = response.get("results")
    expected = [str(row["candidate_id"]) for row in candidates]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise RuntimeError("RERANKER_INVALID_RESPONSE")
    seen: list[str] = []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"candidate_id", "raw_logit", "sigmoid_score"}:
            raise RuntimeError("RERANKER_INVALID_RESPONSE")
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id or candidate_id in seen or candidate_id not in expected:
            raise RuntimeError("RERANKER_INVALID_RESPONSE")
        try:
            raw = float(row["raw_logit"])
            sigmoid = float(row["sigmoid_score"])
        except (TypeError, ValueError):
            raise RuntimeError("RERANKER_INVALID_RESPONSE") from None
        if not math.isfinite(raw) or not math.isfinite(sigmoid) or not 0.0 <= sigmoid <= 1.0:
            raise RuntimeError("RERANKER_INVALID_RESPONSE")
        seen.append(candidate_id)
        normalized.append({"candidate_id": candidate_id, "raw_logit": raw, "sigmoid_score": sigmoid})
    if set(seen) != set(expected):
        raise RuntimeError("RERANKER_INVALID_RESPONSE")
    if seen != [row["candidate_id"] for row in sorted(normalized, key=lambda item: (-item["raw_logit"], item["candidate_id"]))]:
        raise RuntimeError("RERANKER_INVALID_RESPONSE")
    return normalized


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

    def __init__(self, endpoint: str, *, timeout_seconds: float | None = None, timeout_ms: int | None = None) -> None:
        self.endpoint = validate_endpoint(endpoint)
        if timeout_ms is None:
            timeout_ms = parse_timeout_ms() if timeout_seconds is None else int(float(timeout_seconds) * 1000)
        self.timeout_ms = parse_timeout_ms(timeout_ms)
        self.timeout_seconds = self.timeout_ms / 1000.0
        self.last_request_sha256: str | None = None
        self.last_response_sha256: str | None = None
        self.expected_identity: dict[str, str] | None = None

    def set_expected_identity(self, health: Mapping[str, Any]) -> None:
        identity = {field: str(health.get(field) or "") for field in IDENTITY_FIELDS}
        if any(not value for value in identity.values()):
            raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
        self.expected_identity = identity

    def health(self) -> dict[str, Any]:
        req = request.Request(self.endpoint + "/health", method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read()
                value = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            raise RuntimeError("RERANKER_INVALID_RESPONSE") from None
        except Exception as exc:
            raise RuntimeError("RERANKER_UNAVAILABLE") from exc
        if not isinstance(value, Mapping) or value.get("status") != "READY" or value.get("contract") != SERVICE_CONTRACT or value.get("model_id") != MODEL_ID or value.get("model_revision") != MODEL_REVISION or any(not str(value.get(field) or "") for field in IDENTITY_FIELDS):
            raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
        return dict(value)

    def _post(self, path: str, payload: Mapping[str, Any]) -> Any:
        self.last_request_sha256 = hashlib.sha256(canonical_json(payload)).hexdigest()
        req = request.Request(
            self.endpoint + path,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read()
                self.last_response_sha256 = hashlib.sha256(body).hexdigest()
                return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            raise RuntimeError("RERANKER_INVALID_RESPONSE") from None
        except Exception as exc:
            raise RuntimeError("RERANKER_UNAVAILABLE") from exc

    def rerank(self, query: str, passages: Sequence[Mapping[str, str]]) -> Sequence[Mapping[str, Any]]:
        query = str(query or "")
        rows = list(passages)
        if not query.strip() or not 1 <= len(rows) <= MAX_CANDIDATES:
            raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {"candidate_id", "text"} or not str(row.get("candidate_id") or "").strip() or not isinstance(row.get("text"), str):
                raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
        if len({str(row["candidate_id"]) for row in rows}) != len(rows):
            raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
        payload = {"contract": SERVICE_CONTRACT, "query": query, "candidates": rows}
        response = self._post("/rerank", payload)
        if self.expected_identity is not None and any(response.get(field) != value for field, value in self.expected_identity.items()):
            raise RuntimeError("RERANKER_CONTRACT_MISMATCH")
        return validate_rerank_response(response, query, rows)


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
    if not local_snapshot or not Path(local_snapshot).is_dir():
        raise RuntimeError("RERANKER_MODEL_SNAPSHOT_UNAVAILABLE")
    model = CrossEncoder(
        local_snapshot,
        **{},
        device=settings.device,
        max_length=settings.max_length,
        trust_remote_code=False,
    )
    app = FastAPI(title=SERVICE_CONTRACT)

    manifest_root = Path(local_snapshot) if local_snapshot else None
    manifest = snapshot_manifest(manifest_root) if manifest_root and manifest_root.is_dir() else None
    manifest_sha = hashlib.sha256(canonical_json(manifest)).hexdigest() if manifest is not None else ""
    expected_manifest_sha = str(os.getenv(MODEL_MANIFEST_ENV) or "")
    if not expected_manifest_sha or manifest_sha != expected_manifest_sha:
        raise RuntimeError("RERANKER_MODEL_MANIFEST_MISMATCH")
    image_digest = str(os.getenv(IMAGE_DIGEST_ENV) or "").split("@")[-1]
    driver = str(os.getenv("CUDA_DRIVER_VERSION") or "")
    if not image_digest.startswith("sha256:") or not os.getenv(SERVICE_SOURCE_ENV) or not driver:
        raise RuntimeError("RERANKER_DEPLOYMENT_IDENTITY_UNAVAILABLE")

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
            "tokenizer_tree_manifest_sha256": manifest_sha,
            "model_manifest_sha256": manifest_sha,
            "service_source_sha256": str(os.getenv(SERVICE_SOURCE_ENV) or ""),
            "image_digest": image_digest,
            "driver": driver,
            "max_candidates": MAX_CANDIDATES,
            "max_length": settings.max_length,
            "batch_size": settings.batch_size,
            "device": settings.device,
            "dtype": settings.dtype,
        }

    @app.post("/rerank")
    def rerank(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not isinstance(body, Mapping) or body.get("contract") != SERVICE_CONTRACT:
            raise HTTPException(status_code=400, detail="contract mismatch")
        query = body.get("query")
        candidates = body.get("candidates")
        if not isinstance(query, str) or not query.strip() or not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
            raise HTTPException(status_code=400, detail="query and 1..50 candidates required")
        if any(
            not isinstance(candidate, Mapping)
            or set(candidate) != {"candidate_id", "text"}
            or not str(candidate.get("candidate_id") or "").strip()
            or not isinstance(candidate.get("text"), str)
        for candidate in candidates
        ):
            raise HTTPException(status_code=400, detail="each candidate requires only candidate_id and text")
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
            "tokenizer_tree_manifest_sha256": manifest_sha,
            "service_source_sha256": str(os.getenv(SERVICE_SOURCE_ENV) or ""),
            "image_digest": image_digest,
            "driver": driver,
            "settings": settings.__dict__,
            "results": rows,
            "request_sha256": request_sha256(query, candidates),
            "input_ids_sha256": input_ids_sha256(candidates),
        }

    return app


__all__ = [
    "HttpRerankerClient", "MODEL_ID", "MODEL_REVISION", "SERVICE_CONTRACT",
    "MAX_CANDIDATES", "ServiceSettings", "create_app", "snapshot_manifest",
    "canonical_json", "request_sha256", "input_ids_sha256", "parse_timeout_ms",
    "validate_endpoint", "validate_rerank_response",
]
