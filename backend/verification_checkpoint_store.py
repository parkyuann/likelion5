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
import base64
import unicodedata
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


def _profile_payload_sha(profile: Mapping[str, Any]) -> str:
    return _json_sha({
        key: value for key, value in profile.items()
        if key not in {"retrieved_at", "profile_sha256"}
    })


def validate_binding_continuation(
    checkpoint: Checkpoint,
    *,
    expected_release_id: str | None = None,
) -> dict[str, Any]:
    """Verify the sealed binding scope and bytes before any constraint is used."""
    path = checkpoint.root / "binding_continuation.json"
    records = checkpoint.metadata.get("artifact_records")
    record = records.get("binding_continuation.json") if isinstance(records, Mapping) else None
    if not path.is_file() or not isinstance(record, Mapping):
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    payload = path.read_bytes()
    if record.get("sha256") != _bytes_sha(payload) or record.get("bytes") != len(payload):
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    try:
        continuation = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED") from exc
    if not isinstance(continuation, Mapping) or continuation.get("contract_version") != "binding-continuation-v1":
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    target_ids = continuation.get("target_ids")
    if (
        not isinstance(target_ids, list)
        or not target_ids
        or len(target_ids) != len(set(str(value) for value in target_ids))
        or any(not isinstance(value, str) or not value.strip() for value in target_ids)
        or _json_sha(target_ids) != continuation.get("target_scope_sha256")
    ):
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    membership = continuation.get("candidate_membership")
    if (
        not isinstance(membership, list)
        or not membership
        or len(membership) != len(set(str(value) for value in membership))
        or any(not isinstance(value, str) or not value.strip() for value in membership)
        or _json_sha(membership) != continuation.get("candidate_membership_sha256")
    ):
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    raw_profiles = continuation.get("raw_profiles")
    projection_profiles = continuation.get("projection_profiles")
    if (
        not isinstance(raw_profiles, Mapping)
        or set(raw_profiles) != set(membership)
        or not isinstance(projection_profiles, Mapping)
        or _json_sha(projection_profiles) != continuation.get("projection_bundle_sha256")
    ):
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    release_id = str(continuation.get("release_id") or "")
    profile_receipt: list[dict[str, str]] = []
    for table_key in sorted(membership):
        profile = raw_profiles.get(table_key)
        if not isinstance(profile, Mapping):
            raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
        profile_sha = str(profile.get("profile_sha256") or "")
        profile_release = str(profile.get("release_id") or "")
        if (
            str(profile.get("table_key") or table_key) != table_key
            or not release_id
            or profile_release != release_id
            or profile_sha != _profile_payload_sha(profile)
        ):
            raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
        profile_receipt.append({
            "table_key": table_key,
            "profile_sha256": profile_sha,
            "release_id": profile_release,
        })
    if _json_sha(profile_receipt) != continuation.get("profile_bundle_sha256"):
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    if expected_release_id and release_id != expected_release_id:
        raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    return dict(continuation)


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
    if metadata.get("status") not in {"ACTIVE", "RESUMING"} or float(metadata.get("expires_at_epoch") or 0) <= time.time():
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
    clarification_plan: Mapping[str, Any] | None = None,
    option_bundle: Mapping[str, Any] | None = None,
    speculative_bundle: Mapping[str, Any] | None = None,
    binding_continuation: Mapping[str, Any] | None = None,
    changed_roles: list[str] | None = None,
    invalidated_stages: list[str] | None = None,
    reusable_artifacts: list[str] | None = None,
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
        plan_record = dict(clarification_plan or {})
        option_record = dict(option_bundle or {})
        speculative_record = dict(speculative_bundle or {})
        binding_record = dict(binding_continuation or {})
        artifact_records: dict[str, dict[str, Any]] = {}
        for name, value in (("clarification_plan.json", plan_record), ("option_bundle.json", option_record), ("speculative_bundle.json", speculative_record), ("binding_continuation.json", binding_record)):
            if value:
                payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                (root / name).write_bytes(payload)
                artifact_records[name] = {"sha256": _bytes_sha(payload), "bytes": len(payload)}
        context = {
            "contract_version": "clarification-context-v2",
            "article_id": article_id,
            "article_body_sha256": article_body_sha256,
            "clarification_answers": [dict(item) for item in clarification_history],
            "semantic_constraints": [],
            "changed_roles": list(changed_roles or []),
            "invalidated_stages": list(invalidated_stages or []),
        }
        context_path = root / "clarification_context.json"
        context_path.write_text(json.dumps(context, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        now = _now()
        metadata = {
            "contract_version": "verification-checkpoint-v2",
            "status": "ACTIVE",
            "article_id": article_id,
            "title": title,
            "article_body_sha256": article_body_sha256,
            "clarification_history_sha256": _json_sha(clarification_history),
            "runtime_fingerprint": runtime_fingerprint,
            "config_sha256": config_sha256,
            "resume_from_stage": resume_from_stage,
            "resume_generation": 1,
            "pending_question_id": plan_record.get("question", {}).get("id") if isinstance(plan_record.get("question"), Mapping) else None,
            "pending_role": plan_record.get("question", {}).get("role") if isinstance(plan_record.get("question"), Mapping) else None,
            "clarification_plan_sha256": _json_sha(plan_record) if plan_record else None,
            "option_bundle_sha256": _json_sha(option_record) if option_record else None,
            "speculative_bundle_sha256": _json_sha(speculative_record) if speculative_record else None,
            "candidate_membership_sha256": plan_record.get("candidate_membership_sha256"),
            "profile_bundle_sha256": plan_record.get("profile_bundle_sha256"),
            "binding_continuation_sha256": artifact_records.get("binding_continuation.json", {}).get("sha256"),
            "binding_candidate_membership_sha256": binding_record.get("candidate_membership_sha256"),
            "binding_profile_bundle_sha256": binding_record.get("profile_bundle_sha256"),
            "binding_release_id": binding_record.get("release_id"),
            "binding_target_scope_sha256": binding_record.get("target_scope_sha256"),
            "changed_roles": list(changed_roles or []),
            "invalidated_stages": list(invalidated_stages or []),
            "reusable_artifacts": list(reusable_artifacts or ["l1", "l2", "layers"]),
            "artifact_records": artifact_records,
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
    expected_question_id: str | None = None,
    expected_role: str | None = None,
) -> Checkpoint:
    cleanup()
    root, metadata = _read(token)
    if metadata.get("article_body_sha256") != article_body_sha256:
        raise CheckpointError("RESUME_CHECKPOINT_FINGERPRINT_MISMATCH")
    if metadata.get("runtime_fingerprint") != runtime_fingerprint or metadata.get("config_sha256") != config_sha256:
        raise CheckpointError("RESUME_CHECKPOINT_FINGERPRINT_MISMATCH")
    if str(metadata.get("title") or "") != title:
        raise CheckpointError("RESUME_CHECKPOINT_ARTICLE_MISMATCH")
    checkpoint = Checkpoint(token, root, root / "articles.jsonl", root / "out", root / "clarification_context.json", metadata)
    if str(metadata.get("resume_from_stage") or "") == "binding":
        continuation = validate_binding_continuation(checkpoint)
        if (
            metadata.get("binding_continuation_sha256") != metadata.get("artifact_records", {}).get("binding_continuation.json", {}).get("sha256")
            or metadata.get("binding_candidate_membership_sha256") != continuation.get("candidate_membership_sha256")
            or metadata.get("binding_profile_bundle_sha256") != continuation.get("profile_bundle_sha256")
            or metadata.get("binding_release_id") != continuation.get("release_id")
            or metadata.get("binding_target_scope_sha256") != continuation.get("target_scope_sha256")
        ):
            raise CheckpointError("RESUME_ARTIFACT_INVALIDATED")
    prior = json.loads((root / "clarification_context.json").read_text(encoding="utf-8"))
    prior_answers = prior.get("clarification_answers") if isinstance(prior, dict) else None
    if not isinstance(prior_answers, list) or clarification_history[: len(prior_answers)] != prior_answers:
        raise CheckpointError("RESUME_CHECKPOINT_HISTORY_MISMATCH")
    pending_question_id = str(metadata.get("pending_question_id") or "")
    pending_role = str(metadata.get("pending_role") or "")
    legacy_question_id = f"clarify-{expected_role}" if expected_role else ""
    if expected_question_id and pending_question_id and expected_question_id not in {pending_question_id, legacy_question_id}:
        raise CheckpointError("CLARIFICATION_QUESTION_MISMATCH")
    if expected_role and pending_role and expected_role != pending_role:
        raise CheckpointError("CLARIFICATION_ROLE_MISMATCH")
    new_answers = clarification_history[len(prior_answers):]
    if pending_question_id and len(new_answers) != 1:
        raise CheckpointError("CLARIFICATION_ANSWER_CARDINALITY_INVALID")
    if not pending_question_id and new_answers:
        raise CheckpointError("CLARIFICATION_PLAN_INVALID")
    if new_answers and pending_question_id:
        plan_path = root / "clarification_plan.json"
        bundle_path = root / "option_bundle.json"
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
            bundle = json.loads(bundle_path.read_text(encoding="utf-8")) if bundle_path.is_file() else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckpointError("CLARIFICATION_PLAN_INVALID") from exc
        input_mode = str(((plan.get("question") or {}).get("input_mode")) or "FREE_TEXT") if isinstance(plan, Mapping) else "FREE_TEXT"
        allow_direct_input = bool(((plan.get("question") or {}).get("allow_direct_input", input_mode in {"DATE", "FREE_TEXT"}))) if isinstance(plan, Mapping) else False
        option_required = input_mode in {"OPTIONS", "SEARCHABLE_OPTIONS"} and not allow_direct_input
        options = bundle.get("options") if isinstance(bundle, Mapping) else None
        option_by_id = {
            str(item.get("option_id") or item.get("id") or ""): item
            for item in options or ()
            if isinstance(item, Mapping)
        }
        for answer in new_answers:
            if not isinstance(answer, Mapping):
                raise CheckpointError("CLARIFICATION_ANSWER_INVALID")
            if str(answer.get("question_id") or "") not in {pending_question_id, legacy_question_id}:
                raise CheckpointError("CLARIFICATION_QUESTION_MISMATCH")
            if str(answer.get("role") or "") != pending_role:
                raise CheckpointError("CLARIFICATION_ROLE_MISMATCH")
            option_id = str(answer.get("option_id") or "").strip()
            if option_required and not option_id:
                raise CheckpointError("CLARIFICATION_OPTION_REQUIRED")
            if option_id:
                option = option_by_id.get(option_id)
                if option is None:
                    raise CheckpointError("CLARIFICATION_OPTION_INVALID")
                expected_label = " ".join(unicodedata.normalize("NFKC", str(option.get("display_label") or option.get("label") or "")).split())
                supplied_label = " ".join(unicodedata.normalize("NFKC", str(answer.get("value") or "")).split())
                if not expected_label or expected_label != supplied_label:
                    raise CheckpointError("CLARIFICATION_OPTION_VALUE_MISMATCH")
            elif input_mode not in {"DATE", "FREE_TEXT"} and not allow_direct_input:
                raise CheckpointError("CLARIFICATION_OPTION_INVALID")
    # A resume can fail after validation but before the next checkpoint or a
    # final receipt is published.  Keep the same sealed answer retryable;
    # the service will discard it only after a completed successor/final run.
    metadata["status"] = "RESUMING"
    metadata["resuming_answer_sha256"] = _json_sha(new_answers[-1]) if new_answers else None
    (root / "checkpoint.json").write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return checkpoint


def update_context(checkpoint: Checkpoint, clarification_history: list[Mapping[str, Any]]) -> None:
    context = json.loads(checkpoint.context_path.read_text(encoding="utf-8"))
    context["clarification_answers"] = [dict(item) for item in clarification_history]
    context.setdefault("contract_version", "clarification-context-v2")
    constraints = []
    for answer in clarification_history:
        if isinstance(answer, Mapping):
            constraints.append({
                "role": answer.get("role"), "value": answer.get("value"),
                "source": "USER_CLARIFICATION", "question_id": answer.get("question_id"),
                "answer_sha256": _json_sha(dict(answer)),
                "option_bundle_sha256": context.get("option_bundle_sha256"),
            })
    context["semantic_constraints"] = constraints
    checkpoint.context_path.write_text(
        json.dumps(context, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


def discard(checkpoint: Checkpoint) -> None:
    _remove(checkpoint.root)


def read_option_page(
    token: str,
    *,
    question_id: str,
    query: str = "",
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Read-only pagination over a checkpoint's sealed public option bundle."""
    if not 1 <= int(limit) <= 50:
        raise CheckpointError("CLARIFICATION_OPTIONS_LIMIT_INVALID")
    root, metadata = _read(token)
    if str(metadata.get("pending_question_id") or "") != str(question_id):
        raise CheckpointError("CLARIFICATION_QUESTION_MISMATCH")
    path = root / "option_bundle.json"
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError("CLARIFICATION_OPTION_BUNDLE_INVALID") from exc
    if not isinstance(bundle, Mapping) or bundle.get("question_id") != question_id:
        raise CheckpointError("CLARIFICATION_OPTION_BUNDLE_INVALID")
    bundle_sha = _json_sha(bundle)
    expected_bundle_sha = str(metadata.get("option_bundle_sha256") or "")
    if expected_bundle_sha and expected_bundle_sha != bundle_sha:
        raise CheckpointError("CLARIFICATION_OPTION_BUNDLE_MISMATCH")
    raw_query = " ".join(unicodedata.normalize("NFKC", str(query or "")).split())
    query_key = _json_sha({"question_id": question_id, "query": raw_query, "bundle": bundle_sha})
    offset = 0
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)).decode("utf-8"))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CheckpointError("CLARIFICATION_CURSOR_INVALID") from exc
        if not isinstance(decoded, Mapping) or decoded.get("query_sha256") != query_key or decoded.get("bundle_sha256") != bundle_sha:
            raise CheckpointError("CLARIFICATION_CURSOR_MISMATCH")
        offset = int(decoded.get("offset") or 0)
    options = bundle.get("options")
    if not isinstance(options, list):
        raise CheckpointError("CLARIFICATION_OPTION_BUNDLE_INVALID")
    normalized = " ".join(unicodedata.normalize("NFKC", raw_query).casefold().split())
    selected = []
    for item in options:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("display_label") or item.get("label") or "")
        if normalized and normalized not in " ".join(unicodedata.normalize("NFKC", label).casefold().split()):
            continue
        selected.append({
            "id": str(item.get("option_id") or item.get("id") or ""),
            "label": label,
            "description": str(item.get("description") or ""),
            "applicable_candidate_count": int(item.get("applicable_candidate_count") or len(item.get("applicability") or [])),
        })
    selected.sort(key=lambda item: (item["label"], item["id"]))
    page = selected[offset:offset + int(limit)]
    next_offset = offset + len(page)
    next_cursor = None
    if next_offset < len(selected):
        payload = {"question_id": question_id, "query_sha256": query_key, "bundle_sha256": bundle_sha, "offset": next_offset}
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "question_id": question_id,
        "options": page,
        "page": {"total": len(selected), "limit": int(limit), "next_cursor": next_cursor, "search_supported": True, "options_complete": next_cursor is None},
    }


__all__ = ["Checkpoint", "CheckpointError", "TTL_SECONDS", "MAX_CHECKPOINTS", "cleanup", "consume", "create", "discard", "read_option_page", "update_context"]
