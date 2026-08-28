"""Fixture-first, append-only stage trace runner for ``article_body_v1``."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import traceback
import uuid
from typing import Any, Mapping

from ..hcx_claim_experiment import env_api_key
from .article_body_sentence_splitter_v1 import (
    SPLITTER_MODE,
    iter_article_body_sentence_spans,
    splitter_source_sha256,
)
from .l1_value_candidates import build_span_candidates
from .run_l2_segmentation import L2ReceiptError, run as run_l2
from .run_layer_stack import run_stack
from .run_pipeline_operational_v2 import (
    OperationalPipelineError,
    materialize_operational_l2,
    project_trace_operational_l2,
    run_live_from_files,
)

CONTRACT_VERSION = "article-body-pipeline-trace-v1"
CALL_LIMITS = {
    "hcx_l2": 1, "official_search": 46, "bm25": 46,
    "query_encoder": 69, "qdrant_dense": 69, "reranker": 23,
    "metadata_api": 6000, "cell_api": 23, "hcx_answer": 46,
}
SAME_SERIES_EVIDENCE_CELL_API_LIMIT = 128
PHYSICAL_CELL_API_TOTAL_LIMIT = CALL_LIMITS["cell_api"] + SAME_SERIES_EVIDENCE_CELL_API_LIMIT
_CACHE_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CACHE_RECEIPT_KEYS = {
    "contract_version", "scope", "key_contract", "misses", "hits",
    "entries", "upstream_calls", "entry_receipts",
}
_STAGE_FILES = {
    "01": ({"01_sentences.jsonl", "01_value_candidates.jsonl"}, {"01_trace.log"}),
    "02": ({"02_l2_predictions.jsonl", "02_l2_results.jsonl"}, {"02_trace.log"}),
    "03": ({"03_routed.jsonl"}, {"03_trace.log"}),
    "04": ({"04_stage_ledger.jsonl", "04_answers.jsonl"}, {"04_trace.log"}),
}
_SECRET_RE = re.compile(r"(?:Bearer\s+[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|token)\s*[:=]\s*\S+)", re.I)
_SECRET_ENV_NAME_RE = re.compile(
    r"(?:^|_)(?:API_KEY|APIKEY|SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE_KEY|CREDENTIAL)(?:_|$)",
    re.I,
)
_NON_SECRET_ENV_SUFFIX_RE = re.compile(
    r"_(?:URL|URI|PATH|FILE|SOURCE|ID|REVISION|SHA256|HOST|PORT|PRESENT|ENABLED|NAME|COUNT|LIMIT|MODE|TYPE)$",
    re.I,
)


class TraceStageError(RuntimeError):
    pass


def _publish_runtime_directory(source: Path, destination: Path, *, attempts: int = 10) -> None:
    """Atomically publish a completed runtime directory despite short Windows handle lag."""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            gc.collect()
            time.sleep(0.2)


def _remove_runtime_temp(path: Path) -> None:
    """Best-effort cleanup; a locked diagnostic directory is evidence, not a second failure."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        return


