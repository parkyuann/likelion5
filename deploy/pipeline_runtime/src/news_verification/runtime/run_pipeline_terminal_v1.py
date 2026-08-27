"""Interactive terminal entrypoint for one article-body verification run.

This module is deliberately a thin wrapper around the sealed article-body
trace.  It does not change retrieval, binding, comparison, or answer rules.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
import uuid

from src.news_verification.runtime.adapters.shadow_compat import annual_requery_api

AnnualRequeryError, verify_annual_requery = annual_requery_api()
from src.news_verification.runtime.l1_value_candidates import build_span_candidates
from src.news_verification.runtime.local_pipeline_services_v1 import (
    LocalPipelineServices,
    LocalServiceStartError,
    SERVICE_BLOCKERS,
)
from src.news_verification.runtime.adapters.legacy_stages import body_trace_api

_trace_module = body_trace_api()
TraceStageError = _trace_module.TraceStageError
_validate_manifest = _trace_module._validate_manifest
run_trace = _trace_module.run_trace
from src.news_verification.runtime.portfolio_mvp_sealing_v1 import (
    SealingError,
    ast_top_level_function_sha256,
    atomic_publish_json,
    build_exact_tree_inventory,
    canonical_sha256,
    inventory_sha256,
    sha256_file,
    validate_exact_tree,
)
from src.news_verification.runtime.run_pipeline_operational_v2 import preflight
from src.news_verification.runtime.operational_article_acquisition_v2 import ArticleAcquisitionError, acquire_article_url


CONTRACT_VERSION = "pipeline-terminal-v1"
DEFAULT_OUTPUT_PARENT = Path("data/develop/terminal_pipeline_runs")
COMPLETION_RECEIPT_CONTRACT = "portfolio-mvp-terminal-completion-receipt-v3"
_TRACE_STAGES = ("01", "02", "03", "04")
_RUNTIME_ROOT = Path(__file__).resolve().parent
_LEGACY_DEVELOP_ROOT = Path(__file__).resolve().parents[3] / "src" / "develop"
_CODE_PATHS = {
    "terminal": Path(__file__),
    "trace": _LEGACY_DEVELOP_ROOT / "run_article_body_pipeline_trace_v1.py",
    "operational": _RUNTIME_ROOT / "run_pipeline_operational_v2.py",
    "query_front": _LEGACY_DEVELOP_ROOT / "deterministic_query_claim_front_v1.py",
    "answer": _RUNTIME_ROOT / "operational_answer_v2.py",
}

BLOCKER_GUIDANCE = {
    "QUERY_ENCODER_UNAVAILABLE": "질의 encoder가 실행되지 않았거나 /health 응답이 없습니다.",
    "RERANKER_UNAVAILABLE": "reranker가 실행되지 않았거나 /health 응답이 없습니다.",
    "V6_QDRANT_UNAVAILABLE": "원본 Qdrant가 실행되지 않았거나 컬렉션을 읽을 수 없습니다.",
    "KOSIS_API_KEY_MISSING": "KOSIS API 키를 현재 터미널 환경에서 읽을 수 없습니다.",
    "NCP_CLOVASTUDIO_API_KEY_MISSING": "HCX API 키를 현재 터미널 환경에서 읽을 수 없습니다.",
}


class TerminalInputError(ValueError):
    pass


INPUT_BODY_AND_QUERY = "BODY_AND_QUERY"
INPUT_SUPPORTED_KHAN_URL_AND_QUERY = "SUPPORTED_KHAN_URL_AND_QUERY"
INPUT_QUERY_ONLY_SYNTHETIC_CLAIM = "QUERY_ONLY_SYNTHETIC_CLAIM"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def completion_receipt_path(output_root: Path) -> Path:
    return output_root.parent / f"{output_root.name}.completion_receipt.json"


def _case_binding(case_file: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    query = str(row.get("query") or "").strip()
    case_id = str(row.get("case_id") or "").strip()
    if not case_id or not query:
        raise TerminalInputError("CASE_FILE_ROW_INVALID")
    return {
        "case_id": case_id,
        "case_file_path": str(case_file.resolve()),
        "case_file_sha256": _sha256_bytes(case_file.read_bytes()),
        "case_canonical_sha256": canonical_sha256(dict(row)),
        "query_sha256": _sha256_bytes(query.encode("utf-8")),
    }


def load_case_file(case_file: str | Path, case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one unique case row and return its immutable binding evidence."""

    path = Path(case_file).resolve()
    if not path.is_file() or path.is_symlink():
        raise TerminalInputError("CASE_FILE_INVALID")
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TerminalInputError("CASE_FILE_ROW_INVALID")
            row_id = str(value.get("case_id") or "").strip()
            query = str(value.get("query") or "").strip()
            if not row_id or not query:
                raise TerminalInputError("CASE_FILE_ROW_INVALID")
            rows.append(value)
    except TerminalInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalInputError("CASE_FILE_INVALID") from exc
    ids = [str(row.get("case_id") or "").strip() for row in rows]
    if len(ids) != len(set(ids)):
        raise TerminalInputError("CASE_FILE_DUPLICATE_ID")
    selected = [row for row in rows if str(row.get("case_id") or "").strip() == str(case_id).strip()]
    if len(selected) != 1:
        raise TerminalInputError("CASE_ID_NOT_FOUND")
    row = dict(selected[0])
    return row, _case_binding(path, row)


def terminal_provenance_path(output_root: Path) -> Path:
    return output_root.parent / f"{output_root.name}.provenance.json"


def acquisition_root_for_output(output_root: Path) -> Path:
    """Keep URL artifacts beside, never inside, the empty trace root."""
    return output_root.parent / f"{output_root.name}.acquisition"


def _query_has_value_unit(query: str) -> bool:
    return any(row.get("kind") == "value_unit" for row in build_span_candidates(query))


def classify_input_mode(
    *, body: str | None, url: str | None, query: str | None,
    title: str = "", published_date: str = "", image_path: str = "",
) -> str:
    body_supplied = body is not None
    url_supplied = bool(str(url or "").strip())
    query_text = str(query or "").strip()
    if body_supplied and url_supplied:
        raise TerminalInputError("INPUT_MODE_CONFLICT_BODY_AND_URL")
    if url_supplied:
        if not query_text:
            raise TerminalInputError("CLAIM_QUERY_REQUIRED")
        if any(str(value or "").strip() for value in (title, published_date, image_path)):
            raise TerminalInputError("INPUT_MODE_CONFLICT_URL_METADATA")
        return INPUT_SUPPORTED_KHAN_URL_AND_QUERY
    if body_supplied:
        if not query_text:
            raise TerminalInputError("CLAIM_QUERY_REQUIRED")
        return INPUT_BODY_AND_QUERY
    if not query_text:
        raise TerminalInputError("CLAIM_QUERY_REQUIRED")
    if any(str(value or "").strip() for value in (title, published_date, image_path)):
        raise TerminalInputError("INPUT_MODE_CONFLICT_QUERY_ONLY_METADATA")
    if not _query_has_value_unit(query_text):
        raise TerminalInputError("QUERY_ONLY_NUMERIC_CLAIM_REQUIRED")
    return INPUT_QUERY_ONLY_SYNTHETIC_CLAIM


