"""Internal, pinned BGE-M3-ko query encoder service.

This process is deliberately small and dependency-free at the HTTP boundary.
The base image supplies the CUDA/model runtime; this module never downloads a
model, falls back to CPU, or exposes an alternate embedding route.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import math
import os
from pathlib import Path
import platform
import secrets
import sys
from typing import Any, Mapping, Sequence
import unicodedata


MODEL_ID = "dragonkue/BGE-m3-ko"
MODEL_REVISION = "7074d66aa46562342193ca4feb3d89bf9dad71b4"
SERVICE_CONTRACT = "bge-m3-ko-query-encoder-service-v2"
REQUEST_CONTRACT = "bge-m3-ko-query-encoder-request-v2"
RESPONSE_CONTRACT = "bge-m3-ko-query-encoder-response-v2"
VECTOR_DIMENSION = 1024
MAX_LENGTH = 1024
MAX_QUERY_CODEPOINTS = 200
MAX_QUERY_BYTES = 800
MODEL_CLOSURE_SHA256 = "fd2d4a2ecb5443f856b6bb991d7d6d4dda2b03dbe0fa428f6b9a6da51ed3312f"
BASE_IMAGE_DIGEST = "sha256:b98d5ae3c07824c21e2f0242e9cf488c47563a6c0320e9876a4af245f7538adb"
FIXED_PROBE_TEXT = "고정 BGE query encoder readiness probe"
TOKEN_HEADER = "X-Internal-Service-Token"


class EncoderConfigurationError(RuntimeError):
    """Raised when the pinned runtime cannot be proven safe to start."""


class RequestContractError(ValueError):
    """Raised for a malformed or non-pinned API request."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_MANIFEST_INVALID") from exc
    if not isinstance(value, Mapping):
        raise EncoderConfigurationError("QUERY_ENCODER_MANIFEST_INVALID")
    return value


def _hex_sha(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and len(value) == length and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _normalise_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def validate_vector(vector: Sequence[Any]) -> list[float]:
    if isinstance(vector, (str, bytes)) or len(vector) != VECTOR_DIMENSION:
        raise RuntimeError("QUERY_ENCODER_INVALID_VECTOR")
    try:
        values = [float(item) for item in vector]
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("QUERY_ENCODER_INVALID_VECTOR") from exc
    if not all(math.isfinite(item) for item in values):
        raise RuntimeError("QUERY_ENCODER_INVALID_VECTOR")
    norm = math.sqrt(sum(item * item for item in values))
    if not math.isfinite(norm) or not 0.999 <= norm <= 1.001:
        raise RuntimeError("QUERY_ENCODER_VECTOR_NOT_NORMALIZED")
    return values


def _receipt_value(receipt: Mapping[str, Any], *names: str) -> Any:
    """Read a scalar from the known preflight receipt shapes only."""
    for name in names:
        if name in receipt:
            return receipt[name]
    for container_name in ("result", "attestation", "model", "probe"):
        nested = receipt.get(container_name)
        if isinstance(nested, Mapping):
            for name in names:
                if name in nested:
                    return nested[name]
    return None


def validate_model_receipt(path: Path, expected_sha256: str) -> Mapping[str, Any]:
    if not _hex_sha(expected_sha256):
        raise EncoderConfigurationError("QUERY_ENCODER_RECEIPT_PIN_INVALID")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_RECEIPT_UNAVAILABLE") from exc
    if not hmac.compare_digest(_sha256_bytes(raw), expected_sha256.lower()):
        raise EncoderConfigurationError("QUERY_ENCODER_RECEIPT_SHA_MISMATCH")
    receipt = _read_json(path)
    model = receipt.get("model")
    output = receipt.get("output")
    runtime = receipt.get("runtime")
    if (
        receipt.get("schema_version") != "news-verification-bge-gpu-preflight-v1"
        or receipt.get("status") != "READY"
        or receipt.get("component") != "query_encoder"
        or receipt.get("authority") != "candidate_generation_only"
        or not isinstance(model, Mapping)
        or model.get("id") != MODEL_ID
        or model.get("revision") != MODEL_REVISION
        or model.get("model_safetensors_sha256")
        != "da164fa90633e730db4ee91a79ebf99a0826fb31d6f002c4a8ec7952a286c4f4"
        or not isinstance(output, Mapping)
        or output.get("dimension") != VECTOR_DIMENSION
        or output.get("finite") is not True
        or output.get("normalized") is not True
        or output.get("repeat_exact") is not True
        or output.get("shape") != [2, VECTOR_DIMENSION]
        or not isinstance(runtime, Mapping)
        or runtime.get("python") != "3.11.16"
        or runtime.get("torch") != "2.13.0+cu130"
        or runtime.get("transformers") != "5.15.1"
        or runtime.get("sentence_transformers") != "6.0.0"
        or runtime.get("huggingface_hub") != "1.28.0"
        or runtime.get("numpy") != "2.4.6"
        or runtime.get("cuda_runtime") != "13.0"
    ):
        raise EncoderConfigurationError("QUERY_ENCODER_RECEIPT_CONTRACT_MISMATCH")
    return receipt


def validate_model_closure(
    manifest_path: Path,
    repository_root: Path,
    snapshot_path: Path,
) -> Mapping[str, Any]:
    try:
        if not hmac.compare_digest(_sha256_file(manifest_path), MODEL_CLOSURE_SHA256):
            raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_SHA_MISMATCH")
    except OSError as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_UNAVAILABLE") from exc
    manifest = _read_json(manifest_path)
    if (
        manifest.get("contract") != "bge-model-snapshot-closure-v1"
        or manifest.get("model_id") != MODEL_ID
        or manifest.get("revision") != MODEL_REVISION
    ):
        raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_CONTRACT_MISMATCH")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != 11:
        raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_FILE_COUNT_MISMATCH")
    try:
        repository_real = repository_root.resolve(strict=True)
        snapshot_real = snapshot_path.resolve(strict=True)
    except OSError as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_MODEL_SNAPSHOT_UNAVAILABLE") from exc
    try:
        snapshot_real.relative_to(repository_real)
    except ValueError as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_MODEL_PATH_ESCAPE") from exc
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_ENTRY_INVALID")
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or relative in seen
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(entry.get("size"), int)
            or isinstance(entry.get("size"), bool)
            or not _hex_sha(entry.get("sha256"))
        ):
            raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_ENTRY_INVALID")
        seen.add(relative)
        candidate = snapshot_path / relative
        try:
            candidate_real = candidate.resolve(strict=True)
            candidate_real.relative_to(repository_real)
            if not candidate_real.is_file() or candidate.stat().st_size != entry["size"]:
                raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_FILE_MISMATCH")
            if not hmac.compare_digest(_sha256_file(candidate), str(entry["sha256"]).lower()):
                raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_FILE_MISMATCH")
        except EncoderConfigurationError:
            raise
        except (OSError, ValueError) as exc:
            raise EncoderConfigurationError("QUERY_ENCODER_CLOSURE_FILE_MISMATCH") from exc
    return manifest