def _same_series_evidence_cell_usage_v2m(evidence_answers: Any) -> int:
    if evidence_answers in (None, (), []):
        return 0
    if not isinstance(evidence_answers, (list, tuple)):
        raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
    total = 0
    for answer in evidence_answers:
        if not isinstance(answer, Mapping):
            raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
        evidence = answer.get("evidence_answer")
        identity = evidence.get("identity") if isinstance(evidence, Mapping) else None
        receipt = identity.get("cell_fetch_cache") if isinstance(identity, Mapping) else None
        if answer.get("status") != "EVALUATED":
            if receipt is not None:
                raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
            continue
        if not isinstance(receipt, Mapping) or set(receipt) != _CACHE_RECEIPT_KEYS:
            raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
        if (
            receipt.get("contract_version") != "same-series-cell-fetch-cache-v2l"
            or receipt.get("scope") != "ONE_SYNTHESIS_INVOCATION"
            or receipt.get("key_contract") != "canonical-full-query-plan-sha256-v1"
        ):
            raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
        counters = [receipt.get(name) for name in ("misses", "hits", "entries", "upstream_calls")]
        if any(type(value) is not int or value < 0 for value in counters):
            raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
        misses, _hits, entries, upstream_calls = counters
        entry_receipts = receipt.get("entry_receipts")
        if (
            misses != entries or entries != upstream_calls
            or not isinstance(entry_receipts, list) or len(entry_receipts) != entries
        ):
            raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
        query_hashes: list[str] = []
        for entry in entry_receipts:
            if not isinstance(entry, Mapping) or set(entry) != {"query_sha256", "response_sha256"}:
                raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
            query_sha = entry.get("query_sha256")
            response_sha = entry.get("response_sha256")
            if (
                not isinstance(query_sha, str) or _CACHE_SHA256_RE.fullmatch(query_sha) is None
                or not isinstance(response_sha, str) or _CACHE_SHA256_RE.fullmatch(response_sha) is None
            ):
                raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
            query_hashes.append(query_sha)
        if query_hashes != sorted(query_hashes) or len(query_hashes) != len(set(query_hashes)):
            raise TraceStageError("CALL_BUDGET_RECEIPT_INVALID")
        total += upstream_calls
    return total


def enforce_call_limits(
    calls: Mapping[str, Any], evidence_answers: Any = None,
) -> dict[str, dict[str, int]]:
    ledger: dict[str, dict[str, int]] = {}
    for name, limit in CALL_LIMITS.items():
        if name == "cell_api":
            total_cell_api = int(calls.get(name) or 0)
            evidence_cell_api = _same_series_evidence_cell_usage_v2m(evidence_answers)
            base_cell_api = total_cell_api - evidence_cell_api
            if (
                base_cell_api < 0 or base_cell_api > limit
                or evidence_cell_api > SAME_SERIES_EVIDENCE_CELL_API_LIMIT
                or total_cell_api > PHYSICAL_CELL_API_TOTAL_LIMIT
            ):
                raise TraceStageError("CALL_BUDGET_EXHAUSTED")
            ledger[name] = {"used": base_cell_api, "limit": limit}
            ledger["same_series_evidence_cell_api"] = {
                "used": evidence_cell_api, "limit": SAME_SERIES_EVIDENCE_CELL_API_LIMIT,
            }
            ledger["physical_cell_api_total"] = {
                "used": total_cell_api, "limit": PHYSICAL_CELL_API_TOTAL_LIMIT,
            }
            continue
        used = int(calls.get(name) or 0)
        if used > limit:
            raise TraceStageError("CALL_BUDGET_EXHAUSTED")
        ledger[name] = {"used": used, "limit": limit}
    return ledger


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _rows(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _atomic_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    _atomic_bytes(
        path,
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n"
            for row in rows
        ).encode("utf-8"),
    )


def _safe_env_secrets() -> list[str]:
    values: list[str] = []
    for name, value in os.environ.items():
        if (
            isinstance(value, str)
            and len(value) >= 8
            and _SECRET_ENV_NAME_RE.search(name)
            and not _NON_SECRET_ENV_SUFFIX_RE.search(name)
        ):
            values.append(value)
    return values


def _scan_text(value: str) -> bool:
    if _SECRET_RE.search(value):
        return True
    return any(secret in value for secret in _safe_env_secrets())


