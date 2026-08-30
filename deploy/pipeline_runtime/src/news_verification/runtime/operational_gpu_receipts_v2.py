"""Immutable GPU execution receipt validation for operational pipeline v2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from src.news_verification.runtime.bge_m3_ko_query_encoder_service import (
    MODEL_ID as ENCODER_MODEL_ID,
    MODEL_REVISION as ENCODER_REVISION,
    SERVICE_CONTRACT as ENCODER_CONTRACT,
)
from src.news_verification.runtime.bge_reranker_v2_service import (
    MODEL_ID as RERANKER_MODEL_ID,
    MODEL_REVISION as RERANKER_REVISION,
    SERVICE_CONTRACT as RERANKER_CONTRACT,
)


CONTRACT = "operational-gpu-receipts-v2"


class GpuReceiptError(ValueError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.is_file():
        raise GpuReceiptError(f"GPU_RECEIPT_MISSING:{path.name}")
    if sha256_file(path) != expected_sha256:
        raise GpuReceiptError(f"GPU_RECEIPT_SHA_MISMATCH:{path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GpuReceiptError(f"GPU_RECEIPT_INVALID_JSON:{path.name}") from exc
    if not isinstance(value, dict):
        raise GpuReceiptError(f"GPU_RECEIPT_OBJECT_REQUIRED:{path.name}")
    return value


def _cuda_health(health: Mapping[str, Any]) -> bool:
    return (
        health.get("status") == "READY"
        and str((health.get("settings") or {}).get("device") or "").lower() == "cuda"
        and bool(str(health.get("cuda") or "").strip())
    )


def validate_gpu_receipts(
    query_encoder_path: str | Path,
    reranker_path: str | Path,
    *,
    query_encoder_sha256: str,
    reranker_sha256: str,
) -> dict[str, Any]:
    encoder_path = Path(query_encoder_path)
    reranker_path = Path(reranker_path)
    encoder = _read(encoder_path, query_encoder_sha256)
    reranker = _read(reranker_path, reranker_sha256)
    encoder_health = encoder.get("health") if isinstance(encoder.get("health"), Mapping) else {}
    reranker_health = reranker.get("health") if isinstance(reranker.get("health"), Mapping) else {}
    errors: list[str] = []
    if encoder.get("status") != "READY" or encoder.get("repeat_exact") is not True:
        errors.append("QUERY_ENCODER_EXECUTION_NOT_READY")
    if (
        encoder_health.get("contract") != ENCODER_CONTRACT
        or encoder_health.get("model_id") != ENCODER_MODEL_ID
        or encoder_health.get("model_revision") != ENCODER_REVISION
        or encoder_health.get("vector_dimension") != 1024
        or encoder_health.get("authority") != "CANDIDATE_GENERATION_ONLY"
        or not _cuda_health(encoder_health)
        or len(str(encoder.get("vectors_sha256") or "")) != 64
    ):
        errors.append("QUERY_ENCODER_RECEIPT_CONTRACT_MISMATCH")
    if (
        reranker.get("status") != "READY"
        or reranker.get("repeat_exact") is not True
        or reranker.get("normal_http_statuses") != [200, 200]
        or reranker.get("candidate_count") != 100
        or reranker.get("over_100_http_status") != 400
        or reranker.get("duplicate_id_http_status") != 400
    ):
        errors.append("RERANKER_EXECUTION_NOT_READY")
    if (
        reranker_health.get("contract") != RERANKER_CONTRACT
        or reranker_health.get("model_id") != RERANKER_MODEL_ID
        or reranker_health.get("model_revision") != RERANKER_REVISION
        or not _cuda_health(reranker_health)
        or len(str(reranker.get("results_sha256") or "")) != 64
    ):
        errors.append("RERANKER_RECEIPT_CONTRACT_MISMATCH")
    if errors:
        raise GpuReceiptError(",".join(errors))
    return {
        "contract": CONTRACT,
        "status": "READY",
        "query_encoder": {
            "path": str(encoder_path), "sha256": query_encoder_sha256,
            "model_revision": ENCODER_REVISION, "cuda": encoder_health["cuda"],
            "torch": encoder_health.get("torch"), "repeat_exact": True,
        },
        "reranker": {
            "path": str(reranker_path), "sha256": reranker_sha256,
            "model_revision": RERANKER_REVISION, "cuda": reranker_health["cuda"],
            "torch": reranker_health.get("torch"), "repeat_exact": True,
        },
        "authority": "CANDIDATE_GENERATION_ONLY",
    }


__all__ = ["CONTRACT", "GpuReceiptError", "sha256_file", "validate_gpu_receipts"]




