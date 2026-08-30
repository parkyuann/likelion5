from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from deploy import release_manifest


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def _clean_repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "release-test")
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_bytes(b"immutable source\n")
    _git(tmp_path, "add", "deploy/compose.yaml", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "sealed source")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _closure() -> dict[str, object]:
    return {"valid": True, "expected": 74, "matched": 74, "mismatches": [], "manifest_sha256": "a" * 64}


def _digests() -> dict[str, str]:
    return {"api": "sha256:" + "a" * 64, "nginx": "sha256:" + "b" * 64}


def test_release_manifest_is_deterministic_and_bound_to_clean_head(monkeypatch, tmp_path: Path) -> None:
    root, head = _clean_repo(tmp_path)
    monkeypatch.setattr(release_manifest, "verify_runtime_closure", lambda _root: _closure())

    first = release_manifest.build_manifest(
        root, app_release_sha=head, image_digests=_digests(), compose_paths=["deploy\\compose.yaml"],
    )
    second = release_manifest.build_manifest(
        root, app_release_sha=head, image_digests=_digests(), compose_paths=["deploy/compose.yaml"],
    )

    assert first == second
    assert first["app_release_sha"] == head
    assert first["compose_paths"] == ["deploy/compose.yaml"]
    assert first["runtime_closure"]["matched"] == 74
    assert [row["path"] for row in first["files"]] == ["deploy/compose.yaml", "tracked.txt"]
    for row in first["files"]:
        data = (root / row["path"]).read_bytes()
        assert row["size"] == len(data)
        assert row["sha256"] == hashlib.sha256(data).hexdigest()
    unsigned = {key: value for key, value in first.items() if key != "manifest_sha256"}
    assert first["manifest_sha256"] == hashlib.sha256(release_manifest._canonical_bytes(unsigned)).hexdigest()


def test_release_manifest_rejects_non_head_dirty_or_incomplete_inputs(monkeypatch, tmp_path: Path) -> None:
    root, head = _clean_repo(tmp_path)
    monkeypatch.setattr(release_manifest, "verify_runtime_closure", lambda _root: _closure())
    with pytest.raises(release_manifest.ReleaseManifestError, match="APP_RELEASE_SHA_NOT_HEAD"):
        release_manifest.build_manifest(root, app_release_sha="a" * 40, image_digests=_digests(), compose_paths=["deploy/compose.yaml"])

    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(release_manifest.ReleaseManifestError, match="TRACKED_WORKTREE_DIRTY"):
        release_manifest.build_manifest(root, app_release_sha=head, image_digests=_digests(), compose_paths=["deploy/compose.yaml"])

    _git(root, "checkout", "--", "tracked.txt")
    (root / "untracked-build-input.py").write_text("print('not sealed')\n", encoding="utf-8")
    with pytest.raises(release_manifest.ReleaseManifestError, match="UNTRACKED_WORKTREE_CONTENT"):
        release_manifest.build_manifest(root, app_release_sha=head, image_digests=_digests(), compose_paths=["deploy/compose.yaml"])

    (root / "untracked-build-input.py").unlink()
    with pytest.raises(release_manifest.ReleaseManifestError, match="REQUIRED_IMAGE_DIGESTS_INVALID"):
        release_manifest.build_manifest(root, app_release_sha=head, image_digests={"api": "sha256:" + "a" * 64}, compose_paths=["deploy/compose.yaml"])