def validate_runtime_lock(path: Path) -> Mapping[str, Any]:
    lock = _read_json(path)
    if lock.get("contract") != "bge-query-encoder-runtime-lock-v1":
        raise EncoderConfigurationError("QUERY_ENCODER_RUNTIME_LOCK_INVALID")
    if lock.get("base_image_digest") != BASE_IMAGE_DIGEST:
        raise EncoderConfigurationError("QUERY_ENCODER_BASE_IMAGE_MISMATCH")
    python_version = lock.get("python")
    if not isinstance(python_version, str) or platform.python_version() != python_version:
        raise EncoderConfigurationError("QUERY_ENCODER_PYTHON_VERSION_MISMATCH")
    packages = lock.get("packages")
    if not isinstance(packages, Mapping):
        raise EncoderConfigurationError("QUERY_ENCODER_RUNTIME_LOCK_INVALID")
    package_modules = {
        "torch": "torch",
        "transformers": "transformers",
        "sentence-transformers": "sentence_transformers",
        "huggingface-hub": "huggingface_hub",
        "numpy": "numpy",
    }
    for package, module_name in package_modules.items():
        expected = packages.get(package)
        if not isinstance(expected, str) or not expected:
            raise EncoderConfigurationError("QUERY_ENCODER_RUNTIME_LOCK_INVALID")
        try:
            module = __import__(module_name)
            actual = str(getattr(module, "__version__"))
        except Exception as exc:
            raise EncoderConfigurationError("QUERY_ENCODER_RUNTIME_UNAVAILABLE") from exc
        if actual != expected:
            raise EncoderConfigurationError("QUERY_ENCODER_RUNTIME_VERSION_MISMATCH")
    return lock


