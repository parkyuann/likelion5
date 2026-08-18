"""배포 환경의 HCX 인증값 조회 보조 모듈."""

from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Load a local .env without logging its contents or overwriting exports."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and value:
            os.environ.setdefault(key, value)


def env_api_key() -> str:
    """Return an explicitly exported HCX key, optionally loading local .env."""
    _load_env_file(Path.cwd() / ".env")
    _load_env_file(Path(__file__).resolve().parent / ".env")
    return (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()
