"""Strict internal client for the release-pinned BGE query encoder service."""

from __future__ import annotations

import hashlib
import math
import os
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from backend.errors import BackendError


ENCODER_CONTRACT = "bge-m3-ko-query-encoder-service-v2"
ENCODER_REQUEST_CONTRACT = "bge-m3-ko-query-encoder-request-v2"
ENCODER_RESPONSE_CONTRACT = "bge-m3-ko-query-encoder-response-v2"
ENCODER_MODEL_ID = "dragonkue/BGE-m3-ko"
ENCODER_MODEL_REVISION = "7074d66aa46562342193ca4feb3d89bf9dad71b4"
ENCODER_VECTOR_SIZE = 1024
ENCODER_MAX_LENGTH = 1024
ENCODER_URL = "http://bge-query-encoder:8101"
ENCODER_TIMEOUT_SECONDS = 3.0
ENCODER_RECEIPT_ENV = "BGE_QUERY_ENCODER_MODEL_RECEIPT_SHA256"


def _fail(code: str, message: str, status_code: int = 503) -> BackendError:
    return BackendError(code, message, status_code=status_code)


def normalize_encoder_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(query or "")).split())


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in "0123456789abcdefABCDEF" for char in value)


def _value(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, Mapping):
        return payload.get(key, default)
    return getattr(payload, key, default)


@dataclass(frozen=True)
class QueryEncoderConfig:
    enabled: bool
    url: str
    contract: str
    model_id: str
    model_revision: str
    vector_size: int
    model_receipt_sha256: str
    timeout_seconds: float
    token_file: str

    @classmethod
    def from_env(cls) -> "QueryEncoderConfig":
        enabled = os.getenv("BGE_QUERY_ENCODER_ENABLED", "").strip() == "true"
        url = os.getenv("BGE_QUERY_ENCODER_URL", "").strip().rstrip("/")
        contract = os.getenv("BGE_QUERY_ENCODER_CONTRACT", "").strip()
        model_id = os.getenv("BGE_QUERY_ENCODER_MODEL_ID", "").strip()
        revision = os.getenv("BGE_QUERY_ENCODER_MODEL_REVISION", "").strip()
        receipt = os.getenv(ENCODER_RECEIPT_ENV, "").strip().lower()
        token_file = os.getenv(
            "BGE_QUERY_ENCODER_TOKEN_FILE", ""
        ).strip()
        raw_size = os.getenv("BGE_QUERY_ENCODER_VECTOR_SIZE", "").strip()
        raw_timeout = os.getenv(
            "BGE_QUERY_ENCODER_TIMEOUT_SECONDS", ""
        ).strip()
        try:
            vector_size = int(raw_size)
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            raise _fail("QUERY_ENCODER_CONFIGURATION_PENDING", "query encoder 설정이 올바르지 않습니다.")
        if (
            not enabled
            or contract != ENCODER_CONTRACT
            or model_id != ENCODER_MODEL_ID
            or revision != ENCODER_MODEL_REVISION
            or vector_size != ENCODER_VECTOR_SIZE
            or not _is_hex(receipt, 64)
            or timeout_seconds != ENCODER_TIMEOUT_SECONDS
            or not token_file
        ):
            raise _fail(
                "QUERY_ENCODER_CONFIGURATION_PENDING",
                "query encoder strict 설정이 준비되지 않았습니다.",
            )
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise _fail("QUERY_ENCODER_CONFIGURATION_PENDING", "query encoder URL이 올바르지 않습니다.") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "bge-query-encoder"
            or port != 8101
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise _fail(
                "QUERY_ENCODER_CONFIGURATION_PENDING",
                "query encoder는 내부 Docker HTTP URL이어야 합니다.",
            )
        return cls(
            enabled=True,
            url=url,
            contract=contract,
            model_id=model_id,
            model_revision=revision,
            vector_size=vector_size,
            model_receipt_sha256=receipt,
            timeout_seconds=timeout_seconds,
            token_file=token_file,
        )


