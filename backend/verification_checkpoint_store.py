"""Short-lived, opaque checkpoints for resumable article verification.

The checkpoint deliberately keeps the immutable article/trace artifacts apart
from the mutable clarification context.  It is process-local by default; the
API container may lose it on restart, which is an explicit development
environment limitation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
from typing import Any, Mapping


TTL_SECONDS = 15 * 60
MAX_CHECKPOINTS = 32
_TRACE_STAGE_FILES = {
    "01": ({"01_sentences.jsonl", "01_value_candidates.jsonl"}, {"01_trace.log"}),
    "02": ({"02_l2_predictions.jsonl", "02_l2_results.jsonl"}, {"02_trace.log"}),
    "03": ({"03_routed.jsonl"}, {"03_trace.log"}),
}


class CheckpointError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Checkpoint:
    token: str
    root: Path
    article_path: Path
    output_root: Path
    context_path: Path
    metadata: dict[str, Any]


def _root() -> Path:
    configured = os.getenv("PIPELINE_CHECKPOINT_ROOT", "").strip()
    path = Path(configured) if configured else Path(tempfile.gettempdir()) / "kosis_verify_checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _bytes_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_record(path: Path, record: Mapping[str, Any]) -> None:
    if not path.is_file():
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID")
    data = path.read_bytes()
    if record.get("sha256") != _bytes_sha(data) or record.get("bytes") != len(data):
        raise CheckpointError("RESUME_CHECKPOINT_PAYLOAD_MISMATCH")
    if "rows" in record:
        try:
            rows = sum(1 for line in data.decode("utf-8").splitlines() if line.strip())
        except UnicodeDecodeError as exc:
            raise CheckpointError("RESUME_CHECKPOINT_PAYLOAD_MISMATCH") from exc
        if record.get("rows") != rows:
            raise CheckpointError("RESUME_CHECKPOINT_PAYLOAD_MISMATCH")


def _article_input_metadata(path: Path) -> tuple[list[str], dict[str, str], str]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError("RESUME_CHECKPOINT_ARTIFACT_INVALID") from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise CheckpointError("RESUME_CHECKPOINT_ARTIFACT_INVALID")
    ids = [str(row.get("article_idx") or "") for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise CheckpointError("RESUME_CHECKPOINT_ARTIFACT_INVALID")
    bodies = {
        article_id: _bytes_sha(str(row.get("article_text") or "").encode("utf-8"))
        for article_id, row in zip(ids, rows)
    }
    return ids, bodies, _bytes_sha(path.read_bytes())


def _manifest_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_stage_manifest(path: Path, stage: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID") from exc
    if (
        not isinstance(value, dict)
        or value.get("status") != "COMPLETE"
        or value.get("stage") != stage
        or not isinstance(value.get("contract_version"), str)
    ):
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID")
    data = value.get("data_payloads")
    logs = value.get("sealed_logs")
    runtime = value.get("runtime_payloads")
    if (
        not isinstance(data, dict)
        or not isinstance(logs, dict)
        or not isinstance(runtime, dict)
        or set(data) != _TRACE_STAGE_FILES[stage][0]
        or set(logs) != _TRACE_STAGE_FILES[stage][1]
        or runtime
    ):
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID")
    for record in list(data.values()) + list(logs.values()):
        if not isinstance(record, dict):
            raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID")
    article_input = value.get("article_input")
    if (
        not isinstance(article_input, dict)
        or not isinstance(article_input.get("path"), str)
        or not article_input.get("path")
        or not isinstance(article_input.get("sha256"), str)
    ):
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID")
    if not isinstance(value.get("ordered_article_ids"), list) or not isinstance(value.get("article_body_sha256"), dict):
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID")
    return value


def _validate_stage_manifest(
    path: Path, manifest: Mapping[str, Any], stage: str, article_path: Path,
    article_ids: list[str], body_sha: dict[str, str], input_sha: str,
) -> None:
    article_input = manifest.get("article_input")
    if (
        not isinstance(article_input, Mapping)
        or article_input.get("path") != str(article_path.resolve())
        or article_input.get("sha256") != input_sha
        or manifest.get("ordered_article_ids") != article_ids
        or manifest.get("article_body_sha256") != body_sha
    ):
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_INVALID")
    root = path.parent
    data = manifest["data_payloads"]
    logs = manifest["sealed_logs"]
    for name, record in list(data.items()) + list(logs.items()):
        _file_record(root / name, record)


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(_manifest_bytes(manifest))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _reseal_trace_manifests(root: Path) -> None:
    """Rebind copied 01-03 manifests without changing immutable payloads."""
    output = root / "out"
    article_path = root / "articles.jsonl"
    stages = [stage for stage in ("01", "02", "03") if (output / f"{stage}_manifest.json").is_file()]
    if not stages:
        return
    if stages != [str(index).zfill(2) for index in range(1, int(stages[-1]) + 1)]:
        raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_CHAIN_INVALID")
    article_ids, body_sha, input_sha = _article_input_metadata(article_path)
    manifests: dict[str, dict[str, Any]] = {}
    original_sha: dict[str, str] = {}
    for stage in stages:
        path = output / f"{stage}_manifest.json"
        manifest = _load_stage_manifest(path, stage)
        _validate_stage_manifest(path, manifest, stage, Path(str(manifest["article_input"]["path"])), article_ids, body_sha, input_sha)
        manifests[stage] = manifest
        original_sha[stage] = _bytes_sha(path.read_bytes())
    for index, stage in enumerate(stages):
        expected_previous = None if index == 0 else original_sha[stages[index - 1]]
        if manifests[stage].get("predecessor_manifest_sha256") != expected_previous:
            raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_CHAIN_INVALID")
    for index, stage in enumerate(stages):
        manifest = dict(manifests[stage])
        manifest["article_input"] = dict(manifest["article_input"])
        manifest["article_input"]["path"] = str(article_path.resolve())
        manifest["predecessor_manifest_sha256"] = None if index == 0 else _bytes_sha(
            _manifest_bytes(manifests[stages[index - 1]])
        )
        # The previous manifest's new article path must be included before its
        # SHA is used by the next stage.
        if index:
            previous = dict(manifests[stages[index - 1]])
            previous["article_input"] = dict(previous["article_input"])
            previous["article_input"]["path"] = str(article_path.resolve())
            manifests[stages[index - 1]] = previous
            manifest["predecessor_manifest_sha256"] = _bytes_sha(_manifest_bytes(previous))
        manifests[stage] = manifest
        _write_manifest(output / f"{stage}_manifest.json", manifest)
    rebound: dict[str, dict[str, Any]] = {}
    rebound_sha: dict[str, str] = {}
    for stage in stages:
        path = output / f"{stage}_manifest.json"
        manifest = _load_stage_manifest(path, stage)
        _validate_stage_manifest(path, manifest, stage, article_path, article_ids, body_sha, input_sha)
        rebound[stage] = manifest
        rebound_sha[stage] = _bytes_sha(path.read_bytes())
    for index, stage in enumerate(stages):
        expected_previous = None if index == 0 else rebound_sha[stages[index - 1]]
        if rebound[stage].get("predecessor_manifest_sha256") != expected_previous:
            raise CheckpointError("RESUME_CHECKPOINT_MANIFEST_CHAIN_INVALID")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def cleanup() -> None:
    root = _root()
    entries: list[tuple[float, Path]] = []
    now = time.time()
    for path in root.iterdir():
        if not path.is_dir():
            continue
        metadata = path / "checkpoint.json"
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
            expires = float(value.get("expires_at_epoch") or 0)
            created = float(value.get("created_at_epoch") or path.stat().st_mtime)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            _remove(path)
            continue
        if expires <= now or value.get("status") in {"CONSUMED", "CANCELLED"}:
            _remove(path)
            continue
        entries.append((created, path))
    for _, path in sorted(entries)[:-MAX_CHECKPOINTS]:
        _remove(path)


def _read(token: str) -> tuple[Path, dict[str, Any]]:
    if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
        raise CheckpointError("RESUME_CHECKPOINT_INVALID")
    path = _root() / _token_digest(token)
    metadata_path = path / "checkpoint.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise CheckpointError("RESUME_CHECKPOINT_NOT_FOUND") from None
    if metadata.get("status") != "ACTIVE" or float(metadata.get("expires_at_epoch") or 0) <= time.time():
        _remove(path)
        raise CheckpointError("RESUME_CHECKPOINT_EXPIRED")
    return path, metadata


def create(
    *,
    workdir: Path,
    article_body_sha256: str,
    title: str,
    article_id: str,
    clarification_history: list[Mapping[str, Any]],
    runtime_fingerprint: str,
    config_sha256: str,
    resume_from_stage: str,
) -> Checkpoint:
    cleanup()
    token = secrets.token_urlsafe(32)
    root = _root() / _token_digest(token)
    root.mkdir(parents=True, exist_ok=False)
    try:
        source_input = workdir / "articles.jsonl"
        source_output = workdir / "out"
        if not source_input.is_file() or not source_output.is_dir():
            raise CheckpointError("RESUME_CHECKPOINT_ARTIFACT_MISSING")
        shutil.copy2(source_input, root / "articles.jsonl")
        shutil.copytree(source_output, root / "out")
        _reseal_trace_manifests(root)
        context = {
            "contract_version": "clarification-context-v1",
            "article_id": article_id,
            "article_body_sha256": article_body_sha256,
            "clarification_answers": [dict(item) for item in clarification_history],
        }
        context_path = root / "clarification_context.json"
        context_path.write_text(json.dumps(context, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        now = _now()
        metadata = {
            "contract_version": "verification-checkpoint-v1",
            "status": "ACTIVE",
            "article_id": article_id,
            "title": title,
            "article_body_sha256": article_body_sha256,
            "clarification_history_sha256": _json_sha(clarification_history),
            "runtime_fingerprint": runtime_fingerprint,
            "config_sha256": config_sha256,
            "resume_from_stage": resume_from_stage,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=TTL_SECONDS)).isoformat(),
            "created_at_epoch": now.timestamp(),
            "expires_at_epoch": (now + timedelta(seconds=TTL_SECONDS)).timestamp(),
        }
        (root / "checkpoint.json").write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        return Checkpoint(token, root, root / "articles.jsonl", root / "out", context_path, metadata)
    except Exception:
        _remove(root)
        raise


def consume(
    token: str,
    *,
    article_body_sha256: str,
    title: str,
    clarification_history: list[Mapping[str, Any]],
    runtime_fingerprint: str,
    config_sha256: str,
) -> Checkpoint:
    cleanup()
    root, metadata = _read(token)
    if metadata.get("article_body_sha256") != article_body_sha256:
        raise CheckpointError("RESUME_CHECKPOINT_FINGERPRINT_MISMATCH")
    if metadata.get("runtime_fingerprint") != runtime_fingerprint or metadata.get("config_sha256") != config_sha256:
        raise CheckpointError("RESUME_CHECKPOINT_FINGERPRINT_MISMATCH")
    if str(metadata.get("title") or "") != title:
        raise CheckpointError("RESUME_CHECKPOINT_ARTICLE_MISMATCH")
    prior = json.loads((root / "clarification_context.json").read_text(encoding="utf-8"))
    prior_answers = prior.get("clarification_answers") if isinstance(prior, dict) else None
    if not isinstance(prior_answers, list) or clarification_history[: len(prior_answers)] != prior_answers:
        raise CheckpointError("RESUME_CHECKPOINT_HISTORY_MISMATCH")
    metadata["status"] = "CONSUMED"
    (root / "checkpoint.json").write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return Checkpoint(token, root, root / "articles.jsonl", root / "out", root / "clarification_context.json", metadata)


def update_context(checkpoint: Checkpoint, clarification_history: list[Mapping[str, Any]]) -> None:
    context = json.loads(checkpoint.context_path.read_text(encoding="utf-8"))
    context["clarification_answers"] = [dict(item) for item in clarification_history]
    checkpoint.context_path.write_text(
        json.dumps(context, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


def discard(checkpoint: Checkpoint) -> None:
    _remove(checkpoint.root)


__all__ = ["Checkpoint", "CheckpointError", "TTL_SECONDS", "MAX_CHECKPOINTS", "cleanup", "consume", "create", "discard", "update_context"]
