"""Pinned, candidate-only DiffuRank pointwise service.

This process intentionally has no PostgreSQL, OpenSearch, Qdrant, Redis, KOSIS
API, verdict, or cell-value client.  Its authority ends at returning a stable
ordering for the candidate IDs supplied by the data/API node.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sys
from typing import Any


SERVICE_CONTRACT = "diffurank-pointwise-shadow-service-v1"
DIFFUSIONRANK_REPOSITORY = "https://github.com/liuqi6777/DiffusionRank.git"
DIFFUSIONRANK_COMMIT = "8f38364f22db68a506e80a217add08fab739e8cf"
BASE_MODEL_ID = "GSAI-ML/LLaDA-1.5"
BASE_MODEL_REVISION = "84346fd91ba60252d260022201ad6fc5a3468fb2"
TOKENIZER_ORIGIN_ID = "liuqi6777/DiffuRank_Pointwise"
TOKENIZER_ORIGIN_REVISION = "d8298bdc049c5531ece2eeb936b3c6c2577d36c3"
FINAL_ADAPTER_SHA256 = "1108f9b5d0a287541b3440affd9080f8f76ef6c5fe536522a9645876af541d49"
FINAL_TRAIN_RUN_ID = "20260821T022022Z"
POINTWISE_PROMPT = """Given a query and a document, determine whether the document is relevant to the query. The relevance score should be either 0 (not relevant) or 1 (relevant).

Query: {query}

