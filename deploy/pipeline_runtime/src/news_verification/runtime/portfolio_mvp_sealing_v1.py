"""Small, strict sealing primitives used by the portfolio MVP v3 paths.

The public helpers in this module deliberately operate on bytes and on a
single explicitly supplied tree.  They do not infer a root from the current
working directory and they never follow links while walking a tree.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping, Sequence
import uuid


REPARSE_POINT_ATTRIBUTE = 0x400


class SealingError(RuntimeError):
    """A contract violation in an input tree or publication operation."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _stat_is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SealingError(f"TREE_STAT_FAILED:{path}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)


def _relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SealingError("SEAL_PATH_ESCAPE") from exc
    text = relative.as_posix()
    if not text or text == "." or text.startswith("../") or text == "..":
        raise SealingError("SEAL_PATH_INVALID")
    return text


def _assert_inside(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise SealingError("SEAL_PATH_ESCAPE") from exc


def _walk_tree(root: Path) -> list[dict[str, Any]]:
    root = Path(root).resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise SealingError("TREE_ROOT_NOT_DIRECTORY")
    if _stat_is_reparse(root):
        raise SealingError("TREE_ROOT_LINK_OR_REPARSE")

    entries: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SealingError(f"TREE_SCAN_FAILED:{directory}") from exc
        for child in children:
            path = Path(child.path)
            _assert_inside(path, root)
            if _stat_is_reparse(path):
                raise SealingError(f"TREE_LINK_OR_REPARSE:{_relative_path(path, root)}")
            try:
                is_directory = child.is_dir(follow_symlinks=False)
                is_file = child.is_file(follow_symlinks=False)
            except OSError as exc:
                raise SealingError(f"TREE_ENTRY_STAT_FAILED:{_relative_path(path, root)}") from exc
            relative = _relative_path(path, root)
            if is_directory:
                entries.append({"kind": "directory", "path": relative})
                visit(path)
            elif is_file:
                try:
                    size = child.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    raise SealingError(f"TREE_ENTRY_STAT_FAILED:{relative}") from exc
                entries.append({
                    "kind": "file",
                    "path": relative,
                    "sha256": sha256_file(path),
                    "bytes": int(size),
                })
            else:
                raise SealingError(f"TREE_SPECIAL_ENTRY:{relative}")

    visit(root)
    return sorted(entries, key=lambda item: (str(item["path"]), str(item["kind"])))


def build_exact_tree_inventory(root: str | Path) -> list[dict[str, Any]]:
    """Return all regular files and directories below ``root``.

    Every path is root-relative and canonicalized with forward slashes.  A
    symlink, junction, reparse point, special file, or path escape fails closed.
    """

    return _walk_tree(Path(root))


def file_inventory(root: str | Path) -> list[dict[str, Any]]:
    """Return only file records from an exact tree inventory."""

    return [item for item in build_exact_tree_inventory(root) if item["kind"] == "file"]


def inventory_sha256(inventory: Sequence[Mapping[str, Any]]) -> str:
    normalized = [dict(item) for item in inventory]
    return canonical_sha256(normalized)


def _normalize_expected_inventory(
    expected: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in expected:
        if not isinstance(item, Mapping):
            raise SealingError("TREE_INVENTORY_INVALID")
        raw_path = str(item.get("path") or "").replace("\\", "/")
        path = Path(raw_path)
        if not raw_path or path.is_absolute() or ".." in path.parts or raw_path.startswith("/"):
            raise SealingError("SEAL_PATH_INVALID")
        kind = str(item.get("kind") or "file")
        if kind not in {"file", "directory"}:
            raise SealingError("TREE_INVENTORY_INVALID")
        key = (raw_path, kind)
        if key in seen:
            raise SealingError("TREE_INVENTORY_DUPLICATE")
        seen.add(key)
        if kind == "directory":
            if set(item) - {"path", "kind"}:
                raise SealingError("TREE_INVENTORY_INVALID")
            normalized.append({"kind": kind, "path": raw_path})
            continue
        sha = str(item.get("sha256") or "")
        if len(sha) != 64 or any(char not in "0123456789abcdefABCDEF" for char in sha):
            raise SealingError("TREE_INVENTORY_INVALID")
        try:
            size = int(item["bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SealingError("TREE_INVENTORY_INVALID") from exc
        if size < 0:
            raise SealingError("TREE_INVENTORY_INVALID")
        normalized.append({"kind": kind, "path": raw_path, "sha256": sha.lower(), "bytes": size})
    return sorted(normalized, key=lambda item: (str(item["path"]), str(item["kind"])))


def validate_exact_tree(
    root: str | Path,
    expected_inventory: Iterable[Mapping[str, Any]],
    *,
    expected_inventory_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Recompute and compare an exact tree, including empty directories."""

    expected = _normalize_expected_inventory(expected_inventory)
    if expected_inventory_sha256 and inventory_sha256(expected) != str(expected_inventory_sha256):
        raise SealingError("TREE_INVENTORY_SHA_MISMATCH")
    actual = build_exact_tree_inventory(root)
    if actual != expected:
        actual_paths = {(str(item["path"]), str(item["kind"])) for item in actual}
        expected_paths = {(str(item["path"]), str(item["kind"])) for item in expected}
        if actual_paths - expected_paths:
            raise SealingError("TREE_EXTRA_ENTRY")
        if expected_paths - actual_paths:
            raise SealingError("TREE_MISSING_ENTRY")
        raise SealingError("TREE_ENTRY_HASH_OR_SIZE_MISMATCH")
    return actual


def _fsync_parent(path: Path) -> None:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        handle = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def atomic_publish_bytes(destination: str | Path, data: bytes) -> Path:
    """Publish bytes with a sibling temp and a no-overwrite hard link.

    The final name is never opened for writing.  ``os.link`` is same-volume
    and atomic for the regular-file publication used by receipts/results.
    """

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise SealingError("PUBLISH_DESTINATION_EXISTS")
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise SealingError("PUBLISH_DESTINATION_EXISTS") from exc
        except OSError as exc:
            raise SealingError("PUBLISH_ATOMIC_LINK_UNAVAILABLE") from exc
        _fsync_parent(destination.parent)
        return destination
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_publish_json(destination: str | Path, value: Any) -> Path:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return atomic_publish_bytes(destination, data)


def atomic_publish_directory(source: str | Path, destination: str | Path) -> Path:
    """Atomically rename a complete sibling directory to a new name."""

    source = Path(source)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise SealingError("PUBLISH_DESTINATION_EXISTS")
    if not source.is_dir() or _stat_is_reparse(source):
        raise SealingError("PUBLISH_SOURCE_DIRECTORY_INVALID")
    if source.parent.resolve(strict=False) != destination.parent.resolve(strict=False):
        raise SealingError("PUBLISH_DIFFERENT_VOLUME")
    try:
        # On Windows os.rename is no-overwrite; the explicit existence check
        # plus the same-directory rename keeps the destination atomic.
        os.rename(source, destination)
    except FileExistsError as exc:
        raise SealingError("PUBLISH_DESTINATION_EXISTS") from exc
    except OSError as exc:
        raise SealingError("PUBLISH_DIRECTORY_RENAME_FAILED") from exc
    _fsync_parent(destination.parent)
    return destination


def ast_top_level_function_sha256(
    path: str | Path,
    function_name: str,
    *,
    through_next_top_level_function: bool = False,
) -> str:
    """Hash a normalized-LF top-level function source block.

    Ordinary blocks include the function's terminating newline.  The strict
    answer validator contract historically includes the blank lines before the
    next top-level function, so callers can request that exact slice.
    """

    raw = Path(path).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    text = raw.decode("utf-8")
    tree = ast.parse(text)
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    node = next((item for item in functions if item.name == function_name), None)
    if node is None:
        raise SealingError(f"FUNCTION_NOT_FOUND:{function_name}")
    lines = text.splitlines(keepends=True)
    end_line = node.end_lineno
    if through_next_top_level_function:
        next_lines = [item.lineno for item in functions if item.lineno > node.lineno]
        if next_lines:
            end_line = next_lines[0] - 1
    source = "".join(lines[node.lineno - 1:end_line])
    return sha256_bytes(source.encode("utf-8"))


__all__ = [
    "SealingError",
    "ast_top_level_function_sha256",
    "atomic_publish_bytes",
    "atomic_publish_directory",
    "atomic_publish_json",
    "build_exact_tree_inventory",
    "canonical_json_bytes",
    "canonical_sha256",
    "file_inventory",
    "inventory_sha256",
    "sha256_bytes",
    "sha256_file",
    "validate_exact_tree",
]