def read_multiline_body(input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print) -> str:
    """Read a pasted article body, ending at an empty line or EOF."""
    output_fn("기사 본문을 붙여넣으세요. 빈 줄에서 입력이 끝납니다.")
    lines: list[str] = []
    while True:
        try:
            line = input_fn("")
        except EOFError:
            break
        if not line:
            break
        lines.append(line)
    body = "\n".join(lines).strip()
    if not body:
        raise TerminalInputError("ARTICLE_BODY_REQUIRED")
    return body


def build_article(
    *, body: str, query: str, title: str = "", source_url: str = "", published_date: str = "",
    image_path: str = "", article_id: str | None = None, require_date: bool = True,
) -> dict[str, str]:
    """Build the existing live-article contract plus explicitly inert metadata."""
    body, query = str(body or "").strip(), str(query or "").strip()
    if not body:
        raise TerminalInputError("ARTICLE_BODY_REQUIRED")
    if not query:
        raise TerminalInputError("CLAIM_QUERY_REQUIRED")
    resolved_date = str(published_date or "").strip()
    if not resolved_date and require_date:
        raise TerminalInputError("ARTICLE_DATE_REQUIRED")
    if resolved_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_date):
        raise TerminalInputError("ARTICLE_DATE_INVALID")
    if resolved_date:
        try:
            date.fromisoformat(resolved_date)
        except ValueError as exc:
            raise TerminalInputError("ARTICLE_DATE_INVALID") from exc
    article = {
        "article_idx": str(article_id or f"terminal-{uuid.uuid4().hex[:12]}"),
        "title": str(title or query).strip(),
        "date": resolved_date[:10],
        "source_url": str(source_url or "").strip(),
        "article_text_field": "terminal_user_provided_article_body",
        "article_text": body,
        "claim_query": query,
        "article_image_path": str(image_path or "").strip(),
        "image_processing": "NOT_IMPLEMENTED_METADATA_ONLY",
    }
    if os.getenv("EVIDENCE_FIRST_STATISTICS_SHADOW_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    } and resolved_date:
        article["article_date_provenance"] = {
            "date_source": "client_asserted",
            "source_path": "terminal_argument",
            "date_field": "date",
            "article_text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
    return article


def input_path_for_output(output_root: Path) -> Path:
    """Keep input outside the trace root, which must start empty at L1."""
    return output_root.parent / f"{output_root.name}.articles.jsonl"


def write_terminal_input(article: Mapping[str, Any], output_root: Path) -> Path:
    path = input_path_for_output(output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise TerminalInputError("INPUT_OUTPUT_EXISTS")
    path.write_text(json.dumps(dict(article), ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_terminal_provenance(
    *, article: Mapping[str, Any], input_path: Path, output_root: Path,
    input_kind: str, acquisition_root: Path | None = None,
    case_binding: Mapping[str, Any] | None = None,
    terminal_invocation: Mapping[str, Any] | None = None,
) -> Path:
    path = terminal_provenance_path(output_root)
    if path.exists():
        raise TerminalInputError("PROVENANCE_OUTPUT_EXISTS")
    article_body = str(article.get("article_text") or "").encode("utf-8")
    provenance: dict[str, Any] = {
        "contract": "pipeline-terminal-provenance-v1",
        "input_kind": input_kind,
        "article_idx": str(article.get("article_idx") or ""),
        "terminal_input_sha256": _sha256_bytes(input_path.read_bytes()),
        "article_body_sha256": _sha256_bytes(article_body),
        "claim_query_sha256": _sha256_bytes(str(article.get("claim_query") or "").encode("utf-8")),
    }
    if case_binding is not None:
        provenance["case_binding"] = dict(case_binding)
    if terminal_invocation is not None:
        provenance["terminal_invocation"] = dict(terminal_invocation)
    if acquisition_root is not None:
        receipt_path = acquisition_root / "acquisition_receipt.json"
        frozen_path = acquisition_root / "frozen_article.jsonl"
        if not receipt_path.is_file() or not frozen_path.is_file():
            raise TerminalInputError("URL_ACQUISITION_PROVENANCE_MISSING")
        receipt_bytes = receipt_path.read_bytes()
        frozen_bytes = frozen_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        receipt_frozen_sha256 = str(receipt.get("frozen_article_sha256") or "")
        receipt_body_sha256 = str(
            ((receipt.get("extraction") or {}).get("article_text_sha256")) or ""
        )
        frozen_sha256 = _sha256_bytes(frozen_bytes)
        body_sha256 = _sha256_bytes(article_body)
        if receipt_frozen_sha256 != frozen_sha256 or receipt_body_sha256 != body_sha256:
            raise TerminalInputError("URL_ACQUISITION_PROVENANCE_MISMATCH")
        provenance["acquisition"] = {
            "root": str(acquisition_root.resolve()),
            "source_url": str(receipt.get("source_url") or ""),
            "final_url": str(receipt.get("final_url") or ""),
            "receipt_sha256": _sha256_bytes(receipt_bytes),
            "frozen_file_sha256": frozen_sha256,
            "article_body_sha256": body_sha256,
            "receipt_frozen_article_sha256": receipt_frozen_sha256,
            "receipt_article_body_sha256": receipt_body_sha256,
        }
    path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_terminal_provenance(output_root: Path) -> dict[str, Any]:
    path = terminal_provenance_path(output_root)
    if not path.is_file():
        raise TerminalInputError("RESUME_PROVENANCE_MISSING")
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TerminalInputError("RESUME_PROVENANCE_INVALID") from exc
    if not isinstance(provenance, dict):
        raise TerminalInputError("RESUME_PROVENANCE_INVALID")
    input_path = input_path_for_output(output_root)
    expected = str(provenance.get("terminal_input_sha256") or "")
    if not input_path.is_file() or expected != _sha256_bytes(input_path.read_bytes()):
        raise TerminalInputError("RESUME_INPUT_PROVENANCE_MISMATCH")
    acquisition = provenance.get("acquisition")
    if acquisition is not None:
        if not isinstance(acquisition, Mapping):
            raise TerminalInputError("RESUME_PROVENANCE_INVALID")
        root = Path(str(acquisition.get("root") or ""))
        receipt_path, frozen_path = root / "acquisition_receipt.json", root / "frozen_article.jsonl"
        if not receipt_path.is_file() or not frozen_path.is_file():
            raise TerminalInputError("RESUME_ACQUISITION_PROVENANCE_MISSING")
        receipt_bytes, frozen_bytes = receipt_path.read_bytes(), frozen_path.read_bytes()
        rows = _rows(input_path)
        article_body_sha256 = _sha256_bytes(str((rows[0] if rows else {}).get("article_text") or "").encode("utf-8"))
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        if (
            str(acquisition.get("receipt_sha256") or "") != _sha256_bytes(receipt_bytes)
            or str(acquisition.get("frozen_file_sha256") or "") != _sha256_bytes(frozen_bytes)
            or str(acquisition.get("article_body_sha256") or "") != article_body_sha256
            or str(receipt.get("frozen_article_sha256") or "") != _sha256_bytes(frozen_bytes)
            or str(((receipt.get("extraction") or {}).get("article_text_sha256")) or "") != article_body_sha256
        ):
            raise TerminalInputError("RESUME_ACQUISITION_PROVENANCE_MISMATCH")
    return provenance


def default_output_root(parent: Path = DEFAULT_OUTPUT_PARENT) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return parent / f"terminal_{stamp}_{uuid.uuid4().hex[:8]}"


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_terminal_input(output_root: Path) -> dict[str, Any]:
    path = input_path_for_output(output_root)
    rows = _rows(path)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise TerminalInputError("RESUME_INPUT_INVALID")
    return rows[0]


def _record_for_tree(path: Path, relative: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "kind": "file",
        "path": relative.replace("\\", "/"),
        "sha256": _sha256_bytes(data),
        "bytes": len(data),
    }


def _expected_trace_inventory(output_root: Path, manifests: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for stage in _TRACE_STAGES:
        manifest = manifests[stage]
        for section in ("data_payloads", "sealed_logs", "runtime_payloads"):
            values = manifest.get(section) or {}
            if not isinstance(values, Mapping):
                raise TerminalInputError("MANIFEST_INVALID")
            for raw_name in values:
                name = str(raw_name).replace("\\", "/")
                path = output_root / Path(name)
                if not path.is_file():
                    raise TerminalInputError("MANIFEST_INVALID")
                files[name] = _record_for_tree(path, name)
        manifest_name = f"{stage}_manifest.json"
        manifest_path = output_root / manifest_name
        if not manifest_path.is_file():
            raise TerminalInputError("MANIFEST_INVALID")
        files[manifest_name] = _record_for_tree(manifest_path, manifest_name)
    directories: set[str] = set()
    for name in files:
        parent = Path(name).parent
        while str(parent) not in {"", "."}:
            directories.add(parent.as_posix())
            parent = parent.parent
    result = [*files.values(), *({"kind": "directory", "path": name} for name in directories)]
    return sorted(result, key=lambda item: (str(item["path"]), str(item["kind"])))


def _load_trace_manifests(
    output_root: Path,
    *,
    article_path: Path,
    article: Mapping[str, Any],
    terminal_invocation: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    manifests: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    articles = [dict(article)]
    previous_sha: str | None = None
    for stage in _TRACE_STAGES:
        path = output_root / f"{stage}_manifest.json"
        if not path.is_file():
            raise TerminalInputError("MISSING_STAGE_MANIFEST")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TerminalInputError("MANIFEST_INVALID") from exc
        if not isinstance(manifest, dict):
            raise TerminalInputError("MANIFEST_INVALID")
        try:
            actual_sha = _validate_manifest(
                output_root, manifest, stage, article_path, articles, terminal_invocation,
            )
        except TraceStageError as exc:
            raise TerminalInputError(str(exc.args[0]) if exc.args else "MANIFEST_INVALID") from exc
        recorded_previous = manifest.get("predecessor_manifest_sha256")
        if stage == "01":
            if recorded_previous not in (None, ""):
                raise TerminalInputError("SHA_MISMATCH")
        elif str(recorded_previous or "") != str(previous_sha or ""):
            raise TerminalInputError("SHA_MISMATCH")
        if dict(manifest.get("terminal_invocation") or {}) != dict(terminal_invocation):
            raise TerminalInputError("INVOCATION_MISMATCH")
        manifests[stage] = manifest
        hashes[f"{stage}_manifest.json"] = actual_sha
        previous_sha = actual_sha
    return manifests, hashes


def _code_attestation() -> dict[str, Any]:
    paths = {name: path.resolve() for name, path in _CODE_PATHS.items()}
    result: dict[str, Any] = {
        "files": {name: sha256_file(path) for name, path in paths.items()},
        "validator_function": ast_top_level_function_sha256(
            paths["answer"], "validate_and_render_answer", through_next_top_level_function=True,
        ),
        "operational_source_blocks": {
            name: ast_top_level_function_sha256(paths["operational"], name)
            for name in ("resolve_top50", "fetch_exact_single_cell", "compare_official_cell", "run_new_articles_v2")
        },
    }
    return result


_CASE_INVOCATION_KEYS = {
    "invocation_id", "resume", "stage_request", "case_id",
    "case_file_sha256", "case_canonical_sha256", "query_sha256", "started_at_utc",
}


def _validate_case_invocation(
    case_binding: Mapping[str, Any], terminal_invocation: Mapping[str, Any],
) -> None:
    """Require one exact, non-resumable all-stage invocation bound to the case."""

    binding = dict(case_binding)
    invocation = dict(terminal_invocation)
    if set(invocation) != _CASE_INVOCATION_KEYS:
        raise TerminalInputError("INVOCATION_MISMATCH")
    expected = {
        "resume": False,
        "stage_request": "all",
        "case_id": binding.get("case_id"),
        "case_file_sha256": binding.get("case_file_sha256"),
        "case_canonical_sha256": binding.get("case_canonical_sha256"),
        "query_sha256": binding.get("query_sha256"),
    }
    if any(invocation.get(key) != value for key, value in expected.items()):
        raise TerminalInputError("INVOCATION_MISMATCH")
    try:
        invocation_id = uuid.UUID(str(invocation.get("invocation_id") or ""))
        started = datetime.fromisoformat(str(invocation.get("started_at_utc") or ""))
    except (ValueError, TypeError) as exc:
        raise TerminalInputError("INVOCATION_MISMATCH") from exc
    if str(invocation_id) != invocation.get("invocation_id") or started.tzinfo is None or started.utcoffset() != timezone.utc.utcoffset(started):
        raise TerminalInputError("INVOCATION_MISMATCH")


def _validate_case_normalized_argv(
    normalized_argv: Any,
    *,
    case_binding: Mapping[str, Any],
    output_root: Path,
    config_path: Path,
) -> None:
    """Validate the semantic case-mode command recorded by the receipt."""

    if not isinstance(normalized_argv, list) or not all(isinstance(value, str) and value for value in normalized_argv):
        raise TerminalInputError("NORMALIZED_ARGV_INVALID")
    value_options = {"--case-file", "--case-id", "--output", "--config", "--stage"}
    flag_options = {"--no-start-local-services", "--resume"}
    parsed: dict[str, Any] = {}
    index = 0
    while index < len(normalized_argv):
        token = normalized_argv[index]
        option, separator, inline_value = token.partition("=")
        if option in value_options:
            if option in parsed:
                raise TerminalInputError("NORMALIZED_ARGV_INVALID")
            if separator:
                value = inline_value
            else:
                index += 1
                if index >= len(normalized_argv) or normalized_argv[index].startswith("--"):
                    raise TerminalInputError("NORMALIZED_ARGV_INVALID")
                value = normalized_argv[index]
            if not value:
                raise TerminalInputError("NORMALIZED_ARGV_INVALID")
            parsed[option] = value
        elif option in flag_options and not separator:
            if option in parsed:
                raise TerminalInputError("NORMALIZED_ARGV_INVALID")
            parsed[option] = True
        else:
            raise TerminalInputError("NORMALIZED_ARGV_INVALID")
        index += 1
    required = {"--case-file", "--case-id", "--output", "--config", "--no-start-local-services"}
    if not required.issubset(parsed) or parsed.get("--resume") is True:
        raise TerminalInputError("NORMALIZED_ARGV_INVALID")
    if parsed.get("--stage", "all") != "all":
        raise TerminalInputError("NORMALIZED_ARGV_INVALID")
    binding = dict(case_binding)
    if (
        Path(str(parsed["--case-file"])).resolve() != Path(str(binding.get("case_file_path") or "")).resolve()
        or parsed["--case-id"] != binding.get("case_id")
        or Path(str(parsed["--output"])).resolve() != output_root.resolve()
        or Path(str(parsed["--config"])).resolve() != config_path.resolve()
    ):
        raise TerminalInputError("NORMALIZED_ARGV_MISMATCH")


def validate_completion_receipt(
    output_root: str | Path,
    *,
    expected_case_binding: Mapping[str, Any] | None = None,
    expected_invocation: Mapping[str, Any] | None = None,
    expected_config_path: str | Path | None = None,
    expected_config_sha256: str | None = None,
) -> dict[str, Any]:
    """Recompute a terminal completion receipt without publishing anything."""

    root = Path(output_root).resolve()
    receipt_path = completion_receipt_path(root)
    if not root.is_dir() or not receipt_path.is_file():
        raise TerminalInputError("COMPLETION_RECEIPT_MISSING")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalInputError("COMPLETION_RECEIPT_INVALID") from exc
    if not isinstance(receipt, dict) or receipt.get("contract") != COMPLETION_RECEIPT_CONTRACT:
        raise TerminalInputError("COMPLETION_RECEIPT_INVALID")
    article_path = input_path_for_output(root)
    provenance_path = terminal_provenance_path(root)
    if not article_path.is_file() or not provenance_path.is_file():
        raise TerminalInputError("COMPLETION_INPUT_MISSING")
    article = load_terminal_input(root)
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalInputError("PROVENANCE_INVALID") from exc
    if not isinstance(provenance, dict):
        raise TerminalInputError("PROVENANCE_INVALID")
    binding = provenance.get("case_binding")
    invocation = provenance.get("terminal_invocation")
    if not isinstance(binding, Mapping) or not isinstance(invocation, Mapping):
        raise TerminalInputError("CASE_BINDING_MISSING")
    binding = dict(binding)
    invocation = dict(invocation)
    if expected_case_binding is not None and binding != dict(expected_case_binding):
        raise TerminalInputError("CASE_BINDING_MISMATCH")
    if expected_invocation is not None and invocation != dict(expected_invocation):
        raise TerminalInputError("INVOCATION_MISMATCH")
    case_file = Path(str(binding.get("case_file_path") or "")).resolve()
    try:
        selected, recalculated_binding = load_case_file(case_file, str(binding.get("case_id") or ""))
    except TerminalInputError:
        raise
    if recalculated_binding != binding:
        raise TerminalInputError("CASE_BINDING_MISMATCH")
    _validate_case_invocation(recalculated_binding, invocation)
    query = str(selected.get("query") or "").strip()
    if (
        str(article.get("article_idx") or "") != str(binding.get("case_id") or "")
        or str(article.get("claim_query") or "") != query
        or str(article.get("article_text") or "") != query
        or str(article.get("input_kind") or "") != INPUT_QUERY_ONLY_SYNTHETIC_CLAIM
        or dict(article.get("case_binding") or {}) != binding
        or dict(article.get("terminal_invocation") or {}) != invocation
    ):
        raise TerminalInputError("CASE_INPUT_MISMATCH")
    if dict(provenance.get("case_binding") or {}) != binding or dict(provenance.get("terminal_invocation") or {}) != invocation:
        raise TerminalInputError("PROVENANCE_BINDING_MISMATCH")
    config_record = receipt.get("config") if isinstance(receipt.get("config"), Mapping) else {}
    config_path = Path(str(config_record.get("path") or "")).resolve()
    if expected_config_path is not None and config_path != Path(expected_config_path).resolve():
        raise TerminalInputError("CONFIG_PATH_MISMATCH")
    if not config_path.is_file() or str(config_record.get("sha256") or "") != sha256_file(config_path):
        raise TerminalInputError("CONFIG_SHA_MISMATCH")
    if expected_config_sha256 is not None and sha256_file(config_path) != str(expected_config_sha256):
        raise TerminalInputError("CONFIG_SHA_MISMATCH")
    _validate_case_normalized_argv(
        receipt.get("normalized_argv"),
        case_binding=recalculated_binding,
        output_root=root,
        config_path=config_path,
    )
    manifests, manifest_hashes = _load_trace_manifests(
        root, article_path=article_path, article=article, terminal_invocation=invocation,
    )
    expected_inventory = _expected_trace_inventory(root, manifests)
    try:
        actual_inventory = validate_exact_tree(
            root, expected_inventory,
            expected_inventory_sha256=str(receipt.get("tree_inventory_sha256") or "") or None,
        )
    except SealingError as exc:
        raise TerminalInputError(str(exc.args[0]) if exc.args else "TREE_INVALID") from exc
    code = _code_attestation()
    input_record = {
        "path": str(article_path.resolve()),
        "sha256": _sha256_bytes(article_path.read_bytes()),
    }
    provenance_record = {
        "path": str(provenance_path.resolve()),
        "sha256": _sha256_bytes(provenance_path.read_bytes()),
    }
    if receipt.get("input") != input_record or receipt.get("provenance") != provenance_record:
        raise TerminalInputError("COMPLETION_INPUT_PROVENANCE_MISMATCH")
    if receipt.get("case_binding") != binding or receipt.get("terminal_invocation") != invocation:
        raise TerminalInputError("COMPLETION_RECEIPT_BINDING_MISMATCH")
    if dict(receipt.get("stage_manifest_sha256") or {}) != manifest_hashes:
        raise TerminalInputError("COMPLETION_MANIFEST_MISMATCH")
    if receipt.get("code") != code:
        raise TerminalInputError("CODE_ATTESTATION_MISMATCH")
    if receipt.get("exact_tree_inventory") != actual_inventory:
        raise TerminalInputError("TREE_INVENTORY_MISMATCH")
    if receipt.get("tree_inventory_sha256") != inventory_sha256(actual_inventory):
        raise TerminalInputError("TREE_INVENTORY_MISMATCH")
    return receipt


def seal_terminal_completion(
    *,
    article: Mapping[str, Any],
    article_path: Path,
    output_root: Path,
    config_path: Path,
    terminal_invocation: Mapping[str, Any],
    case_binding: Mapping[str, Any],
    argv: list[str],
) -> Path:
    """Validate the complete four-stage tree and publish one sibling receipt."""

    receipt_path = completion_receipt_path(output_root)
    if receipt_path.exists() or receipt_path.is_symlink():
        raise TerminalInputError("COMPLETION_RECEIPT_EXISTS")
    if not output_root.is_dir():
        raise TerminalInputError("OUTPUT_MISSING")
    if dict(article.get("case_binding") or {}) != dict(case_binding):
        raise TerminalInputError("CASE_INPUT_MISMATCH")
    if dict(article.get("terminal_invocation") or {}) != dict(terminal_invocation):
        raise TerminalInputError("INVOCATION_MISMATCH")
    selected, recalculated_binding = load_case_file(
        Path(str(case_binding.get("case_file_path") or "")), str(case_binding.get("case_id") or ""),
    )
    if recalculated_binding != dict(case_binding) or str(selected.get("query") or "").strip() != str(article.get("claim_query") or ""):
        raise TerminalInputError("CASE_BINDING_MISMATCH")
    _validate_case_invocation(recalculated_binding, terminal_invocation)
    normalized_argv = [str(value).replace("\\", "/") for value in argv]
    _validate_case_normalized_argv(
        normalized_argv,
        case_binding=recalculated_binding,
        output_root=output_root,
        config_path=config_path,
    )
    manifests, manifest_hashes = _load_trace_manifests(
        output_root,
        article_path=article_path,
        article=article,
        terminal_invocation=terminal_invocation,
    )
    expected_inventory = _expected_trace_inventory(output_root, manifests)
    try:
        actual_inventory = validate_exact_tree(output_root, expected_inventory)
    except SealingError as exc:
        raise TerminalInputError(str(exc.args[0]) if exc.args else "TREE_INVALID") from exc
    code = _code_attestation()
    input_record = {"path": str(article_path.resolve()), "sha256": _sha256_bytes(article_path.read_bytes())}
    provenance_path = terminal_provenance_path(output_root)
    if not provenance_path.is_file():
        raise TerminalInputError("PROVENANCE_OUTPUT_MISSING")
    provenance_record = {"path": str(provenance_path.resolve()), "sha256": _sha256_bytes(provenance_path.read_bytes())}
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise TerminalInputError("CONFIG_MISSING")
    receipt = {
        "contract": COMPLETION_RECEIPT_CONTRACT,
        "terminal_invocation": dict(terminal_invocation),
        "case_binding": dict(case_binding),
        "input": input_record,
        "provenance": provenance_record,
        "config": {"path": str(config_path), "sha256": sha256_file(config_path)},
        "code": code,
        "stage_manifest_sha256": manifest_hashes,
        "exact_tree_inventory": actual_inventory,
        "tree_inventory_sha256": inventory_sha256(actual_inventory),
        "normalized_argv": normalized_argv,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        atomic_publish_json(receipt_path, receipt)
    except SealingError as exc:
        raise TerminalInputError(str(exc.args[0]) if exc.args else "COMPLETION_RECEIPT_PUBLISH_FAILED") from exc
    return receipt_path


def print_terminal_summary(output_root: Path, output_fn: Callable[[str], None] = print) -> None:
    """Print stable, compact stage observations without exposing runtime secrets."""
    l1_sentences = _rows(output_root / "01_sentences.jsonl")
    l1_values = _rows(output_root / "01_value_candidates.jsonl")
    l2_results = _rows(output_root / "02_l2_results.jsonl")
    routed = _rows(output_root / "03_routed.jsonl")
    ledgers = _rows(output_root / "04_stage_ledger.jsonl")
    answers = _rows(output_root / "04_answers.jsonl")
    value_units = sum(1 for row in l1_values if row.get("kind") == "value_unit")
    output_fn(f"[SUMMARY] L1 sentences={len(l1_sentences)} values={value_units}")
    if l2_results:
        output_fn(f"[SUMMARY] L2 articles={len(l2_results)} statuses={','.join(str(row.get('status') or '') for row in l2_results)}")
    if routed:
        output_fn(f"[SUMMARY] L3-L5 routed_targets={len(routed)}")
    for ledger in ledgers:
        resolution = ledger.get("resolution") if isinstance(ledger.get("resolution"), Mapping) else {}
        cell = ledger.get("cell") if isinstance(ledger.get("cell"), Mapping) else {}
        recovery = ledger.get("failure_recovery_shadow") if isinstance(ledger.get("failure_recovery_shadow"), Mapping) else {}
        intent = ledger.get("user_intent_shadow") if isinstance(ledger.get("user_intent_shadow"), Mapping) else {}
        if intent and intent.get("status") != "DISABLED":
            output_fn(
                "[INTENT] "
                f"task={intent.get('task_intent') or ''} "
                f"measurements={','.join(str(value) for value in intent.get('measurement_intents') or [])} "
                f"status={intent.get('status') or ''}"
            )
            for question in intent.get("questions") or []:
                if isinstance(question, Mapping):
                    output_fn(f"[INTENT_CLARIFICATION] {question.get('prompt') or ''}")
        output_fn(
            "[RESULT] "
            f"target={ledger.get('value_span_id') or ledger.get('target_id') or ''} "
            f"resolution={resolution.get('outcome') or ledger.get('resolution') or ''} "
            f"table={resolution.get('chosen_table_key') or ''} "
            f"cell_status={cell.get('status') or ''} "
            f"official_value={(cell.get('cell') or {}).get('DT') or ''}"
        )
        action = str(recovery.get("action") or "")
        if action in {"ASK_USER", "ASK_USER_AFTER_CORRECTION"}:
            question = recovery.get("question") if isinstance(recovery.get("question"), Mapping) else {}
            output_fn(f"[CLARIFICATION] {question.get('prompt') or '통계 기준을 더 구체적으로 알려주세요.'}")
            options = question.get("options") if isinstance(question.get("options"), list) else []
            if options:
                output_fn("[CLARIFICATION_OPTIONS] " + " | ".join(str(value) for value in options))
        elif action == "CORRECTIVE_RETRIEVAL":
            output_fn(
                "[CORRECTION] "
                f"cases={','.join(str(value) for value in recovery.get('case_ids') or [])} "
                f"retry={recovery.get('retry_budget', {}).get('used', 0)}/1 "
                f"recovered={str(bool(recovery.get('recovered'))).lower()}"
            )
        elif action == "SKIP":
            output_fn("[CORRECTION] skipped=QUERY_READY")
    for answer in answers:
        output_fn(f"[ANSWER] verdict={answer.get('verdict') or ''} headline={answer.get('headline') or ''}")
        output_fn(f"[ANSWER] {answer.get('explanation') or answer.get('answer') or ''}")
        if answer.get("limitation"):
            output_fn(f"[LIMITATION] {answer['limitation']}")
    output_fn(f"[OUTPUT] {output_root.resolve()}")
    output_fn("[FILES] 01~04 trace logs, manifests, and JSONL results are in this directory. The input JSONL path is printed above.")


def annual_requery_path(output_root: Path) -> Path:
    return output_root.parent / f"{output_root.name}.annual_requery.json"


def intent_requires_annual_requery(output_root: Path) -> bool:
    for ledger in _rows(output_root / "04_stage_ledger.jsonl"):
        intent = ledger.get("user_intent_shadow") if isinstance(ledger.get("user_intent_shadow"), Mapping) else {}
        execution = intent.get("execution_plan") if isinstance(intent.get("execution_plan"), Mapping) else {}
        resolution = ledger.get("resolution") if isinstance(ledger.get("resolution"), Mapping) else {}
        if execution.get("annual_requery_required") is True and resolution.get("outcome") == "QUERY_READY":
            return True
    return False


def _official_unit_from_ledger(ledger: Mapping[str, Any]) -> str:
    chosen = str((ledger.get("resolution") or {}).get("chosen_table_key") or "")
    for projection in ledger.get("projections") or []:
        if not isinstance(projection, Mapping) or str(projection.get("table_key") or "") != chosen:
            continue
        for assignment in projection.get("assignments") or []:
            if not isinstance(assignment, Mapping):
                continue
            for binding in assignment.get("bindings") or []:
                if not isinstance(binding, Mapping) or binding.get("axis_kind") != "UNIT":
                    continue
                evidence = binding.get("evidence") if isinstance(binding.get("evidence"), Mapping) else {}
                unit = str(evidence.get("profile_label") or binding.get("value_id") or "")
                if unit:
                    return unit
    return ""


def run_annual_requery_shadow(
    output_root: Path, *, cell_fetcher: Callable[[dict[str, Any]], Any] | None = None,
    output_fn: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Append one sibling result without changing the sealed 01~04 trace."""
    routed = _rows(output_root / "03_routed.jsonl")
    ledgers = [
        row for row in _rows(output_root / "04_stage_ledger.jsonl")
        if isinstance(row.get("resolution"), Mapping)
        and row["resolution"].get("outcome") == "QUERY_READY"
        and isinstance(row.get("cell"), Mapping)
        and row["cell"].get("status") == "CELL_RESOLVED"
    ]
    if len(ledgers) != 1:
        raise AnnualRequeryError(f"CURRENT_QUERY_READY_NOT_UNIQUE:{len(ledgers)}")
    ledger = ledgers[0]
    if cell_fetcher is None:
        from src.news_verification.runtime.operational_live_adapters_v2 import FailClosedCellFetcher
        from src.news_verification.runtime.kosis_client import get_data_from_query
        cell_fetcher = FailClosedCellFetcher(get_data_from_query)
    result = verify_annual_requery(
        rows=routed,
        current_plan=ledger["resolution"]["query_plan"],
        current_cell_result=ledger["cell"],
        current_target_id=str(ledger.get("value_span_id") or ""),
        cell_fetcher=cell_fetcher,
        official_unit=_official_unit_from_ledger(ledger),
    )
    path = annual_requery_path(output_root)
    if path.exists():
        raise AnnualRequeryError("ANNUAL_REQUERY_OUTPUT_EXISTS")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    official = result["official"]
    output_fn(
        "[REQUERY] "
        f"baseline={official['baseline']}{official['level_unit']} "
        f"current={official['current']}{official['level_unit']} "
        f"change={official['signed_change']}{official['change_unit']}"
    )
    output_fn(
        "[REQUERY] "
        f"level={result['components']['current_level']['verdict']} "
        f"change={result['components']['change']['verdict']} overall={result['verdict']}"
    )
    output_fn(f"[REQUERY_OUTPUT] {path.resolve()}")
    return result


def print_preflight_blocked(
    blockers: list[str], *, output_root: Path, role_aware_dimension_shadow: bool,
    output_fn: Callable[[str], None] = print,
) -> None:
    output_fn("[PREFLIGHT] 차단됨: " + ",".join(blockers))
    for blocker in blockers:
        output_fn(f"[원인] {blocker}: {BLOCKER_GUIDANCE.get(blocker, '사전점검 계약을 충족하지 못했습니다.')}")
    shadow = " --role-aware-dimension-shadow" if role_aware_dimension_shadow else ""
    output_fn(
        "[재개] 환경을 복구한 뒤 다음 명령으로 live 단계만 이어서 실행할 수 있습니다: "
        f".\\scripts\\파이프라인_터미널_실행.ps1 --resume \"{output_root}\" --stage live{shadow}"
    )
    output_fn("[보존] L1, L2, L3-L5 결과는 삭제하지 않았습니다.")


def ensure_live_services(
    *, config_path: Path, output_root: Path, services: LocalPipelineServices | None,
    no_start_local_services: bool, announce: bool, deterministic_answer_only: bool = False,
) -> tuple[dict[str, Any], LocalPipelineServices | None, bool]:
    """Check the three local retrieval services and start only missing ones.

    The returned boolean is true only when this call had to start a service.
    Callers use it to reset the answer-runtime clock after an unexpected service
    restart, rather than mixing separate ready periods into one duration.
    """
    if announce:
        print("[SERVICE] Qdrant·질의 encoder·reranker 기동 상태를 확인합니다.")
    preflight_kwargs = {"check_service": True}
    if deterministic_answer_only:
        preflight_kwargs["allow_deterministic_answer_only"] = True
    gate = preflight(config_path, **preflight_kwargs)
    blockers = [str(value) for value in (gate.get("blockers") or [])]
    service_blockers = [value for value in blockers if value in SERVICE_BLOCKERS]
    started_here = False
    if service_blockers and not no_start_local_services:
        if services is None:
            services = LocalPipelineServices(
                repo_root=Path.cwd().resolve(), config_path=config_path.resolve(),
                log_root=output_root.parent / f"{output_root.name}.local_services",
            )
        services.start_missing(service_blockers)
        services.wait_until_ready()
        started_here = True
        gate = preflight(config_path, **preflight_kwargs)
        blockers = [str(value) for value in (gate.get("blockers") or [])]
        service_blockers = [value for value in blockers if value in SERVICE_BLOCKERS]
    if not service_blockers:
        print("[SERVICE] Qdrant·질의 encoder·reranker 준비 완료")
    return dict(gate), services, started_here


def _run_stage(
    stage: str, *, article_path: Path, output_root: Path, config_path: Path,
    role_aware_dimension_shadow: bool, claim_query: str, failure_recovery_shadow: bool,
    user_intent_shadow: bool, terminal_invocation: Mapping[str, Any] | None = None,
) -> None:
    labels = {"l1": "L1", "l2": "L2", "layers": "L3-L5", "live": "live", "all": "all"}
    print(f"[STAGE] {labels[stage]} 시작")
    started_at = time.monotonic()
    run_trace(
        articles_path=article_path, output_root=output_root, stage=stage, config_path=config_path,
        role_aware_dimension_shadow=role_aware_dimension_shadow, claim_query=claim_query,
        failure_recovery_shadow=failure_recovery_shadow,
        user_intent_shadow=user_intent_shadow,
        terminal_invocation=terminal_invocation,
    )
    print(f"[STAGE] {labels[stage]} 완료 ({time.monotonic() - started_at:.2f}초)")


def _interactive_value(label: str, default: str = "", input_fn: Callable[[str], str] = input) -> str:
    suffix = f" [{default}]" if default else ""
    value = input_fn(label + suffix + ": ").strip()
    return value or default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="기사 본문 1건을 단계별로 실행하고 터미널에 결과를 요약합니다.")
    parser.add_argument("--body", help="기사 본문. 없으면 터미널 붙여넣기 입력을 사용합니다.")
    parser.add_argument("--query", help="검증할 통계 주장 질의. 없으면 터미널에서 묻습니다.")
    parser.add_argument("--title", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--date", dest="published_date", default="", help="기사 발행일 YYYY-MM-DD (필수)")
    parser.add_argument("--image", dest="image_path", default="", help="현재는 저장만 하며 이미지 분석에는 사용하지 않습니다.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-parent", type=Path, default=DEFAULT_OUTPUT_PARENT)
    parser.add_argument("--resume", type=Path, help="기존 terminal run 폴더의 다음 단계를 실행합니다.")
    parser.add_argument("--case-file", type=Path, help="v3 frozen case JSONL path")
    parser.add_argument("--case-id", help="v3 frozen case identifier")
    parser.add_argument("--stage", choices=("l1", "l2", "layers", "live", "all"), default="all")
    parser.add_argument("--config", type=Path, default=Path("configs/pipeline_operational_v2.json"))
    parser.add_argument("--role-aware-dimension-shadow", action="store_true")
    parser.add_argument(
        "--annual-requery-shadow", action="store_true",
        help="확정된 연간 series에서 기준연도 셀 1건을 재질의해 수준·증감 주장을 함께 검증합니다.",
    )
    parser.add_argument(
        "--failure-recovery-shadow", action="store_true",
        help="결합 모호성에는 사용자 질문을 만들고, 후보 누락 의심에는 실패사례 기반 재검색을 1회 수행합니다.",
    )
    parser.add_argument(
        "--user-intent-shadow", action="store_true",
        help="사용자 작업·측정 의도를 먼저 분류하고, 조건 부족 시 검색 전에 직접 질문합니다.",
    )
    parser.add_argument(
        "--no-start-local-services", action="store_true",
        help="실행 시작 시 원본 Qdrant/encoder/reranker 자동 기동을 끕니다.",
    )
    args = parser.parse_args(argv)
    services: LocalPipelineServices | None = None
    output_root: Path | None = None
    answer_runtime_started_at: float | None = None
    case_mode = bool(args.case_file is not None or args.case_id is not None)
    case_binding: dict[str, Any] | None = None
    terminal_invocation: dict[str, Any] | None = None
    try:
        if args.annual_requery_shadow and args.stage not in {"all", "live"}:
            raise TerminalInputError("ANNUAL_REQUERY_REQUIRES_LIVE")
        if case_mode:
            if args.case_file is None or not str(args.case_id or "").strip():
                raise TerminalInputError("CASE_FILE_AND_ID_REQUIRED")
            if args.stage != "all":
                raise TerminalInputError("CASE_STAGE_ALL_REQUIRED")
            if not args.no_start_local_services:
                raise TerminalInputError("CASE_NO_START_REQUIRED")
            if args.role_aware_dimension_shadow or args.annual_requery_shadow or args.failure_recovery_shadow or args.user_intent_shadow:
                raise TerminalInputError("CASE_SHADOWS_FORBIDDEN")
            if any(
                value is not None and (not isinstance(value, str) or value.strip())
                for value in (args.body, args.query, args.url, args.resume, args.title, args.published_date, args.image_path)
            ):
                raise TerminalInputError("CASE_INPUT_CONFLICT")
            if args.output is None:
                raise TerminalInputError("CASE_OUTPUT_REQUIRED")
            output_root = args.output.resolve()
            if output_root.exists():
                raise TerminalInputError("OUTPUT_EXISTS")
            receipt_path = completion_receipt_path(output_root)
            if receipt_path.exists() or receipt_path.is_symlink():
                raise TerminalInputError("COMPLETION_RECEIPT_EXISTS")
            if input_path_for_output(output_root).exists() or terminal_provenance_path(output_root).exists():
                raise TerminalInputError("INPUT_OUTPUT_EXISTS")
            case_row, case_binding = load_case_file(args.case_file, str(args.case_id))
            query = str(case_row.get("query") or "").strip()
            terminal_invocation = {
                "invocation_id": str(uuid.uuid4()),
                "resume": False,
                "stage_request": "all",
                "case_id": str(case_binding["case_id"]),
                "case_file_sha256": str(case_binding["case_file_sha256"]),
                "case_canonical_sha256": str(case_binding["case_canonical_sha256"]),
                "query_sha256": str(case_binding["query_sha256"]),
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            article = build_article(
                body=query,
                query=query,
                title=query,
                published_date=str(case_row.get("date") or ""),
                article_id=str(case_binding["case_id"]),
                require_date=False,
            )
            article["input_kind"] = INPUT_QUERY_ONLY_SYNTHETIC_CLAIM
            article["case_binding"] = dict(case_binding)
            article["terminal_invocation"] = dict(terminal_invocation)
            article_path = write_terminal_input(article, output_root)
            _write_terminal_provenance(
                article=article,
                input_path=article_path,
                output_root=output_root,
                input_kind=INPUT_QUERY_ONLY_SYNTHETIC_CLAIM,
                case_binding=case_binding,
                terminal_invocation=terminal_invocation,
            )
        elif args.resume is not None:
            if any(
                value is not None and (not isinstance(value, str) or value.strip())
                for value in (args.output, args.body, args.query, args.url, args.title, args.published_date, args.image_path)
            ):
                raise TerminalInputError("RESUME_CANNOT_REPLACE_INPUT")
            if args.stage in {"l1", "all"}:
                raise TerminalInputError("RESUME_REQUIRES_L2_LAYERS_OR_LIVE")
            output_root = args.resume.resolve()
            if not output_root.is_dir():
                raise TerminalInputError("RESUME_RUN_NOT_FOUND")
            load_terminal_provenance(output_root)
        else:
            if args.stage not in {"l1", "all"}:
                raise TerminalInputError("NEW_RUN_REQUIRES_L1_OR_ALL")
            if args.body is None and not args.url and args.query is None:
                body = read_multiline_body()
                query = _interactive_value("검증 질의")
                title = _interactive_value("기사 제목 (선택)")
                source_url = ""
                published_date = _interactive_value("기사 발행일 YYYY-MM-DD")
                input_kind = classify_input_mode(
                    body=body, url=source_url, query=query, title=title,
                    published_date=published_date, image_path=args.image_path,
                )
            else:
                body = args.body
                query = args.query
                input_kind = classify_input_mode(
                    body=body, url=args.url, query=query, title=args.title,
                    published_date=args.published_date, image_path=args.image_path,
                )
                title, source_url, published_date = args.title, args.url, args.published_date
            output_root = (args.output or default_output_root(args.output_parent)).resolve()
            if output_root.exists():
                raise TerminalInputError("OUTPUT_EXISTS")
            acquisition_root: Path | None = None
            if input_kind == INPUT_SUPPORTED_KHAN_URL_AND_QUERY:
                acquisition_root = acquisition_root_for_output(output_root)
                frozen_path, _receipt = acquire_article_url(args.url.strip(), acquisition_root)
                frozen_rows = _rows(frozen_path)
                if len(frozen_rows) != 1 or not isinstance(frozen_rows[0], dict):
                    raise TerminalInputError("URL_FROZEN_ARTICLE_INVALID")
                article = dict(frozen_rows[0])
                article.update({
                    "claim_query": str(query or "").strip(),
                    "article_text_field": "terminal_acquired_article_body",
                    "article_image_path": "",
                    "image_processing": "NOT_IMPLEMENTED_METADATA_ONLY",
                    "input_kind": input_kind,
                })
            else:
                if input_kind == INPUT_QUERY_ONLY_SYNTHETIC_CLAIM:
                    body = str(query or "").strip()
                article = build_article(
                    body=str(body or ""), query=str(query or ""), title=title,
                    source_url=source_url, published_date=published_date,
                    image_path=args.image_path,
                    require_date=input_kind != INPUT_QUERY_ONLY_SYNTHETIC_CLAIM,
                )
                article["input_kind"] = input_kind
            article_path = write_terminal_input(article, output_root)
            _write_terminal_provenance(
                article=article, input_path=article_path, output_root=output_root,
                input_kind=input_kind, acquisition_root=acquisition_root,
            )

        if args.resume is not None:
            article = load_terminal_input(output_root)
            article_path = input_path_for_output(output_root)
        if article.get("article_image_path"):
            print("[IMAGE] 현재 파이프라인은 이미지 분석을 하지 않습니다. 경로만 입력 파일에 기록했습니다.")
        print(f"[INPUT] article_id={article['article_idx']} date={article['date']} query={article['claim_query']}")
        print(f"[INPUT_FILE] {article_path.resolve()}")
        if case_mode:
            gate, services, started_here = ensure_live_services(
                config_path=args.config, output_root=output_root, services=services,
                no_start_local_services=args.no_start_local_services, announce=True,
                deterministic_answer_only=True,
            )
            blockers = [str(value) for value in (gate.get("blockers") or [])]
            if gate.get("status") != "READY":
                print_preflight_blocked(
                    blockers, output_root=output_root,
                    role_aware_dimension_shadow=args.role_aware_dimension_shadow,
                )
                print_terminal_summary(output_root)
                return 2
            answer_runtime_started_at = time.monotonic()
            print("[RUNTIME] 서비스 준비 완료 시점부터 최종 답변/결과 출력까지 측정합니다.")
            print("[PREFLIGHT] 준비 완료")
            _run_stage(
                "all", article_path=article_path, output_root=output_root, config_path=args.config,
                role_aware_dimension_shadow=args.role_aware_dimension_shadow,
                claim_query=article["claim_query"],
                failure_recovery_shadow=args.failure_recovery_shadow,
                user_intent_shadow=args.user_intent_shadow,
                terminal_invocation=terminal_invocation,
            )
        else:
            stages = ("l1", "l2", "layers", "live") if args.stage == "all" else (args.stage,)
            for stage in stages:
                if stage == "live":
                    gate, services, started_here = ensure_live_services(
                        config_path=args.config, output_root=output_root, services=services,
                        no_start_local_services=args.no_start_local_services, announce=True,
                    )
                    blockers = [str(value) for value in (gate.get("blockers") or [])]
                    if gate.get("status") != "READY":
                        print_preflight_blocked(
                            blockers, output_root=output_root,
                            role_aware_dimension_shadow=args.role_aware_dimension_shadow,
                        )
                        print_terminal_summary(output_root)
                        return 2
                    answer_runtime_started_at = time.monotonic()
                    print("[RUNTIME] 서비스 준비 완료 시점부터 최종 답변/결과 출력까지 측정합니다.")
                    print("[PREFLIGHT] 준비 완료")
                _run_stage(
                    stage, article_path=article_path, output_root=output_root, config_path=args.config,
                    role_aware_dimension_shadow=args.role_aware_dimension_shadow,
                    claim_query=article["claim_query"],
                    failure_recovery_shadow=args.failure_recovery_shadow,
                    user_intent_shadow=args.user_intent_shadow,
                )
        if case_mode:
            assert case_binding is not None and terminal_invocation is not None and output_root is not None
            receipt = seal_terminal_completion(
                article=article,
                article_path=article_path,
                output_root=output_root,
                config_path=args.config,
                terminal_invocation=terminal_invocation,
                case_binding=case_binding,
                argv=[str(value) for value in (argv if argv is not None else __import__("sys").argv[1:])],
            )
            print(f"[COMPLETION_RECEIPT] {receipt.resolve()}")
        automatic_annual_requery = (
            args.user_intent_shadow and output_root is not None
            and intent_requires_annual_requery(output_root)
        )
        if args.annual_requery_shadow or automatic_annual_requery:
            if automatic_annual_requery and not args.annual_requery_shadow:
                print("[INTENT] 기간 비교 의도에 따라 동일 series 전년 셀을 자동 재질의합니다.")
            run_annual_requery_shadow(output_root)
        print_terminal_summary(output_root)
        if answer_runtime_started_at is not None:
            elapsed = time.monotonic() - answer_runtime_started_at
            print(f"[RUNTIME] 서비스 준비 완료→최종 답변/결과 출력={elapsed:.2f}초")
        return 0
    except (TerminalInputError, ArticleAcquisitionError, TraceStageError, LocalServiceStartError, AnnualRequeryError, SealingError) as exc:
        print(f"[PIPELINE] BLOCKED {str(exc.args[0]) if exc.args else 'TERMINAL_RUN_FAILED'}")
        if output_root is not None and output_root.exists():
            print_terminal_summary(output_root)
        return 2
    finally:
        if services is not None:
            services.stop_owned()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTRACT_VERSION", "COMPLETION_RECEIPT_CONTRACT", "TerminalInputError", "build_article", "classify_input_mode", "default_output_root", "main",
    "annual_requery_path", "ensure_live_services", "input_path_for_output", "intent_requires_annual_requery", "load_terminal_input",
    "print_preflight_blocked", "print_terminal_summary", "run_annual_requery_shadow",
    "read_multiline_body", "write_terminal_input", "terminal_provenance_path", "load_terminal_provenance",
    "completion_receipt_path", "load_case_file", "seal_terminal_completion", "validate_completion_receipt",
    "acquisition_root_for_output", "INPUT_BODY_AND_QUERY", "INPUT_SUPPORTED_KHAN_URL_AND_QUERY",
    "INPUT_QUERY_ONLY_SYNTHETIC_CLAIM",
]