class QueryEncoderService:
    """Loaded model plus the exact v2 request/response contract."""

    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        *,
        token: str,
        receipt_sha256: str,
        cuda_version: str,
    ) -> None:
        if not token or len(token) > 4096:
            raise EncoderConfigurationError("QUERY_ENCODER_TOKEN_INVALID")
        if not _hex_sha(receipt_sha256):
            raise EncoderConfigurationError("QUERY_ENCODER_RECEIPT_PIN_INVALID")
        if not isinstance(cuda_version, str) or not cuda_version.strip():
            raise EncoderConfigurationError("QUERY_ENCODER_CUDA_UNAVAILABLE")
        self.model = model
        self.tokenizer = tokenizer
        self.token = token
        self.receipt_sha256 = receipt_sha256.lower()
        self.cuda_version = cuda_version
        self.ready = False

    @staticmethod
    def _token_count(tokenizer: Any, text: str) -> int:
        try:
            encoded = tokenizer(
                text,
                add_special_tokens=True,
                truncation=False,
                return_attention_mask=False,
            )
            input_ids = encoded["input_ids"] if isinstance(encoded, Mapping) else encoded.input_ids
            if input_ids and isinstance(input_ids[0], (list, tuple)):
                input_ids = input_ids[0]
            count = len(input_ids)
        except Exception as exc:
            raise RuntimeError("QUERY_ENCODER_TOKENIZATION_FAILED") from exc
        if not isinstance(count, int) or count < 1:
            raise RuntimeError("QUERY_ENCODER_TOKENIZATION_FAILED")
        return count

    def _encode(self, text: str) -> tuple[list[float], int, float]:
        token_count = self._token_count(self.tokenizer, text)
        if token_count > MAX_LENGTH:
            raise RequestContractError("QUERY_ENCODER_INPUT_TOO_LONG", status_code=413)
        try:
            matrix = self.model.encode(
                [text],
                batch_size=1,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            row = matrix[0]
            if hasattr(row, "tolist"):
                row = row.tolist()
            vector = validate_vector(row)
        except RequestContractError:
            raise
        except Exception as exc:
            raise RuntimeError("QUERY_ENCODER_INFERENCE_FAILED") from exc
        l2_norm = math.sqrt(sum(value * value for value in vector))
        return vector, token_count, l2_norm

    def run_fixed_probe(self) -> None:
        first, _, _ = self._encode(FIXED_PROBE_TEXT)
        second, _, _ = self._encode(FIXED_PROBE_TEXT)
        if first != second:
            raise EncoderConfigurationError("QUERY_ENCODER_PROBE_NOT_REPEAT_EXACT")
        self.ready = True

    def health(self) -> dict[str, Any]:
        return {
            "status": "READY" if self.ready else "NOT_READY",
            "contract": SERVICE_CONTRACT,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_receipt_sha256": self.receipt_sha256,
            "vector_dimension": VECTOR_DIMENSION,
            "dtype": "float32",
            "normalize_embeddings": True,
            "authority": "CANDIDATE_GENERATION_ONLY",
            "device": "cuda",
            "cuda": self.cuda_version,
            "max_length": MAX_LENGTH,
        }

    def embed(self, body: Mapping[str, Any]) -> dict[str, Any]:
        required = {"contract", "model_id", "model_revision", "normalize_embeddings", "texts"}
        if set(body) != required:
            raise RequestContractError("QUERY_ENCODER_REQUEST_CONTRACT_MISMATCH")
        if (
            body["contract"] != REQUEST_CONTRACT
            or body["model_id"] != MODEL_ID
            or body["model_revision"] != MODEL_REVISION
            or body["normalize_embeddings"] is not True
        ):
            raise RequestContractError("QUERY_ENCODER_REQUEST_PIN_MISMATCH")
        texts = body["texts"]
        if not isinstance(texts, list) or len(texts) != 1 or not isinstance(texts[0], str):
            raise RequestContractError("QUERY_ENCODER_BATCH_MUST_BE_ONE")
        text = texts[0]
        if text != _normalise_query(text) or not text:
            raise RequestContractError("QUERY_ENCODER_QUERY_MUST_BE_NORMALIZED")
        if len(text) > MAX_QUERY_CODEPOINTS or len(text.encode("utf-8")) > MAX_QUERY_BYTES:
            raise RequestContractError("QUERY_ENCODER_INPUT_INVALID")
        vector, token_count, l2_norm = self._encode(text)
        return {
            "contract": RESPONSE_CONTRACT,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_receipt_sha256": self.receipt_sha256,
            "vector_dimension": VECTOR_DIMENSION,
            "dtype": "float32",
            "normalized": True,
            "truncated": False,
            "items": [{
                "index": 0,
                "input_sha256": _sha256_bytes(text.encode("utf-8")),
                "token_count": token_count,
                "l2_norm": l2_norm,
                "vector": vector,
            }],
        }


class EncoderHTTPServer(http.server.HTTPServer):
    """HTTPServer intentionally uses one request worker for deterministic GPU use."""

    def __init__(self, address: tuple[str, int], service: QueryEncoderService) -> None:
        self.service = service
        super().__init__(address, EncoderRequestHandler)


class EncoderRequestHandler(http.server.BaseHTTPRequestHandler):
    server: EncoderHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Request paths and headers must not become an accidental secret/query log.
        return

    def _send(self, status: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        # HTTPServer is intentionally single-worker.  Closing every response
        # prevents one idle HTTP/1.1 keep-alive connection from monopolising
        # the server and starving Docker health checks.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTPServer API
        if self.path != "/health":
            self._send(404, {"error": "NOT_FOUND"})
            return
        self._send(200 if self.server.service.ready else 503, self.server.service.health())

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTPServer API
        if self.path != "/v2/query-embeddings":
            self._send(404, {"error": "NOT_FOUND"})
            return
        supplied = self.headers.get(TOKEN_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, self.server.service.token):
            self._send(401, {"error": "UNAUTHORIZED"})
            return
        if not (self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json"):
            self._send(415, {"error": "CONTENT_TYPE_REQUIRED"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > 64 * 1024:
            self._send(413, {"error": "REQUEST_TOO_LARGE"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, Mapping):
                raise RequestContractError("QUERY_ENCODER_REQUEST_INVALID")
            response = self.server.service.embed(body)
        except RequestContractError as exc:
            self._send(exc.status_code, {"error": str(exc)})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._send(400, {"error": "INVALID_JSON"})
            return
        except Exception:
            self._send(503, {"error": "QUERY_ENCODER_UNAVAILABLE"})
            return
        self._send(200, response)


def _read_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_TOKEN_UNAVAILABLE") from exc
    if not value or len(value) > 4096:
        raise EncoderConfigurationError("QUERY_ENCODER_TOKEN_INVALID")
    return value


def create_service_from_environment() -> QueryEncoderService:
    """Validate every pin, load only the local CUDA snapshot, and probe it."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    runtime_lock = Path(os.getenv("BGE_QUERY_ENCODER_RUNTIME_LOCK_PATH", "/app/encoder-runtime.lock.json"))
    runtime_contract = validate_runtime_lock(runtime_lock)
    repository = Path(os.getenv("BGE_QUERY_ENCODER_MODEL_REPOSITORY", "/models/repository"))
    revision = os.getenv("BGE_QUERY_ENCODER_MODEL_REVISION", MODEL_REVISION).strip()
    if revision != MODEL_REVISION:
        raise EncoderConfigurationError("QUERY_ENCODER_MODEL_REVISION_MISMATCH")
    snapshot = repository / "snapshots" / MODEL_REVISION
    receipt_path = Path(os.getenv(
        "BGE_QUERY_ENCODER_MODEL_RECEIPT_PATH",
        "/receipts/query_encoder_preflight_20260827.json",
    ))
    closure_path = Path(os.getenv(
        "BGE_QUERY_ENCODER_MODEL_CLOSURE_PATH",
        "/app/bge-model-closure-7074d66a.json",
    ))
    receipt_sha = os.getenv("BGE_QUERY_ENCODER_MODEL_RECEIPT_SHA256", "").strip().lower()
    validate_model_receipt(receipt_path, receipt_sha)
    validate_model_closure(closure_path, repository, snapshot)
    token_path = Path(os.getenv(
        "BGE_QUERY_ENCODER_TOKEN_FILE",
        "/run/secrets/bge_query_encoder_token",
    ))
    token = _read_secret(token_path)
    try:
        import torch
        from sentence_transformers import SentenceTransformer
    except Exception as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_RUNTIME_UNAVAILABLE") from exc
    if not torch.cuda.is_available():
        raise EncoderConfigurationError("QUERY_ENCODER_CUDA_UNAVAILABLE")
    if str(torch.version.cuda or "") != str(runtime_contract.get("cuda") or ""):
        raise EncoderConfigurationError("QUERY_ENCODER_CUDA_VERSION_MISMATCH")
    try:
        torch.use_deterministic_algorithms(True)
        model = SentenceTransformer(
            str(snapshot),
            device="cuda",
            trust_remote_code=False,
            local_files_only=True,
        )
        model.max_seq_length = MAX_LENGTH
        dimension = model.get_sentence_embedding_dimension()
        tokenizer = model.tokenizer
    except Exception as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_MODEL_LOAD_FAILED") from exc
    if dimension != VECTOR_DIMENSION:
        raise EncoderConfigurationError("QUERY_ENCODER_DIMENSION_MISMATCH")
    service = QueryEncoderService(
        model,
        tokenizer,
        token=token,
        receipt_sha256=receipt_sha,
        cuda_version=str(torch.version.cuda or ""),
    )
    service.run_fixed_probe()
    return service


def main() -> int:
    service = create_service_from_environment()
    host = os.getenv("BGE_QUERY_ENCODER_HOST", "0.0.0.0")
    try:
        port = int(os.getenv("BGE_QUERY_ENCODER_PORT", "8101"))
    except ValueError as exc:
        raise EncoderConfigurationError("QUERY_ENCODER_PORT_INVALID") from exc
    if not 1 <= port <= 65535:
        raise EncoderConfigurationError("QUERY_ENCODER_PORT_INVALID")
    server = EncoderHTTPServer((host, port), service)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
