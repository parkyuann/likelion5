"""Fail-closed environment projection for child runtime processes."""

from __future__ import annotations

import os
from typing import Mapping, Type


class ChildEnvironmentError(RuntimeError):
    """Raised when a projected child environment violates the contract."""


CHILD_ENV_ALLOWLIST = frozenset({
    "COMSPEC", "CUDA_VISIBLE_DEVICES", "CUBLAS_WORKSPACE_CONFIG", "NUMBER_OF_PROCESSORS", "PATH",
    "PATHEXT", "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "PROGRAMFILES", "SYSTEMDRIVE", "SYSTEMROOT",
    "TEMP", "TMP", "USERPROFILE", "WINDIR", "PYTHONIOENCODING", "PYTHONUTF8",
    "HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "SENTENCE_TRANSFORMERS_HOME",
    "LOCAL_ENCODER_MODEL_REVISION", "LOCAL_RERANKER_MODEL_REVISION", "LOCAL_ENCODER_SNAPSHOT", "LOCAL_RERANKER_SNAPSHOT",
})


def _forbidden_env_name(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in ("KOSIS", "HCX", "NCP", "API_KEY", "ACCESS_TOKEN", "SECRET", "PASSWORD", "OPENAI"))


def strict_child_environment(
    base: Mapping[str, str] | None = None,
    *,
    blocked_error: Type[RuntimeError] = ChildEnvironmentError,
) -> dict[str, str]:
    source = dict(os.environ if base is None else base)
    result = {key: str(value) for key, value in source.items() if key.upper() in CHILD_ENV_ALLOWLIST and not _forbidden_env_name(key)}
    result.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false",
        "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
    })
    if any(_forbidden_env_name(key) for key in result):
        raise blocked_error("CHILD_SECRET_ENV")
    return result


__all__ = ["CHILD_ENV_ALLOWLIST", "ChildEnvironmentError", "strict_child_environment"]
