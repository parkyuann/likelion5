from __future__ import annotations

import hashlib
from pathlib import Path

from backend import app


def test_release_version_returns_sha_manifest_sha_and_release_id_without_secrets(
    monkeypatch, tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"runtime manifest\n")
    monkeypatch.setenv("APP_RELEASE_SHA", "b" * 40)
    monkeypatch.setenv("KOSIS_RELEASE_ID", "release-test")
    monkeypatch.setenv("PIPELINE_RUNTIME_MANIFEST_PATH", str(manifest))
    monkeypatch.delenv("RUNTIME_MANIFEST_SHA256", raising=False)

    payload = app._release_version()

    assert payload == {
        "release_sha": "b" * 40,
        "runtime_manifest_sha256": hashlib.sha256(b"runtime manifest\n").hexdigest(),
        "release_id": "release-test",
    }
    assert "DATABASE_URL" not in payload
    assert "API_KEY" not in payload
