"""Create a deterministic release receipt only for one clean source SHA."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


CONTRACT_VERSION = "ec2-single-sha-release-manifest-v1"
RUNTIME_MANIFEST_PATH = Path("deploy/pipeline_runtime/manifest.json")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseManifestError(ValueError):
    """Raised when a release receipt cannot be bound to committed source."""


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _tracked_paths(root: Path) -> list[Path]:
    output = _git(root, "ls-files", "-z")
    return sorted((Path(raw) for raw in output.split("\0") if raw), key=lambda path: path.as_posix())


def _file_records(root: Path, paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in paths:
        absolute = root / relative
        if not absolute.is_file():
            raise ReleaseManifestError(f"TRACKED_FILE_MISSING:{relative.as_posix()}")
        data = absolute.read_bytes()
        records.append({"path": relative.as_posix(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return records


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _head_sha(root: Path) -> str:
    value = _git(root, "rev-parse", "HEAD").lower()
    if not _SHA40.fullmatch(value):
        raise ReleaseManifestError("GIT_HEAD_INVALID")
    return value


def _assert_clean_head(root: Path, app_release_sha: str) -> str:
    requested = str(app_release_sha).lower()
    head = _head_sha(root)
    if not _SHA40.fullmatch(requested) or requested != head:
        raise ReleaseManifestError("APP_RELEASE_SHA_NOT_HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status:
        if any(line.startswith("?? ") for line in status.splitlines()):
            raise ReleaseManifestError("UNTRACKED_WORKTREE_CONTENT")
        raise ReleaseManifestError("TRACKED_WORKTREE_DIRTY")
    for relative in _tracked_paths(root):
        worktree = root / relative
        head_blob = subprocess.run(
            ["git", "show", f"HEAD:{relative.as_posix()}"], cwd=root, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
        if not worktree.is_file() or worktree.read_bytes() != head_blob:
            raise ReleaseManifestError(f"TRACKED_BYTES_NOT_HEAD:{relative.as_posix()}")
    return head


def _load_runtime_manifest(root: Path) -> dict[str, Any]:
    try:
        loaded = json.loads((root / RUNTIME_MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("RUNTIME_MANIFEST_INVALID") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("files"), list):
        raise ReleaseManifestError("RUNTIME_MANIFEST_INVALID")
    return loaded


def verify_runtime_closure(root: str | Path) -> dict[str, Any]:
    """Verify all sealed runtime paths against current tracked materialized bytes."""
    root_path = Path(root).resolve()
    manifest = _load_runtime_manifest(root_path)
    records = manifest["files"]
    if manifest.get("runtime_module_files") != 74 or len(records) != 74:
        raise ReleaseManifestError("RUNTIME_CLOSURE_EXPECTED_74")
    tracked = {path.as_posix() for path in _tracked_paths(root_path)}
    mismatches: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ReleaseManifestError("RUNTIME_MANIFEST_RECORD_INVALID")
        relative = str(record.get("path") or "")
        if not relative or relative in seen:
            raise ReleaseManifestError("RUNTIME_MANIFEST_PATH_INVALID")
        seen.add(relative)
        runtime_root = (root_path / "deploy" / "pipeline_runtime" / "src").resolve()
        absolute = (runtime_root / relative).resolve()
        if runtime_root not in absolute.parents:
            raise ReleaseManifestError("RUNTIME_MANIFEST_PATH_INVALID")
        repo_relative = absolute.relative_to(root_path).as_posix()
        if repo_relative not in tracked or not absolute.is_file():
            mismatches.append({"path": relative, "reason": "MISSING_OR_UNTRACKED"})
            continue
        if absolute.stat().st_size != record.get("size") or _sha256_bytes(absolute) != record.get("sha256"):
            mismatches.append({"path": relative, "reason": "SIZE_OR_SHA_MISMATCH"})
    return {
        "valid": not mismatches,
        "expected": 74,
        "matched": 74 - len(mismatches),
        "mismatches": mismatches,
        "manifest_sha256": _sha256_bytes(root_path / RUNTIME_MANIFEST_PATH),
    }


def refresh_runtime_manifest(root: str | Path) -> dict[str, Any]:
    """Refresh only size/hash values of the fixed 74-file runtime allowlist."""
    root_path = Path(root).resolve()
    manifest = _load_runtime_manifest(root_path)
    records = manifest["files"]
    if manifest.get("runtime_module_files") != 74 or len(records) != 74:
        raise ReleaseManifestError("RUNTIME_CLOSURE_EXPECTED_74")
    refreshed: list[dict[str, Any]] = []
    for record in records:
        relative = str(record.get("path") or "")
        runtime_root = (root_path / "deploy" / "pipeline_runtime" / "src").resolve()
        absolute = (runtime_root / relative).resolve()
        if runtime_root not in absolute.parents:
            raise ReleaseManifestError("RUNTIME_MANIFEST_PATH_INVALID")
        if not relative or not absolute.is_file():
            raise ReleaseManifestError("RUNTIME_MANIFEST_PATH_INVALID")
        refreshed.append({"path": relative, "size": absolute.stat().st_size, "sha256": _sha256_bytes(absolute)})
    manifest["files"] = refreshed
    destination = root_path / RUNTIME_MANIFEST_PATH
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verified = verify_runtime_closure(root_path)
    if not verified["valid"]:
        raise ReleaseManifestError("RUNTIME_CLOSURE_REFRESH_FAILED")
    return verified


def _validate_deployment_inputs(root: Path, image_digests: dict[str, str] | None, compose_paths: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    digests = {str(name): str(value) for name, value in (image_digests or {}).items()}
    if set(digests) != {"api", "nginx"} or any(not _SHA256_DIGEST.fullmatch(value) for value in digests.values()):
        raise ReleaseManifestError("REQUIRED_IMAGE_DIGESTS_INVALID")
    paths = sorted({str(path).replace("\\", "/") for path in compose_paths})
    if "deploy/compose.yaml" not in paths or any(not (root / path).is_file() for path in paths):
        raise ReleaseManifestError("COMPOSE_PATHS_INVALID")
    return dict(sorted(digests.items())), paths


def build_manifest(root: str | Path, *, app_release_sha: str, image_digests: dict[str, str] | None = None, compose_paths: Iterable[str] = ()) -> dict[str, Any]:
    root_path = Path(root).resolve()
    head = _assert_clean_head(root_path, app_release_sha)
    closure = verify_runtime_closure(root_path)
    if not closure["valid"] or closure["matched"] != closure["expected"]:
        raise ReleaseManifestError("RUNTIME_CLOSURE_INCOMPLETE")
    digests, paths = _validate_deployment_inputs(root_path, image_digests, compose_paths)
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "app_release_sha": head,
        "files": _file_records(root_path, _tracked_paths(root_path)),
        "image_digests": digests,
        "compose_paths": paths,
        "runtime_closure": closure,
    }
    return {**payload, "manifest_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest()}


def _assert_output_outside_root(root: Path, output: Path) -> Path:
    """Keep the generated receipt out of the Docker build context."""
    resolved = output.resolve()
    if resolved == root or root in resolved.parents:
        raise ReleaseManifestError("MANIFEST_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, help="required release receipt path outside the repository")
    parser.add_argument("--app-release-sha")
    parser.add_argument("--image-digest", action="append", default=[], metavar="NAME=DIGEST")
    parser.add_argument("--compose-path", action="append", default=[])
    parser.add_argument("--refresh-runtime-closure", action="store_true")
    args = parser.parse_args(argv)
    if args.refresh_runtime_closure:
        print(json.dumps(refresh_runtime_manifest(args.root), ensure_ascii=False, sort_keys=True))
        return 0
    if args.output is None or args.app_release_sha is None:
        parser.error("--output and --app-release-sha are required unless refreshing closure")
    image_digests: dict[str, str] = {}
    for item in args.image_digest:
        name, separator, digest = item.partition("=")
        if not separator or not name or not digest:
            parser.error("--image-digest must use NAME=DIGEST")
        image_digests[name] = digest
    root = args.root.resolve()
    output = _assert_output_outside_root(root, args.output)
    manifest = build_manifest(root, app_release_sha=args.app_release_sha, image_digests=image_digests, compose_paths=args.compose_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