class BGEQueryEncoderClient:
    """Read-only, no-retry client for the internal v2 encoder contract."""

    def __init__(
        self,
        config: QueryEncoderConfig | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self.config = config or QueryEncoderConfig.from_env()
        self._token = self._read_token(self.config.token_file)
        self._client = client or httpx.Client(
            base_url=self.config.url,
            timeout=httpx.Timeout(2.5, connect=0.5),
        )
        self._ready = False

    @staticmethod
    def _read_token(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as token_file:
                token = token_file.read().strip()
        except Exception as exc:
            raise _fail("QUERY_ENCODER_CONFIGURATION_PENDING", "query encoder 내부 인증 설정을 읽을 수 없습니다.") from exc
        if not token or len(token) > 4096:
            raise _fail("QUERY_ENCODER_CONFIGURATION_PENDING", "query encoder 내부 인증 설정이 올바르지 않습니다.")
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            elif int(getattr(response, "status_code", 200)) >= 400:
                raise RuntimeError("encoder response failed")
            return response
        except BackendError:
            raise
        except Exception as exc:
            raise _fail("QUERY_ENCODER_UNAVAILABLE", "query encoder service에 연결할 수 없습니다.") from exc

    def preflight(self) -> None:
        if self._ready:
            return
        response = self._request("GET", "/health")
        try:
            payload = response.json()
        except Exception as exc:
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder readiness 응답이 올바르지 않습니다.") from exc
        expected = {
            "status": "READY",
            "contract": self.config.contract,
            "model_id": self.config.model_id,
            "model_revision": self.config.model_revision,
            "model_receipt_sha256": self.config.model_receipt_sha256,
            "vector_dimension": self.config.vector_size,
            "dtype": "float32",
            "normalize_embeddings": True,
            "authority": "CANDIDATE_GENERATION_ONLY",
            "device": "cuda",
            "max_length": ENCODER_MAX_LENGTH,
        }
        if not isinstance(payload, Mapping) or any(payload.get(key) != value for key, value in expected.items()):
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder readiness 계약이 일치하지 않습니다.")
        cuda = payload.get("cuda")
        if not isinstance(cuda, str) or not cuda.strip():
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder CUDA readiness가 없습니다.")
        self._ready = True

    def encode(self, query: str) -> tuple[list[float], dict[str, Any]]:
        normalized = normalize_encoder_query(query)
        encoded = normalized.encode("utf-8")
        if not 1 <= len(normalized) <= 200 or len(encoded) > 800:
            raise _fail("QUERY_ENCODER_INPUT_INVALID", "query encoder 입력 범위를 초과했습니다.", 422)
        self.preflight()
        input_sha256 = hashlib.sha256(encoded).hexdigest()
        request_body = {
            "contract": ENCODER_REQUEST_CONTRACT,
            "model_id": self.config.model_id,
            "model_revision": self.config.model_revision,
            "normalize_embeddings": True,
            "texts": [normalized],
        }
        response = self._request(
            "POST",
            "/v2/query-embeddings",
            headers={"X-Internal-Service-Token": self._token},
            json=request_body,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder 응답이 올바르지 않습니다.") from exc
        if not isinstance(payload, Mapping):
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder 응답 계약이 일치하지 않습니다.")
        exact_response = {
            "contract": ENCODER_RESPONSE_CONTRACT,
            "model_id": self.config.model_id,
            "model_revision": self.config.model_revision,
            "model_receipt_sha256": self.config.model_receipt_sha256,
            "vector_dimension": self.config.vector_size,
            "dtype": "float32",
            "normalized": True,
            "truncated": False,
        }
        if any(payload.get(key) != value for key, value in exact_response.items()):
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder 응답 pin이 일치하지 않습니다.")
        items = payload.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or len(items) != 1:
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder item 수가 일치하지 않습니다.")
        item = items[0]
        vector = _value(item, "vector")
        if (
            _value(item, "index") != 0
            or _value(item, "input_sha256") != input_sha256
            or _value(item, "token_count") is None
            or isinstance(_value(item, "token_count"), bool)
            or not isinstance(_value(item, "token_count"), int)
            or not 1 <= int(_value(item, "token_count")) <= ENCODER_MAX_LENGTH
            or not isinstance(vector, Sequence)
            or isinstance(vector, (str, bytes))
            or len(vector) != self.config.vector_size
        ):
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder vector attestation이 일치하지 않습니다.")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in vector):
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder vector가 유한하지 않습니다.")
        values = [float(value) for value in vector]
        l2_norm = math.sqrt(sum(value * value for value in values))
        reported_norm = _value(item, "l2_norm")
        if (
            isinstance(reported_norm, bool)
            or not isinstance(reported_norm, (int, float))
            or not math.isfinite(float(reported_norm))
            or abs(float(reported_norm) - l2_norm) > 1e-4
            or not 0.999 <= l2_norm <= 1.001
        ):
            raise _fail("QUERY_ENCODER_CONTRACT_MISMATCH", "query encoder L2 정규화가 일치하지 않습니다.")
        evidence = {
            "model_id": self.config.model_id,
            "model_revision": self.config.model_revision,
            "model_receipt_sha256": self.config.model_receipt_sha256,
            "vector_dimension": self.config.vector_size,
            "normalized": True,
        }
        return values, evidence