Document: {document}"""


class ContractError(ValueError):
    """An input or immutable-artifact contract violation."""


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    text: str


@dataclass(frozen=True)
class RerankRequest:
    release_id: str
    candidate_scope_sha256: str
    query: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class RuntimeSettings:
    release_id: str
    base_model_path: Path
    adapter_path: Path
    source_path: Path
    device: str
    rope_scaling_factor: float
    max_candidates: int
    max_input_tokens: int
    max_text_chars: int
    internal_token_file: Path | None = None

    @classmethod
    def from_env(cls, *, require_internal_token: bool) -> "RuntimeSettings":
        def required(name: str) -> str:
            value = str(os.environ.get(name) or "").strip()
            if not value:
                raise RuntimeError(f"{name}_REQUIRED")
            return value

        token_file = str(os.environ.get("DIFFURANK_INTERNAL_TOKEN_FILE") or "").strip()
        if require_internal_token and not token_file:
            raise RuntimeError("DIFFURANK_INTERNAL_TOKEN_FILE_REQUIRED")
        return cls(
            release_id=required("DIFFURANK_RELEASE_ID"),
            base_model_path=Path(required("DIFFURANK_BASE_MODEL_PATH")),
            adapter_path=Path(required("DIFFURANK_ADAPTER_PATH")),
            source_path=Path(os.environ.get("DIFFURANK_SOURCE_PATH", "/opt/diffusionrank/src")),
            device=os.environ.get("DIFFURANK_DEVICE", "cuda").strip().lower(),
            # 4.0 is the value in the pinned pointwise LoRA training config.
            # It remains a recorded shadow parameter until a KOSIS eval receipt
            # establishes it as the active reranking setting.
            rope_scaling_factor=float(os.environ.get("DIFFURANK_ROPE_SCALING_FACTOR", "4.0")),
            max_candidates=int(os.environ.get("DIFFURANK_MAX_CANDIDATES", "100")),
            max_input_tokens=int(os.environ.get("DIFFURANK_MAX_INPUT_TOKENS", "1024")),
            max_text_chars=int(os.environ.get("DIFFURANK_MAX_TEXT_CHARS", "12000")),
            internal_token_file=(Path(token_file) if token_file else None),
        )

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value["base_model_path"] = str(self.base_model_path)
        value["adapter_path"] = str(self.adapter_path)
        value["source_path"] = str(self.source_path)
        value["internal_token_file"] = bool(self.internal_token_file)
        return value


@dataclass
class LoadedRuntime:
    settings: RuntimeSettings
    torch: Any
    model: Any
    tokenizer: Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_candidate_scope_sha256(candidates: Sequence[Candidate]) -> str:
    """Bind a request to exactly its ordered candidate IDs and text bytes."""
    canonical = [
        {"candidate_id": candidate.candidate_id, "text_sha256": hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()}
        for candidate in candidates
    ]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_string(value: Any, field: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field}_STRING_REQUIRED")
    text = value.strip()
    if not text:
        raise ContractError(f"{field}_REQUIRED")
    if maximum is not None and len(text) > maximum:
        raise ContractError(f"{field}_TOO_LONG")
    return text


def parse_rerank_request(body: Mapping[str, Any], settings: RuntimeSettings) -> RerankRequest:
    release_id = _require_string(body.get("release_id"), "release_id", maximum=256)
    if release_id != settings.release_id:
        raise ContractError("RELEASE_ID_MISMATCH")
    scope_hash = _require_string(body.get("candidate_scope_sha256"), "candidate_scope_sha256", maximum=64).lower()
    if len(scope_hash) != 64 or any(char not in "0123456789abcdef" for char in scope_hash):
        raise ContractError("CANDIDATE_SCOPE_SHA256_INVALID")
    query = _require_string(body.get("query"), "query", maximum=settings.max_text_chars)
    raw_candidates = body.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates or len(raw_candidates) > settings.max_candidates:
        raise ContractError("CANDIDATES_1_TO_MAX_REQUIRED")
    candidates: list[Candidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise ContractError("CANDIDATE_OBJECT_REQUIRED")
        candidates.append(
            Candidate(
                candidate_id=_require_string(raw.get("candidate_id"), "candidate_id", maximum=512),
                text=_require_string(raw.get("text"), "candidate_text", maximum=settings.max_text_chars),
            )
        )
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ContractError("CANDIDATE_ID_DUPLICATE")
    request = RerankRequest(
        release_id=release_id,
        candidate_scope_sha256=scope_hash,
        query=query,
        candidates=tuple(candidates),
    )
    if not hmac.compare_digest(request.candidate_scope_sha256, canonical_candidate_scope_sha256(request.candidates)):
        raise ContractError("CANDIDATE_SCOPE_SHA256_MISMATCH")
    return request


def _load_internal_token(settings: RuntimeSettings) -> str:
    path = settings.internal_token_file
    if path is None or not path.is_file():
        raise RuntimeError("DIFFURANK_INTERNAL_TOKEN_FILE_MISSING")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("DIFFURANK_INTERNAL_TOKEN_TOO_SHORT")
    return token


def load_runtime(settings: RuntimeSettings) -> LoadedRuntime:
    """Load only the pinned base + local final LoRA; CPU fallback is forbidden."""
    if settings.device != "cuda":
        raise RuntimeError("DIFFURANK_CUDA_REQUIRED")
    adapter_weights = settings.adapter_path / "adapter_model.safetensors"
    if not adapter_weights.is_file():
        raise RuntimeError("DIFFURANK_ADAPTER_MODEL_MISSING")
    if sha256_file(adapter_weights) != FINAL_ADAPTER_SHA256:
        raise RuntimeError("DIFFURANK_ADAPTER_SHA256_MISMATCH")
    if not (settings.base_model_path / "config.json").is_file():
        raise RuntimeError("DIFFURANK_BASE_MODEL_MISSING")
    if not settings.source_path.is_dir():
        raise RuntimeError("DIFFURANK_SOURCE_MISSING")
    source_text = str(settings.source_path)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)

    import torch
    from peft import PeftModel
    from transformers import AutoConfig, AutoTokenizer
    from model.modeling_llada import LLaDAModelLM

    if not torch.cuda.is_available():
        raise RuntimeError("DIFFURANK_CUDA_UNAVAILABLE")
    config = AutoConfig.from_pretrained(
        str(settings.base_model_path), local_files_only=True, trust_remote_code=True
    )
    config.rope_theta = config.rope_theta * settings.rope_scaling_factor
    base_model = LLaDAModelLM.from_pretrained(
        str(settings.base_model_path),
        config=config,
        torch_dtype=torch.bfloat16,
        device_map="cuda",
        low_cpu_mem_usage=True,
        local_files_only=True,
    )
    model = PeftModel.from_pretrained(base_model, str(settings.adapter_path), is_trainable=False)
    model.eval()
    tokenizer = AutoTokenizer.from_pretrained(
        str(settings.adapter_path), local_files_only=True, trust_remote_code=True
    )
    return LoadedRuntime(settings=settings, torch=torch, model=model, tokenizer=tokenizer)


def _score_one(runtime: LoadedRuntime, *, query: str, document: str) -> float:
    prompt = POINTWISE_PROMPT.format(query=query, document=document)
    messages = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "<|mdm_mask|>"},
    ]
    input_ids = runtime.tokenizer.apply_chat_template(
        messages, add_generation_prompt=False, return_tensors="pt"
    ).to(runtime.settings.device)
    if input_ids.shape[1] > runtime.settings.max_input_tokens:
        raise ContractError("CANDIDATE_INPUT_TOO_LONG")
    with runtime.torch.inference_mode():
        logits = runtime.model(input_ids).logits
    mask_positions = (input_ids == 126336).nonzero(as_tuple=True)
    if mask_positions[0].numel() != 1:
        raise RuntimeError("DIFFURANK_MASK_POSITION_INVALID")
    masked_logits = logits[mask_positions[0], mask_positions[1], :]
    yes_loc = runtime.tokenizer.encode("1")[0]
    no_loc = runtime.tokenizer.encode("0")[0]
    probabilities = runtime.torch.softmax(masked_logits, dim=-1)[0]
    p_yes = float(probabilities[yes_loc].item())
    p_no = float(probabilities[no_loc].item())
    denominator = p_yes + p_no
    if not math.isfinite(p_yes) or not math.isfinite(p_no) or denominator <= 0:
        raise RuntimeError("DIFFURANK_NONFINITE_SCORE")
    return p_yes / denominator


def rerank(runtime: LoadedRuntime, request: RerankRequest) -> list[dict[str, Any]]:
    rows = [
        {"candidate_id": candidate.candidate_id, "score": _score_one(runtime, query=request.query, document=candidate.text)}
        for candidate in request.candidates
    ]
    rows.sort(key=lambda row: (-row["score"], row["candidate_id"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def response_fingerprint(results: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(list(results), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_identity(runtime: LoadedRuntime) -> dict[str, Any]:
    return {
        "contract": SERVICE_CONTRACT,
        "status": "READY",
        "authority": "CANDIDATE_ORDERING_ONLY",
        "diffusionrank_repository": DIFFUSIONRANK_REPOSITORY,
        "diffusionrank_commit": DIFFUSIONRANK_COMMIT,
        "base_model_id": BASE_MODEL_ID,
        "base_model_revision": BASE_MODEL_REVISION,
        "tokenizer_origin_id": TOKENIZER_ORIGIN_ID,
        "tokenizer_origin_revision": TOKENIZER_ORIGIN_REVISION,
        "final_adapter_sha256": FINAL_ADAPTER_SHA256,
        "final_train_run_id": FINAL_TRAIN_RUN_ID,
        "settings": runtime.settings.public(),
        "torch": runtime.torch.__version__,
        "cuda": runtime.torch.version.cuda,
        "gpu": runtime.torch.cuda.get_device_name(0),
    }


def create_app():
    """Create the internal HTTP service after all immutable artifacts validate."""
    from fastapi import Body, FastAPI, Header, HTTPException

    settings = RuntimeSettings.from_env(require_internal_token=True)
    token = _load_internal_token(settings)
    runtime = load_runtime(settings)
    app = FastAPI(title=SERVICE_CONTRACT, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return runtime_identity(runtime)

    @app.post("/rerank")
    def rerank_endpoint(
        body: dict[str, Any] = Body(...),
        x_diffurank_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not x_diffurank_token or not hmac.compare_digest(x_diffurank_token, token):
            raise HTTPException(status_code=401, detail="INTERNAL_TOKEN_REQUIRED")
        try:
            request = parse_rerank_request(body, settings)
            results = rerank(runtime, request)
        except ContractError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            **runtime_identity(runtime),
            "release_id": request.release_id,
            "candidate_scope_sha256": request.candidate_scope_sha256,
            "results_sha256": response_fingerprint(results),
            "results": results,
        }

    return app


__all__ = [
    "BASE_MODEL_ID", "BASE_MODEL_REVISION", "Candidate", "ContractError", "DIFFUSIONRANK_COMMIT",
    "FINAL_ADAPTER_SHA256", "RerankRequest", "RuntimeSettings", "SERVICE_CONTRACT",
    "canonical_candidate_scope_sha256", "create_app", "load_runtime", "parse_rerank_request",
    "rerank", "response_fingerprint", "runtime_identity", "sha256_file",
]