def _scan_tree(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if _scan_text(text):
                hits.append(str(path.relative_to(root)))
    return hits


def _record(path: Path, *, rows: bool = True) -> dict[str, Any]:
    data = path.read_bytes()
    result: dict[str, Any] = {"sha256": _sha_bytes(data), "bytes": len(data)}
    if rows:
        result["rows"] = _rows(path)
    return result


def _articles(path: Path) -> list[dict[str, Any]]:
    try:
        result = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception as exc:  # pragma: no cover - bounded projection
        raise TraceStageError("ARTICLE_INPUT_INVALID") from exc
    if not all(isinstance(row, dict) for row in result):
        raise TraceStageError("ARTICLE_INPUT_INVALID")
    return result


def _article_meta(articles: list[Mapping[str, Any]]) -> tuple[list[str], dict[str, str]]:
    ids: list[str] = []
    bodies: dict[str, str] = {}
    for row in articles:
        article_id = str(row.get("article_idx") or "")
        body = str(row.get("article_text") or "")
        if not article_id:
            raise TraceStageError("ARTICLE_INPUT_INVALID")
        ids.append(article_id)
        bodies[article_id] = _sha_bytes(body.encode("utf-8"))
    if len(ids) != len(set(ids)):
        raise TraceStageError("ARTICLE_INPUT_INVALID")
    return ids, bodies


def _manifest(
    *, stage: str, root: Path, article_path: Path, articles: list[Mapping[str, Any]],
    predecessor_sha256: str | None, data_names: set[str], log_names: set[str],
    operational: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ids, bodies = _article_meta(articles)
    data = {name: _record(root / name) for name in sorted(data_names)}
    logs = {name: _record(root / name) for name in sorted(log_names)}
    runtime: dict[str, Any] = {}
    runtime_root = root / "04_runtime"
    if stage == "04" and runtime_root.exists():
        for path in sorted(item for item in runtime_root.rglob("*") if item.is_file()):
            runtime[str(path.relative_to(root)).replace("\\", "/")] = _record(path, rows=False)
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "COMPLETE",
        "stage": stage,
        "created_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "article_input": {"path": str(article_path.resolve()), "sha256": _sha_file(article_path)},
        "ordered_article_ids": ids,
        "article_body_sha256": bodies,
        "splitter_mode": SPLITTER_MODE,
        "splitter_source_sha256": splitter_source_sha256(),
        "predecessor_manifest_sha256": predecessor_sha256,
        "data_payloads": data,
        "sealed_logs": logs,
        "runtime_payloads": runtime,
        "operational_l2": dict(operational or {}),
        "call_ledger": {name: {"used": 0, "limit": limit} for name, limit in CALL_LIMITS.items()},
        "secret_scan": {"status": "PASS", "hits": 0},
    }


def _publish_manifest(root: Path, manifest: dict[str, Any]) -> None:
    if _scan_tree(root):
        raise TraceStageError("SECRET_SCAN_FAILED")
    _atomic_bytes(root / f"{manifest['stage']}_manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _emit_log(root: Path, stage: str, lines: list[str]) -> None:
    name = f"{stage}_trace.log"
    text = "".join(line.rstrip("\n") + "\n" for line in lines)
    if _scan_text(text):
        raise TraceStageError("SECRET_SCAN_FAILED")
    for line in text.splitlines():
        print(line, flush=True)
    _atomic_bytes(root / name, text.encode("utf-8"))


def _safe_name(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and path.name == name


def _validate_manifest(root: Path, manifest: Mapping[str, Any], stage: str, article_path: Path, articles: list[Mapping[str, Any]]) -> str:
    if manifest.get("status") != "COMPLETE" or str(manifest.get("stage")) != stage:
        raise TraceStageError("MANIFEST_INVALID")
    expected_data, expected_logs = _STAGE_FILES[stage]
    data = manifest.get("data_payloads")
    logs = manifest.get("sealed_logs")
    runtime = manifest.get("runtime_payloads")
    if not isinstance(data, Mapping) or not isinstance(logs, Mapping) or not isinstance(runtime, Mapping):
        raise TraceStageError("MANIFEST_INVALID")
    if set(data) != expected_data or set(logs) != expected_logs or (stage != "04" and runtime):
        raise TraceStageError("MANIFEST_INVALID")
    if set(data) & set(logs) or set(data) & set(runtime) or set(logs) & set(runtime):
        raise TraceStageError("MANIFEST_INVALID")
    for name, record in list(data.items()) + list(logs.items()):
        if not _safe_name(str(name)) or not isinstance(record, Mapping):
            raise TraceStageError("MANIFEST_INVALID")
        path = root / str(name)
        if not path.is_file():
            raise TraceStageError("MANIFEST_INVALID")
        actual = _record(path)
        if actual.get("sha256") != record.get("sha256") or actual.get("bytes") != record.get("bytes") or actual.get("rows") != record.get("rows"):
            raise TraceStageError("SHA_MISMATCH")
    if stage == "04":
        for name, record in runtime.items():
            path = Path(str(name))
            if path.is_absolute() or ".." in path.parts or not str(name).replace("\\", "/").startswith("04_runtime/") or not isinstance(record, Mapping):
                raise TraceStageError("MANIFEST_INVALID")
            file_path = root / path
            if not file_path.is_file() or file_path.is_symlink():
                raise TraceStageError("MANIFEST_INVALID")
            actual = _record(file_path, rows=False)
            if actual.get("sha256") != record.get("sha256") or actual.get("bytes") != record.get("bytes"):
                raise TraceStageError("SHA_MISMATCH")
    ids, bodies = _article_meta(articles)
    if manifest.get("article_input", {}).get("sha256") != _sha_file(article_path):
        raise TraceStageError("SHA_MISMATCH")
    if list(manifest.get("ordered_article_ids") or []) != ids or dict(manifest.get("article_body_sha256") or {}) != bodies:
        raise TraceStageError("PRECOMPUTED_PARTITION_MISMATCH")
    if manifest.get("splitter_mode") != SPLITTER_MODE or manifest.get("splitter_source_sha256") != splitter_source_sha256():
        raise TraceStageError("SENTENCE_INVENTORY_MISMATCH")
    return _sha_file(root / f"{stage}_manifest.json")


def _load_predecessor(root: Path, stage: str, article_path: Path, articles: list[Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
    path = root / f"{stage}_manifest.json"
    if not path.is_file():
        raise TraceStageError("PREDECESSOR_MISSING")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TraceStageError("MANIFEST_INVALID") from exc
    return manifest, _validate_manifest(root, manifest, stage, article_path, articles)


def _stage_targets(stage: str) -> set[str]:
    data, logs = _STAGE_FILES[stage]
    targets = set(data) | set(logs) | {f"{stage}_manifest.json", f"{stage}_failure.json"}
    if stage == "04":
        targets.add("04_runtime")
    return targets


def _assert_stage_start(root: Path, stage: str, article_path: Path, articles: list[Mapping[str, Any]], predecessor: str | None) -> None:
    targets = _stage_targets(stage)
    if any((root / name).exists() for name in targets):
        raise TraceStageError("OUTPUT_EXISTS")
    if stage == "04" and any(root.glob(".04_runtime.*.tmp")):
        raise TraceStageError("OUTPUT_EXISTS")
    if stage == "01":
        if any(root.iterdir()):
            raise TraceStageError("OUTPUT_EXISTS")
        return
    expected_prev = {"02": "01", "03": "02", "04": "03"}[stage]
    prev_manifest, prev_sha = _load_predecessor(root, expected_prev, article_path, articles)
    chain_stage = expected_prev
    chain_manifest = prev_manifest
    while chain_stage != "01":
        chain_prev = {"02": "01", "03": "02"}[chain_stage]
        _prior_manifest, prior_sha = _load_predecessor(root, chain_prev, article_path, articles)
        if chain_manifest.get("predecessor_manifest_sha256") != prior_sha:
            raise TraceStageError("SHA_MISMATCH")
        chain_stage = chain_prev
        chain_manifest = _prior_manifest
    if predecessor not in (None, "__ANY__") and prev_sha != predecessor:
        raise TraceStageError("SHA_MISMATCH")


def run_l1(articles: list[dict[str, Any]], article_path: Path, root: Path) -> dict[str, Any]:
    sentences: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    logs = [f"[L1] articles={len(articles)}/{len(articles)}"]
    for article in articles:
        article_id = str(article["article_idx"])
        body = str(article.get("article_text") or "")
        for sid, start, end, text in iter_article_body_sentence_spans(body):
            sentences.append({"article_idx": article_id, "sentence_id": sid, "char_start": start, "char_end": end, "text": text})
            logs.append(f"[L1] article={article_id} sentence={sid} offset={start}:{end} text={text}")
        article_candidates = build_span_candidates(body, sentence_span_iterator=iter_article_body_sentence_spans)
        candidates.extend({"article_idx": article_id, **candidate} for candidate in article_candidates)
        for candidate in article_candidates:
            if candidate.get("kind") == "value_unit":
                logs.append(f"[L1] article={article_id} value={candidate.get('text')} offset={candidate.get('char_start')}:{candidate.get('char_end')}")
    _atomic_jsonl(root / "01_sentences.jsonl", sentences)
    _atomic_jsonl(root / "01_value_candidates.jsonl", candidates)
    value_count = sum(1 for candidate in candidates if candidate.get("kind") == "value_unit")
    logs.append(f"[L1] sentences={len(sentences)}/{len(sentences)} values={value_count}/{value_count}")
    _emit_log(root, "01", logs)
    manifest = _manifest(stage="01", root=root, article_path=article_path, articles=articles, predecessor_sha256=None, data_names=_STAGE_FILES["01"][0], log_names=_STAGE_FILES["01"][1])
    _publish_manifest(root, manifest)
    return manifest


def run_l2_stage(articles: list[dict[str, Any]], article_path: Path, root: Path, *, api_key: str = "") -> dict[str, Any]:
    resolved_api_key = api_key or env_api_key()
    if not resolved_api_key:
        raise TraceStageError("L2_CALL_FAILED")
    try:
        predictions, raw_manifest = run_l2(
            articles,
            api_key=resolved_api_key,
            retries=0,
            pause_seconds=0,
            sentence_span_iterator=iter_article_body_sentence_spans,
        )
    except L2ReceiptError as exc:
        error_code = (
            exc.args[0]
            if len(exc.args) == 1 and isinstance(exc.args[0], str)
            else ""
        )
        if error_code == "L2_RESPONSE_INVALID":
            raise TraceStageError("L2_RESPONSE_INVALID") from exc
        raise TraceStageError("L2_STAGE_FAILED") from exc
    total_tokens = raw_manifest.get("total_tokens")
    if isinstance(total_tokens, bool) or not isinstance(total_tokens, int) or total_tokens < 0:
        raise TraceStageError("L2_RESPONSE_INVALID")
    if total_tokens > 60000:
        raise TraceStageError("CALL_BUDGET_EXHAUSTED")
    try:
        materialized = materialize_operational_l2(
            articles,
            predictions,
            raw_manifest,
            external_model_calls=int(len(articles)),
        )
        projected = project_trace_operational_l2(materialized)
    except OperationalPipelineError as exc:
        error_code = (
            exc.args[0]
            if len(exc.args) == 1 and isinstance(exc.args[0], str)
            else ""
        )
        if error_code == "L2_RESPONSE_INVALID":
            raise TraceStageError("L2_RESPONSE_INVALID") from exc
        raise TraceStageError("L2_STAGE_FAILED") from exc
    flat: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    for result in projected["results"]:
        start = len(flat)
        selected = list(result.get("predictions") or []) if result.get("status") == "L2_READY" else []
        flat.extend(selected)
        result_rows.append({"article_idx": str(result.get("article_idx") or ""), "status": result.get("status"), "prediction_row_start": start, "prediction_row_end": len(flat)})
    l2_call_ledger = enforce_call_limits({"hcx_l2": int(raw_manifest.get("articles") or len(articles))})
    _atomic_jsonl(root / "02_l2_predictions.jsonl", flat)
    _atomic_jsonl(root / "02_l2_results.jsonl", result_rows)
    unresolved = sum(1 for error in projected.get("manifest", {}).get("errors", []) if error.get("kind") == "UNRESOLVED_SPANS")
    _emit_log(root, "02", [f"[L2] articles={len(articles)}/{len(articles)} predictions={len(flat)}/{len(flat)} unresolved={unresolved}/{unresolved}"])
    predecessor = _sha_file(root / "01_manifest.json") if (root / "01_manifest.json").exists() else None
    manifest = _manifest(stage="02", root=root, article_path=article_path, articles=articles, predecessor_sha256=predecessor, data_names=_STAGE_FILES["02"][0], log_names=_STAGE_FILES["02"][1], operational=projected.get("manifest"))
    manifest["call_ledger"] = l2_call_ledger
    _publish_manifest(root, manifest)
    return manifest


def run_layers_stage(articles: list[dict[str, Any]], article_path: Path, root: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in (root / "02_l2_predictions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    l2_results = [json.loads(line) for line in (root / "02_l2_results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ready_ids = {str(row.get("article_idx") or "") for row in l2_results if row.get("status") == "L2_READY"}
    routed = run_stack(
        [article for article in articles if str(article.get("article_idx") or "") in ready_ids],
        rows,
        sentence_span_iterator=iter_article_body_sentence_spans,
    )
    _atomic_jsonl(root / "03_routed.jsonl", routed)
    details = [
        f"[L3-L5] article={row.get('article_idx')} "
        f"target={row.get('value_span_id') or row.get('target_id') or ''} "
        f"routing={row.get('routing_class') or ''} "
        f"query={' | '.join(str(query.get('role') or '') + '=' + str(query.get('query') or '') for query in (row.get('retrieval_queries') or []))}"
        for row in routed
    ]
    _emit_log(root, "03", details + [f"[L3-L5] routed={len(routed)}/{len(routed)}"])
    predecessor = _sha_file(root / "02_manifest.json") if (root / "02_manifest.json").exists() else None
    manifest = _manifest(stage="03", root=root, article_path=article_path, articles=articles, predecessor_sha256=predecessor, data_names=_STAGE_FILES["03"][0], log_names=_STAGE_FILES["03"][1])
    _publish_manifest(root, manifest)
    return manifest


def run_live_stage(
    articles: list[dict[str, Any]],
    article_path: Path,
    root: Path,
    *,
    config_path: str | Path,
    role_aware_dimension_shadow: bool = False,
    claim_query: str | None = None,
    profile_seed_override: str | Path | None = None,
    failure_recovery_shadow: bool = False,
    user_intent_shadow: bool = False,
) -> dict[str, Any]:
    """Run live only from validated L2/L3 predecessor manifests."""
    # Keep the working directory as a short sibling.  Nesting it below a
    # descriptive diagnostic run name can exceed the legacy Windows path
    # limit once per-table raw snapshot names are appended.
    runtime_tmp = root.parent / f".rt04.{uuid.uuid4().hex[:12]}.tmp"
    runtime_final = root / "04_runtime"
    if runtime_final.exists():
        raise TraceStageError("OUTPUT_EXISTS")
    try:
        _assert_stage_start(root, "04", article_path, articles, "__ANY__")
        result = run_live_from_files(
            config_path, article_path, runtime_tmp,
            operational_cache_override=runtime_tmp / "run_cache" / "operational_profiles.sqlite3",
            snapshot_root_override=runtime_tmp / "raw_snapshots",
            budget_ledger_override=runtime_tmp / "budget.sqlite3",
            audit_run_id=root.name,
            pilot_metadata_limit_override=CALL_LIMITS["metadata_api"],
            precomputed_l2_manifest_path=root / "02_manifest.json",
            precomputed_routed_manifest_path=root / "03_manifest.json",
            role_aware_dimension_shadow=role_aware_dimension_shadow,
            claim_query=claim_query,
            profile_seed_override=profile_seed_override,
            failure_recovery_shadow=failure_recovery_shadow,
            user_intent_shadow=user_intent_shadow,
        )
        if int((result.get("l2") or {}).get("external_model_calls") or 0) != 0:
            raise TraceStageError("CALL_BUDGET_EXHAUSTED")
        if result.get("stack_recomputed") is not False:
            raise TraceStageError("DOWNSTREAM_FAILED")
        _publish_runtime_directory(runtime_tmp, runtime_final)
        _atomic_jsonl(root / "04_stage_ledger.jsonl", list(result.get("stage_ledger") or []))
        _atomic_jsonl(root / "04_answers.jsonl", list(result.get("answers") or []))
        calls = result.get("article_api_calls") or {}
        ledger_rows = list(result.get("stage_ledger") or [])
        answer_rows = list(result.get("answers") or [])
        targets: dict[str, dict[str, Any]] = {}
        for row in ledger_rows:
            target = str(row.get("target_id") or f"{row.get('article_idx') or ''}:{row.get('value_span_id') or 'article'}")
            targets.setdefault(target, {}).update(row)
        for row in answer_rows:
            target = str(row.get("target_id") or f"{row.get('article_idx') or ''}:article")
            targets.setdefault(target, {}).update({"answer": row})
        lines = []
        for target, row in sorted(targets.items()):
            intent = row.get("user_intent_shadow") if isinstance(row.get("user_intent_shadow"), Mapping) else {}
            retrieval = row.get("retrieval") if isinstance(row.get("retrieval"), Mapping) else {}
            resolution = row.get("resolution")
            inventory_validation = row.get("inventory_validation")
            profile = row.get("profile") or row.get("profile_sha256")
            if not profile and isinstance(inventory_validation, Mapping):
                profile = inventory_validation.get("profile_sha256") or inventory_validation.get("profile")
            binding = row.get("binding") or row.get("binding_plan") or row.get("assignment") or {}
            cell = row.get("cell") or {}
            comparison = row.get("comparison") or {}
            answer = row.get("answer") or {}
            lines.extend([
                f"[INTENT] target={target} task={intent.get('task_intent') or ''} "
                f"measurements={','.join(str(value) for value in intent.get('measurement_intents') or [])} "
                f"status={intent.get('status') or ''}",
                f"[RETRIEVAL] target={target} path={retrieval.get('channels') or retrieval.get('path') or ''} query={retrieval.get('query') or ''}",
                f"[BINDING] target={target} profile={profile or ''} binding={binding}",
                f"[CELL] target={target} cell={cell} comparator={comparison}",
                f"[ANSWER] target={target} verdict={answer.get('verdict') or row.get('verdict') or ''} reason={answer.get('reason') or resolution or ''}",
            ])
        call_ledger = enforce_call_limits(calls, result.get("evidence_answers"))
        lines.extend([
            f"[RETRIEVAL] calls={int(calls.get('official_search') or 0)}/{CALL_LIMITS['official_search']}",
            f"[BINDING] metadata={int(calls.get('metadata_api') or 0)}/{CALL_LIMITS['metadata_api']}",
            f"[CELL] base={call_ledger['cell_api']['used']}/{call_ledger['cell_api']['limit']} "
            f"evidence={call_ledger['same_series_evidence_cell_api']['used']}/{call_ledger['same_series_evidence_cell_api']['limit']} "
            f"total={call_ledger['physical_cell_api_total']['used']}/{call_ledger['physical_cell_api_total']['limit']}",
            f"[ANSWER] answers={len(answer_rows)}/{len(articles)} l2=0/0 stack=0/0",
        ])
        _emit_log(root, "04", lines)
        predecessor = _sha_file(root / "03_manifest.json")
        manifest = _manifest(stage="04", root=root, article_path=article_path, articles=articles, predecessor_sha256=predecessor, data_names=_STAGE_FILES["04"][0], log_names=_STAGE_FILES["04"][1])
        manifest["call_ledger"] = call_ledger
        _publish_manifest(root, manifest)
        return manifest
    except TraceStageError:
        _remove_runtime_temp(runtime_tmp)
        raise
    except Exception as exc:
        _remove_runtime_temp(runtime_tmp)
        raw_code = str(exc.args[0]) if exc.args else ""
        safe_code = raw_code if re.fullmatch(r"[A-Z][A-Z0-9_:-]{0,120}", raw_code) else "UNCLASSIFIED"
        frames = traceback.extract_tb(exc.__traceback__)
        origin = "unknown"
        if frames:
            last = frames[-1]
            safe_function = re.sub(r"[^A-Za-z0-9_]", "_", last.name)[:80] or "unknown"
            origin = f"{Path(last.filename).name}:{last.lineno}:{safe_function}"
        raise TraceStageError(
            f"DOWNSTREAM_FAILED:{type(exc).__name__}:{safe_code}:{origin}"
        ) from exc


def run_trace(
    *,
    articles_path: str | Path,
    output_root: str | Path,
    stage: str,
    config_path: str | Path | None = None,
    role_aware_dimension_shadow: bool = False,
    claim_query: str | None = None,
    profile_seed_override: str | Path | None = None,
    failure_recovery_shadow: bool = False,
    user_intent_shadow: bool = False,
) -> dict[str, Any]:
    article_path = Path(articles_path).resolve()
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    articles = _articles(article_path)
    stages = {"l1": ["01"], "l2": ["02"], "layers": ["03"], "live": ["04"], "all": ["01", "02", "03", "04"]}[stage]
    if stage in {"l1", "all"}:
        _assert_stage_start(root, "01", article_path, articles, None)
    elif stage == "l2":
        _assert_stage_start(root, "02", article_path, articles, "__ANY__")
    elif stage == "layers":
        _assert_stage_start(root, "03", article_path, articles, "__ANY__")
    elif stage == "live":
        _assert_stage_start(root, "04", article_path, articles, "__ANY__")
    manifests: dict[str, Any] = {}
    try:
        if "01" in stages:
            manifests["01"] = run_l1(articles, article_path, root)
        if "02" in stages:
            if "01" in stages:
                _assert_stage_start(root, "02", article_path, articles, "__ANY__")
            manifests["02"] = run_l2_stage(articles, article_path, root)
        if "03" in stages:
            if "02" in stages:
                _assert_stage_start(root, "03", article_path, articles, "__ANY__")
            manifests["03"] = run_layers_stage(articles, article_path, root)
        if "04" in stages:
            if config_path is None:
                raise TraceStageError("LIVE_PREFLIGHT_BLOCKED")
            if "03" in stages:
                _assert_stage_start(root, "04", article_path, articles, "__ANY__")
            manifests["04"] = run_live_stage(
                articles,
                article_path,
                root,
                config_path=config_path,
                role_aware_dimension_shadow=role_aware_dimension_shadow,
                claim_query=claim_query,
                profile_seed_override=profile_seed_override,
                failure_recovery_shadow=failure_recovery_shadow,
                user_intent_shadow=user_intent_shadow,
            )
        return manifests
    except TraceStageError as exc:
        for target in _stage_targets(stages[min(len(manifests), len(stages) - 1)]):
            path = root / target
            if path.is_file() and not target.endswith("_failure.json"):
                path.unlink()
            elif path.is_dir() and target == "04_runtime":
                _remove_runtime_temp(path)
        failure_stage = stages[min(len(manifests), len(stages) - 1)]
        _atomic_bytes(root / f"{failure_stage}_failure.json", (json.dumps({"status": "FAILED", "error_code": str(exc.args[0]) if exc.args else "DOWNSTREAM_FAILED"}) + "\n").encode("utf-8"))
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config")
    parser.add_argument("--stage", choices=("l1", "l2", "layers", "live", "all"), default="all")
    parser.add_argument("--role-aware-dimension-shadow", action="store_true")
    parser.add_argument("--claim-query")
    parser.add_argument("--profile-seed")
    args = parser.parse_args(argv)
    try:
        result = run_trace(
            articles_path=args.articles,
            output_root=args.output,
            stage=args.stage,
            config_path=args.config,
            role_aware_dimension_shadow=args.role_aware_dimension_shadow,
            claim_query=args.claim_query,
            profile_seed_override=args.profile_seed,
        )
    except TraceStageError as exc:
        print(json.dumps({"status": "FAILED", "error_code": str(exc.args[0]) if exc.args else "DOWNSTREAM_FAILED"}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "COMPLETE", "stages": sorted(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
